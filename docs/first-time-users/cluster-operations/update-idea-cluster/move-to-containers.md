# Move the control plane to containers

From this release the cluster manager, the scheduler and the virtual desktop controller with its DCV broker and connection gateway can run as container tasks on a small pool of hosts, instead of one host each. A cluster moves over once, with `upgrade-cluster`; later upgrades replace tasks while preserving running jobs, with a brief scheduler submission/API interruption. This page is the runbook for that one move.

## Before you start

* The cluster must be on the previous published release. `upgrade-cluster` refuses an older one; upgrade it one release at a time first.
* The account must have the `awsvpcTrunking` ECS setting enabled in the cluster's region. The upgrade checks and refuses otherwise.
* The container hosts are Graviton (`ecs.hosts.instance_type`, default `m7g.large`). Check the type is offered in the cluster's subnets' availability zones.
* The control plane image must be reachable from the cluster: `ecs.image` defaults to the release image in the public repository for the commercial partition. A partition with no repository entry (GovCloud) needs the image pushed to a repository in that account and `ecs.image` set to it, or the stack refuses.
* Resolve held and permanently blocked jobs before the window. Draining waits for queued, running and all other unfinished jobs; it does not release holds, cancel jobs or repair jobs that cannot start.
* Job submission will close for the length of the run, and users see a maintenance message when they try to submit. Announce a window.

## Prepare the local values file

Before editing values, preserve an existing local copy. If it is absent, download the cluster's copy:

```bash
mkdir -p "$HOME/.idea/clusters/<CLUSTER_NAME>/<REGION>"
./idea-admin.sh config download-values --cluster-name <CLUSTER_NAME> --aws-region <REGION>
```

Use `~/.idea/clusters/<CLUSTER_NAME>/<REGION>/values.yml` for the following edits. If no S3 copy
exists, restore a known-good file from backup before proceeding.

## Prepare metrics before the migration

With `metrics_provider: dogstatsd` the modules send their own metrics to a Datadog agent over DogStatsD. Names are prefixed `idea.` and tagged `idea_cluster`, `idea_module` and `component`; the scheduler publishes `idea.job.count`, `idea.job.duration_seconds`, `idea.job.cost`, `idea.job.cost_ondemand`, `idea.job.savings` and `idea.job.cpu_efficiency` as each job completes, sliced by project, owner, queue, instance family and outcome, plus `idea.job.detail.cost` per job, tagged `job_id`, `job_uid` and `instance_type`, for drill-down (not on CloudWatch, which prices every dimension set as its own metric). On a container cluster the host pool runs the agent as a daemon on every host, every task shares its socket, and the agent adds its own container and host metrics tagged `idea_cluster:<cluster>` with the module in `service`. None of this uses the Datadog AWS integration; that stays a per-account setting on the Datadog side.

### Once per cluster

Complete this before running the upgrade when selecting DogStatsD. Both daemon values are required during configuration generation. Neither the host daemon nor the cost-only sidecar supplies `DD_SITE`; with the standard agent image, ingestion uses `datadoghq.com` (US1). Other sites need a deployment code change; `--datadog-site` only changes the proof query destination.

1. Select the target account profile and region, verify the caller ARN, then put the API key in Secrets Manager. The key never enters `values.yml` or the settings table, only the secret's ARN does, and the agent reads it when a task starts.

```bash
aws --profile <PROFILE> --region <REGION> sts get-caller-identity --query Arn
aws --profile <PROFILE> --region <REGION> secretsmanager create-secret \
  --name idea-<CLUSTER_NAME>-datadog-api-key --secret-string '<API_KEY>' --query ARN
```

2. Copy the agent image into a private ECR repository in the account and record its digest. The agent runs with the host's Docker socket and process namespace, so the stack accepts only a digest-pinned image from a private repository.

```bash
aws --profile <PROFILE> --region <REGION> ecr create-repository --repository-name datadog/agent
aws --profile <PROFILE> --region <REGION> ecr get-login-password | \
  docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com
for arch in arm64 amd64; do
  docker pull --platform "linux/$arch" public.ecr.aws/datadog/agent:7.83.1
  docker tag public.ecr.aws/datadog/agent:7.83.1 <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/datadog/agent:7.83.1-$arch
  docker push <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/datadog/agent:7.83.1-$arch
  aws --profile <PROFILE> --region <REGION> ecr describe-images --repository-name datadog/agent \
    --image-ids imageTag=7.83.1-$arch --query 'imageDetails[0].imageDigest' --output text
done
```

