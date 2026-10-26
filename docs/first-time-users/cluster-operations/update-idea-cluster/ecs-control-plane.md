# ECS control-plane operations

IDEA 26.11.1 deploys the control-plane services only on ECS. Direct upgrades require every deployed module to be at 26.10.0 or newer. Complete any host-to-ECS migration with the 26.10.9 administrator before upgrading. See the [recovery command](upgrade-cluster.md#recovery-with-the-26109-administrator).

The OpenLDAP directory option still uses an EC2 server. Its server, bootstrap package and settings remain because existing ECS clusters use them.

## Prepare metrics for ECS

With `metrics_provider: dogstatsd` the modules send their own metrics to a Datadog agent over DogStatsD. Names are prefixed `idea.` and tagged `idea_cluster`, `idea_module` and `component`, plus `env` on non-job runtime series (see below); the scheduler publishes `idea.job.count`, `idea.job.duration_seconds`, `idea.job.cost`, `idea.job.cost_ondemand`, `idea.job.savings` and `idea.job.cpu_efficiency` as each job completes, sliced by project, owner, queue, instance family and outcome, plus `idea.job.detail.cost` per job, tagged `job_id`, `job_uid` and `instance_type`, for drill-down (not on CloudWatch, which prices every dimension set as its own metric). On a container cluster the host pool runs the agent as a daemon on every host, every task shares its socket, and the agent adds its own container and host metrics tagged `idea_cluster:<cluster>` with the module in `service`. None of this uses the Datadog AWS integration; that stays a per-account setting on the Datadog side.

Native runtime Datadog metrics other than `job.*` carry `env:<cluster name>` alongside `idea_cluster:<cluster name>`. Cost and storage collector series, including standalone collection and historical publishing, retain their historical tags without `env`, container or host identity. Filter collector metrics by `idea_cluster`; changing their tags during a replay would create duplicate series. Job series also retain their historical module and component tags without `env`, for both live sends and backfills at every timestamp. There is no environment-tag cutover for existing replayable series in this release; introducing one requires an explicit migration separating old and new series.

### Job CPU efficiency

| Metric | Samples included |
| --- | --- |
| `idea.job.cpu_efficiency` | Existing rule: measured CPU seconds divided by allocated CPU seconds. Missing or invalid measurements are omitted; measured zero CPU is included as zero. |
| `idea.job.cpu_efficiency.measured` | Explicit measured-only series for dashboards replacing IDEAL. Uses the same formula and validity checks, includes measured zero CPU, and omits jobs without a usable CPU measurement. |

Both are ratios from 0 to 1 with identical tags and job-end timestamps. They currently contain the same samples; the existing series never substituted zero for missing measurements. Multi-node partial measurements and impossible ratios are excluded from both. Select one series per dashboard, without combining them.

### Once per cluster

Complete this before running the upgrade when selecting DogStatsD. The secret's ARN is required during configuration generation; the agent image has a release default. Neither the host daemon nor the cost-only sidecar supplies `DD_SITE`; with the standard agent image, ingestion uses `datadoghq.com` (US1). Other sites need a deployment code change; `--datadog-site` only changes the proof query destination.

1. Select the target account profile and region, verify the caller ARN, then put the API key in Secrets Manager. The key never enters `values.yml` or the settings table, only the secret's ARN does, and the agent reads it when a task starts.

```bash
aws --profile <PROFILE> --region <REGION> sts get-caller-identity --query Arn
aws --profile <PROFILE> --region <REGION> secretsmanager create-secret \
  --name idea-<CLUSTER_NAME>-datadog-api-key --secret-string '<API_KEY>' --query ARN
```

2. The agent image is Datadog's official image from their public ECR gallery, pinned by digest to the version this release ships. Nothing to copy. To run another build, name any digest-pinned reference in `datadog_agent_image`, at your own risk: the agent holds the host's Docker socket and process namespace, so a tag is refused.

3. Record the provider and the secret in `values.yml`:

```yaml
metrics_provider: dogstatsd
datadog_api_key_secret_arn: arn:aws:secretsmanager:<REGION>:<ACCOUNT>:secret:idea-<CLUSTER_NAME>-datadog-api-key-XXXXXX
```

A fresh install asks for the secret when Datadog is the metrics provider. During an upgrade, configuration synchronization adds missing settings. Phase 3 updates `metrics.provider` and `metrics.dogstatsd.url` from the generated values, and the preview lists both under `PROVIDER_CUTOVER`. This lets a supported ECS cluster change metrics providers without a separate `config set`.

`ecs.image` and `ecs.hosts.instance_type` are settings-table keys, not values-file overrides. If you need a private control-plane image or a different host type, set them before deployment (choose an image supporting the host architecture):

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=ecs.image,Type=str,Value=<CONTROL_PLANE_IMAGE>' \
  'Key=ecs.hosts.instance_type,Type=str,Value=<INSTANCE_TYPE>'
```

### On a cluster already running containers

Put `metrics_provider: dogstatsd` and `datadog_api_key_secret_arn` in `values.yml` and run `upgrade-cluster`. The `ecs.datadog.*` rows are added if absent, the provider rows are written as above, the daemon service is created and every task is redeployed with the agent's socket. A cluster whose `ecs.datadog.*` rows already exist from an earlier attempt keeps them; correct those with `config set` first if they name a different secret or image.

## Service and host maintenance

* Existing SSH sessions and transfers end when their task stops; reconnect to the same address. The service defaults to two tasks, with shared host keys, so new connections can use the remaining task during replacement. Settings `ecs.tasks.bastion-host.{cpu,memory,desired}` control sizing.
* Fail2ban is not run in the container. The existing bastion security group still restricts SSH sources, and the NLB preserves client IPs. Outbound SSH host-check behavior and the host's TCP transfer tuning carry over. `/data` and `/apps` use the shared storage mounted by the other module tasks.
* The container hosts join the directory (Active Directory or OpenLDAP) after they register with the cluster, through the same automation compute nodes use, so the kernel maps NFSv4 owner names on the shared home file system and every container sees real owners; the bastion service is placed only on hosts that have joined (`aws ecs list-attributes --cluster <ECS_CLUSTER> --target-type container-instance` shows `idea.directory=joined`). A host that cannot reach the directory keeps retrying for two hours and stays available to the other services meanwhile. Terminated hosts have their computer objects removed. Hosts are replaced only when you ask: `upgrade-cluster --refresh-hosts` (or `deploy --refresh-hosts` for the `ecs` module) refreshes the host auto scaling group after the ecs stack deploys, new hosts first, with the capacity provider's managed draining moving the tasks. Without the flag the upgrade stops right after the ecs stack when a host on an older launch template has not joined, and prints the re-run command; before the bastion deploys every host is waited for until it reports `idea.directory=joined`. `id <USERNAME>` on a host is the check. Home directories and SSH keys on a stale uid or gid from an earlier account converge to the account's ids at the next accounts reconcile pass (the login path at once, the rest of the tree as a background task); `Accounts.RepairHomeOwnership` runs it on demand with a dry run. Each host takes its directory computer name as its hostname, as compute nodes do; the cluster keys hosts by instance id. On container clusters the directory automation queue uses SQS-managed encryption (a customer key from `cluster.sqs.kms_key_id` stays, and its key policy must then allow EventBridge to send), so terminated hosts' computer objects can be removed.
* `./idea-admin.sh check-cluster-status --cluster-name <CLUSTER_NAME> --aws-region <REGION>` checks application HTTP endpoints and the analytics dashboard. Separately check ECS desired/running counts and service events, create/connect/delete a desktop, and run the [proof matrix](../../../../source/idea/ideactl/tools/e2e/README.md) desktop and `metrics-sink` checks to verify desktop connectivity and Datadog delivery.

## Spend and storage

The same agent carries the account's spend and the ONTAP storage levels once the cluster-manager collectors are on. Both are settings of the cluster-manager module, off by default, and both publish through `metrics.provider`. Cost collection requires DogStatsD, while storage supports DogStatsD or CloudWatch.

Spend comes from Cost Explorer, commercial partition only: every trailing full day, re-read on each run so revisions land. `idea.cost.amortized` and `idea.cost.unblended` are partitioned by `module`, `project` and `owner` from the cost allocation tags; `idea.cost.by_service.*` by `module` and `service`; `idea.cost.storage.*` by `service` and `usage_type` for FSx and EFS, tag-blind so history from before the tags survives. Spend that carries no module tag is `module:unknown`, which is where savings plan and reservation credits land. Each point is one day's total stamped at that day, so historical metrics ingestion must be on for the `idea.cost` prefix in Datadog before the first run, or the points are dropped silently.

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=cluster-manager.metrics.cost.enabled,Type=bool,Value=true'
```

After upgrading to this release, the collector checks the setting every cycle, including while disabled. Enabling starts collection on the next cycle; disabling stops subsequent cycles. The default interval is six hours (`cluster-manager.metrics.cost.interval_hours`). No additional redeploy or service restart is needed: the task role already permits `ce:GetCostAndUsage`, `ce:GetTags` and `ce:GetDimensionValues`. The tag keys default to `idea:ModuleId`, `idea:Project` and `idea:JobOwner` and follow `cluster-manager.metrics.cost.*`.

For GovCloud billing, or a commercial billing account with no cluster, use cost-only mode in the commercial account that can read the bill. It runs the same collector and a Datadog sidecar in one Fargate task, with no cluster settings table. Enable historical ingestion for `idea.cost` as above, activate the cost allocation tags in that billing account, and deploy:

Prepare the billing account once: create the Datadog API key secret there and make sure the subnets you name can reach Cost Explorer, Datadog and both public registries (or pass `--control-plane-image` and `--agent-image` for images reachable from that account). The agent image defaults to the same official Datadog image the clusters use. The collector tags every point with the `--cluster-name` you give, so use the name the metrics should carry, not the billing account's.

```bash
ideactl cost-collector deploy --aws-profile <BILLING_PROFILE> --aws-region us-east-1 --stack-name gov-spend \
  --cluster-name <GOVCLOUD_CLUSTER_NAME> \
  --control-plane-image <CONTROL_PLANE_IMAGE> \
  --datadog-api-key-secret-arn <SECRET_ARN> --subnet-ids <SUBNET_ID> <SUBNET_ID>
```

The task runs on Linux arm64 (Graviton) like the control-plane image; the default agent image is multi-architecture, and any override must support Linux arm64. Use an API key secret in the deployment region. Subnets must be in one VPC and all public or all private. Public subnets receive a public IP; private subnets need outbound access through NAT. The default interval is six hours with a three-day lookback and the same tag keys as the cluster collector; `--by-account` adds linked account spend. `--cluster-name` labels the account's bill; it does not filter it to that cluster. Remove it with `ideactl cost-collector destroy --aws-region us-east-1 --stack-name gov-spend`.

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

On every partition, fresh installs default to the public release image in the generated
`ecs.image` setting. The repository map does not change that initial image. GovCloud operators
who need an in-boundary registry must set both `ecs.image_repositories.aws-us-gov` and
`ecs.image` to their mirror (in the generated configuration before first deployment, or with
`config set` on an existing cluster). An override only advances older release tags already
under that repository; an image still pointing at the public repository stays unchanged.
For a non-ECR-Public mirror, verify that the new release image is present and pass
`--skip-release-image-check` to both preview and upgrade: the built-in lookup supports only
ECR Public, and otherwise refuses the move.

When the GovCloud repository key is unset or empty, upgrades follow `ecs.image_repositories.aws`.
For its public ECR release image, preview resolves the tag to a manifest digest and the upgrade
writes that exact `tag@sha256:...` reference. `ecs.release_image` records the automatically
selected reference so later upgrades can advance it. An operator-set digest that differs from
that marker stays pinned. Digest resolution is required even with `--skip-release-image-check`
on this public fallback path. This protects task replacements from a release tag being moved.

Run `upgrade-cluster`; when `ecs.image` names the release repository at an older release tag, Phase 3 moves it to the new release tag, and an image outside the configured repository, an operator-set digest or a build tag stays as set. For a routine container upgrade starting with healthy services, cluster-manager (including the portal), VDC controller, DCV broker, DCV connection gateway, and bastion keep their desired healthy task count while replacements pass application and load-balancer readiness checks: each service starts a full set of replacements at once and stops an old task only after a replacement is healthy. No host ever holds two tasks of one service, so a two-task service needs four distinct hosts mid-rollout; the shared pool rests at four hosts (`ecs.hosts.min: 4`) with a fifth for host replacement (`ecs.hosts.max: 5`), and four `m7g.xlarge` hosts hold every service's surge at once. A cluster created before 26.10.6 keeps its `ecs.hosts.min` and `ecs.hosts.max`; set them to 4 and 5 (`ideactl config set`, then `ideactl deploy ecs --upgrade`) or a rollout waits for a host to launch. Recheck capacity when changing host types or task sizes/counts. Existing connections drain for 30 seconds before task termination (15 seconds for the scheduler); a DCV stream or SSH session on a stopping task reconnects once. Global settings are updated in place, existing module ids and obsolete rows remain available. The scheduler remains exactly one task: its healthy-count floor is zero while the old task drains for up to 15 seconds and stops, then the new task starts PBS against persistent state and passes PBS/API and load-balancer checks at five-second intervals; running jobs are designed to survive, but submissions and the scheduler API have an unavoidable gap. The Datadog daemon keeps at least half its agents healthy during an agent revision, with a collection gap on each host while its socket owner is replaced; changing only `ecs.image` does not change the daemon image. Routine image upgrades do not replace existing hosts: scale-in protection and managed draining remain enabled, and host replacement follows the separate host-maintenance procedure.

The change-set guard refuses definite replacements of any resource, conditional replacements of stateful resources, stateful removals and unrecognized custom-resource removals, with specific named exceptions such as retained task-definition revisions. It also refuses removal of VPCs, subnets, gateways, routes, route tables, and Elastic IPs. Review any refusal and its data or lifecycle impact before passing `--allow-replacement <LOGICAL_ID>` for that one entry.