Verify the caller ARN is for the target account before creating resources. Reuse the repository if it already exists; verify each returned digest with `docker buildx imagetools inspect <REGISTRY>/datadog/agent@sha256:<DIGEST>`, and use the ARM64 digest for the default Graviton hosts and the AMD64 digest for cost-only Fargate tasks.

3. Record both in `values.yml`:

```yaml
metrics_provider: dogstatsd
datadog_api_key_secret_arn: arn:aws:secretsmanager:<REGION>:<ACCOUNT>:secret:idea-<CLUSTER_NAME>-datadog-api-key-XXXXXX
datadog_agent_image: <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/datadog/agent@sha256:<DIGEST>
```

A fresh install asks for both when Datadog is the metrics provider. During migration, values generate missing `ecs.enabled` and `ecs.datadog.{enabled,api_key_secret_arn,image}` rows, and missing metrics rows. Synchronization is add-only: on a cluster that ran the previous release, `metrics.provider` and any existing `metrics.dogstatsd.url` remain unchanged, so explicitly set the live rows before the migration:

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=metrics.provider,Type=str,Value=dogstatsd' \
  'Key=metrics.dogstatsd.url,Type=str,Value=unix:///var/run/datadog/dsd.socket'
```

Existing ECS rows are also preserved. If an earlier attempt or deployment already created them, apply the five-row command below as well. Keep the three values-file keys above for future generation; editing them alone does not update live rows. Metrics on old hosts may be interrupted until the applications move to tasks with the socket mounted.

`ecs.image` and `ecs.hosts.instance_type` are settings-table keys, not values-file overrides. If you need a private control-plane image or a different host type, set them before deployment (choose an image supporting the host architecture):

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=ecs.image,Type=str,Value=<CONTROL_PLANE_IMAGE>' \
  'Key=ecs.hosts.instance_type,Type=str,Value=<INSTANCE_TYPE>'
```

### On a cluster already running containers

Full configuration synchronization adds missing non-global rows and preserves existing ones, so switching an existing container cluster is a direct write of the rows the daemon and the modules read, followed by a deploy that gives every task the agent's socket:

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=metrics.provider,Type=str,Value=dogstatsd' \
  'Key=metrics.dogstatsd.url,Type=str,Value=unix:///var/run/datadog/dsd.socket' \
  'Key=ecs.datadog.enabled,Type=bool,Value=true' \
  'Key=ecs.datadog.api_key_secret_arn,Type=str,Value=<SECRET_ARN>' \
  'Key=ecs.datadog.image,Type=str,Value=<IMAGE@sha256:DIGEST>'
./idea-admin.sh upgrade-cluster --cluster-name <CLUSTER_NAME> --aws-region <REGION>
```

Put `metrics_provider`, `datadog_api_key_secret_arn` and `datadog_agent_image` in `values.yml` as well, so a later regeneration agrees with the table.

## Run it

1. Edit and verify `~/.idea/clusters/<CLUSTER_NAME>/<REGION>/values.yml` locally: set `enable_ecs: true` and complete the Datadog preparation above if using DogStatsD. Do not rely on restoration from S3 during this migration: the trunking and scheduler-cutover gates inspect local values before restoration.
2. Review configuration drift before the maintenance window with `./idea-admin.sh config preview-upgrade --cluster-name <CLUSTER_NAME> --aws-region <REGION>`. Full synchronization preserves existing non-global rows, but global settings are deleted and regenerated, and Phase 3 overwrites the AMI/settings rows shown in the preview; accepting drift permits those differing values to be lost.
3. Run the full upgrade with the drain:

```bash
./idea-admin.sh upgrade-cluster --cluster-name <CLUSTER_NAME> --aws-region <REGION> --drain
```

4. Review any drift overwrite prompt, then answer yes to global settings backup/update, full configuration synchronization, AMI/settings updates and deployment of all modules. Full synchronization is required to register the new ECS module; do not skip it. `--force` accepts the phase prompts, but differing overwritten rows require separate `--accept-config-drift` after review.

## What happens, in order

* The upgrade verifies the release floor and trunking, saves the maintenance baseline and writes maintenance settings to close submission, then inventories/drains the host scheduler.
* Base OS and end-of-life validation, configuration preview and applicable confirmations follow. Maintenance has already been written, so a refusal here can leave submission closed until a successful retry.
* The scheduler's DNS record is retained with a policy-only stack update before the upgrade phases, so removing the host does not delete its name.
* Global settings are regenerated, full synchronization adds missing rows and registers ECS, and Phase 3 applies the previewed AMI/settings updates. ECS module-set registration remains held until cluster-manager records the target release as deployed and the deployment completes.
* Stacks deploy in dependency order. The new `ecs` stack creates the host pool. Cluster-manager cuts its load balancer rules over to the container service; the portal is unavailable for a few minutes. The desktop stack moves the controller, broker and gateway to services. The scheduler starts on an empty job database and retires its host. The bastion host is recreated once.
* After deployment and a successful values upload, the original maintenance state is restored. A failed run retains the saved baseline for a completed retry.

## After

* Job ids start again from zero, once. The scheduler's database now lives on a file system that outlives its task and preserves running jobs through later replacements.
* The bastion has a new public address. Update anything that pinned the old one.
* `./idea-admin.sh check-cluster-status --cluster-name <CLUSTER_NAME> --aws-region <REGION>` checks application HTTP endpoints and the analytics dashboard. Separately check ECS desired/running counts and service events, create/connect/delete a desktop, and run the [proof matrix](../../../../source/idea/ideactl/tools/e2e/README.md) desktop and `metrics-sink` checks to verify desktop connectivity and Datadog delivery.

## Spend and storage

The same agent carries the account's spend and the ONTAP storage levels once the cluster-manager collectors are on. Both are settings of the cluster-manager module, off by default, and both publish through `metrics.provider`. Cost collection requires DogStatsD, while storage supports DogStatsD or CloudWatch.

Spend comes from Cost Explorer, commercial partition only: every trailing full day, re-read on each run so revisions land. `idea.cost.amortized` and `idea.cost.unblended` are partitioned by `module`, `project` and `owner` from the cost allocation tags; `idea.cost.by_service.*` by `module` and `service`; `idea.cost.storage.*` by `service` and `usage_type` for FSx and EFS, tag-blind so history from before the tags survives. Spend that carries no module tag is `module:unknown`, which is where savings plan and reservation credits land. Each point is one day's total stamped at that day, so historical metrics ingestion must be on for the `idea.cost` prefix in Datadog before the first run, or the points are dropped silently.

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=cluster-manager.metrics.cost.enabled,Type=bool,Value=true'
./idea-admin.sh deploy --cluster-name <CLUSTER_NAME> --aws-region <REGION> --upgrade cluster-manager
```

After deploying, force a new cluster-manager service deployment using the restart commands below; a settings-only deploy does not ensure collector startup. The deploy grants the cluster-manager role `ce:GetCostAndUsage`, `ce:GetTags` and `ce:GetDimensionValues`; the tag keys default to `idea:ModuleId`, `idea:Project` and `idea:JobOwner` and follow `cluster-manager.metrics.cost.*`.

For GovCloud billing, or a commercial billing account with no cluster, use cost-only mode in the commercial account that can read the bill. It runs the same collector and a Datadog sidecar in one Fargate task, with no cluster settings table. Enable historical ingestion for `idea.cost` as above, activate the cost allocation tags in that billing account, and deploy:

Prepare the billing account once, exactly as for a cluster: copy the control-plane image and the Datadog agent image into that account's ECR and record the agent digest, create the Datadog API key secret there, and make sure the subnets you name can reach Cost Explorer and Datadog. The collector tags every point with the `--cluster-name` you give, so use the name the metrics should carry, not the billing account's.

```bash
ideactl cost-collector deploy --aws-profile <BILLING_PROFILE> --aws-region us-east-1 --stack-name gov-spend \
  --cluster-name <GOVCLOUD_CLUSTER_NAME> \
  --control-plane-image <CONTROL_PLANE_IMAGE> \
  --agent-image <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/datadog/agent@sha256:<AMD64_DIGEST> \
  --datadog-api-key-secret-arn <SECRET_ARN> --subnet-ids <SUBNET_ID> <SUBNET_ID>
```

Use images supporting Linux x86_64 and an API key secret in the deployment region. Subnets must be in one VPC and all public or all private. Public subnets receive a public IP; private subnets need outbound access through NAT. The default interval is six hours with a three-day lookback and the same tag keys as the cluster collector; `--by-account` adds linked account spend. `--cluster-name` labels the account's bill; it does not filter it to that cluster. Remove it with `ideactl cost-collector destroy --aws-region us-east-1 --stack-name gov-spend`.

To verify cost-only delivery, wait for a collection cycle, then query `sum:idea.cost.amortized{idea_cluster:<CLUSTER_NAME>}` in Datadog over the trailing three full UTC days (or the configured lookback), including yesterday; require non-null daily points and compare them with Cost Explorer. `metrics-sink` checks API invocations, which this task does not emit. In ECS, open the task's Logs tab and inspect both `cost-metrics/cost-metrics/<TASK_ID>` and `datadog/datadog/<TASK_ID>` in the stack-created log group for collection failures, dropped historical points or ingestion errors.

Storage levels come from each FSx for NetApp ONTAP file system in `shared-storage` that carries metrics credentials: an ONTAP user that can read `/api/storage/quota/reports` and `/api/storage/volumes` on the SVM management endpoint, its password in a Secrets Manager secret tagged `idea:ClusterName=<CLUSTER_NAME>` and `idea:ModuleName=cluster-manager`, which the cluster-manager role can already read. `idea.storage.used_bytes` and `idea.storage.files_used` are per `user`, `volume` and `qtree`; `idea.storage.volume_size_bytes`, `idea.storage.volume_used_bytes` and `idea.storage.volume_tier_bytes` (`tier:ssd`, `tier:capacity_pool`) per volume; every point carries `svm` and `filesystem`.

Create the ONTAP user on the file system's cluster shell, once per file system, and its password secret once per cluster that reads it. The SVM shell has no `security login` commands, so this is `fsxadmin` on the management endpoint (set its password in the FSx console first):

```bash
ssh fsxadmin@management.<FILE_SYSTEM_ID>.fsx.<REGION>.amazonaws.com
security login rest-role create -vserver <SVM> -role idea-metrics -api /api/storage/quota/reports -access readonly
security login rest-role create -vserver <SVM> -role idea-metrics -api /api/storage/volumes -access readonly
security login create -vserver <SVM> -user-or-group-name idea-metrics -application http -authentication-method password -role idea-metrics
```

```bash
aws --profile <PROFILE> --region <REGION> secretsmanager create-secret --name idea-<CLUSTER_NAME>-ontap-metrics-<NAME> --secret-string '<password>' \
  --tags Key=idea:ClusterName,Value=<CLUSTER_NAME> Key=idea:ModuleName,Value=cluster-manager --query ARN
```

The two tags are what let the cluster-manager role read the secret; without them the collector logs a read failure and publishes nothing for that file system. Then point the settings at both:

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=shared-storage.<NAME>.fsx_netapp_ontap.metrics.username,Type=str,Value=<ONTAP_USER>' \
  'Key=shared-storage.<NAME>.fsx_netapp_ontap.metrics.password_secret_arn,Type=str,Value=<SECRET_ARN>' \
  'Key=cluster-manager.metrics.storage.enabled,Type=bool,Value=true'
```

Storage polls every 60 minutes by default (`cluster-manager.metrics.storage.interval_minutes`), and certificate verification defaults to false (`cluster-manager.metrics.storage.verify_tls`). To verify certificates, install a trusted CA chain in the application image and set `Key=cluster-manager.metrics.storage.verify_tls,Type=bool,Value=true` with `config set`.

After settings changes, deploy any required IAM changes with `deploy --upgrade cluster-manager`, then explicitly restart the service. A settings-only deploy may leave the task definition unchanged and does not guarantee a restart:

```bash
./idea-admin.sh config show --cluster-name <CLUSTER_NAME> --aws-region <REGION> --query 'ecs.cluster_name' --format raw
aws --profile <PROFILE> --region <REGION> ecs list-services --cluster <ECS_CLUSTER>
aws --profile <PROFILE> --region <REGION> ecs update-service --cluster <ECS_CLUSTER> \
  --service <CLUSTER_MANAGER_SERVICE> --force-new-deployment
aws --profile <PROFILE> --region <REGION> ecs wait services-stable --cluster <ECS_CLUSTER> --services <CLUSTER_MANAGER_SERVICE>
```

Select the cluster-manager service from the returned ARNs. Check its application logs for collector startup and provider/credential errors.

### Rotating the key

Write the new value into the same secret, then restart the daemon so its tasks read it: `aws ecs update-service --force-new-deployment` on the datadog service of the cluster's ECS cluster, whose name is the `ecs.cluster_name` setting. Nothing in IDEA changes.

## Routine upgrades from here

Point `ecs.image` at the new release's image and run `upgrade-cluster` without `--drain`. Most services roll behind their load balancer; the scheduler stops its old task before starting the replacement, briefly interrupting submissions and its API while running jobs are designed to survive on persistent PBS state.

The change-set guard refuses definite replacements of any resource, conditional replacements of stateful resources, stateful removals and unrecognized custom-resource removals, with specific named exceptions such as retained task-definition revisions. Review any refusal and its data/lifecycle impact. `upgrade-cluster` has no `--allow-replacement` option; a deliberate exception requires a separately reviewed `deploy --upgrade <MODULE_ID> --allow-replacement <LOGICAL_ID>` operation.
