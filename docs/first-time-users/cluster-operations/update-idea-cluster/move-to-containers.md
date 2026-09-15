# Move the control plane to containers

From this release the cluster manager, the scheduler and the virtual desktop controller with its DCV broker and connection gateway can run as container tasks on a small pool of hosts, instead of one host each. A cluster moves over once, with `upgrade-cluster`; every later upgrade is then a rolling image change that keeps jobs running. This page is the runbook for that one move.

## Before you start

* The cluster must be on the previous published release. `upgrade-cluster` refuses an older one; upgrade it one release at a time first.
* The account must have the `awsvpcTrunking` ECS setting enabled in the cluster's region. The upgrade checks and refuses otherwise.
* The container hosts are Graviton (`ecs.hosts.instance_type`, default `m7g.large`). Check the type is offered in the cluster's subnets' availability zones.
* The control plane image must be reachable from the cluster: `ecs.image` defaults to the release image in the public repository for the commercial partition. A partition with no repository entry (GovCloud) needs the image pushed to a repository in that account and `ecs.image` set to it, or the stack refuses.
* Job submission will close for the length of the run, and users see a maintenance message when they try to submit. Announce a window.

## Run it

1. Add `enable_ecs: true` to the cluster's `values.yml` (the local copy under `~/.idea/clusters/<CLUSTER_NAME>/<REGION>/`, or the copy the upgrade restores from the cluster bucket).
2. Run the upgrade with the drain:

```
./idea-admin.sh upgrade-cluster --cluster-name <CLUSTER_NAME> --aws-region <REGION> --drain
```

3. Read the configuration preview it prints and confirm. Rows you changed by hand are preserved; the run says which.

## What happens, in order

* The scheduler's DNS record is retained on its stack with a policy-only update, so the container scheduler can take the name over without CloudFormation deleting it.
* Job submission closes and the run waits until the host scheduler holds no job. Nothing is written before the inventory is empty.
* Settings are written: the global rows are rewritten, the container module's rows are added, and the module's registration in the module set is held until the last stack has deployed, so the running portal keeps working through the upgrade.
* Stacks deploy in dependency order. A new `ecs` stack creates the host pool. The cluster-manager stack cuts its load balancer rules over to the container service; the portal is unavailable for a few minutes until that stack completes. The desktop stack moves the controller, broker and gateway to services. The scheduler stack starts the scheduler task on an empty job database and retires the host. The bastion host is recreated once.
* Submission reopens with the maintenance flag restored to what it was.

## After

* Job ids start again from zero, once. From now on the scheduler's job database lives on a file system that outlives every task, and later upgrades carry running jobs across.
* The bastion has a new public address. Anything that pinned the old one needs the new one.
* `./idea-admin.sh check-cluster-status --cluster-name <CLUSTER_NAME> --aws-region <REGION>` should report every module healthy.

## Metrics to Datadog

With `metrics_provider: dogstatsd` the modules send their own metrics to a Datadog agent over DogStatsD. Names are prefixed `idea.` and tagged `idea_cluster`, `idea_module` and `component`; the scheduler publishes `idea.job.count`, `idea.job.duration_seconds`, `idea.job.cost`, `idea.job.cost_ondemand`, `idea.job.savings` and `idea.job.cpu_efficiency` as each job completes. On a container cluster the host pool runs the agent as a daemon on every host, every task shares its socket, and the agent adds its own container and host metrics tagged `idea_cluster:<cluster>` with the module in `service`. None of this uses the Datadog AWS integration; that stays a per-account setting on the Datadog side.

### Once per cluster

1. Put the API key in Secrets Manager. The key never enters `values.yml` or the settings table, only the secret's ARN does, and the agent reads it when a task starts.

```bash
aws secretsmanager create-secret --name idea-<CLUSTER_NAME>-datadog-api-key --secret-string '<API key>' --query ARN
```

2. Copy the agent image into a private ECR repository in the account and record its digest. The agent runs with the host's Docker socket and process namespace, so the stack accepts only a digest-pinned image from a private repository.

```bash
aws ecr create-repository --repository-name datadog/agent
docker pull public.ecr.aws/datadog/agent:7.83.1
docker tag public.ecr.aws/datadog/agent:7.83.1 <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/datadog/agent:7.83.1
docker push <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/datadog/agent:7.83.1
aws ecr describe-images --repository-name datadog/agent --query 'imageDetails[0].imageDigest'
```

3. Record both in `values.yml`:

```yaml
metrics_provider: dogstatsd
datadog_api_key_secret_arn: arn:aws:secretsmanager:<REGION>:<ACCOUNT>:secret:idea-<CLUSTER_NAME>-datadog-api-key-XXXXXX
datadog_agent_image: <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/datadog/agent@sha256:<DIGEST>
```

A fresh install asks for both when Datadog is the metrics provider. The move to containers generates the container module's settings from them, so `upgrade-cluster --drain` turns the daemon on in the same run. Later upgrades regenerate the same values and never ask for the key again.

### On a cluster already running containers

An upgrade adds settings rows it has not seen and leaves existing rows alone, so switching an existing container cluster is a direct write of the rows the daemon and the modules read, followed by a deploy that gives every task the agent's socket:

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=metrics.provider,Type=str,Value=dogstatsd' \
  'Key=metrics.dogstatsd.url,Type=str,Value=unix:///var/run/datadog/dsd.socket' \
  'Key=ecs.datadog.enabled,Type=bool,Value=true' \
  'Key=ecs.datadog.api_key_secret_arn,Type=str,Value=<SECRET_ARN>' \
  'Key=ecs.datadog.image,Type=str,Value=<IMAGE@sha256:DIGEST>'
./idea-admin.sh upgrade-cluster --cluster-name <CLUSTER_NAME> --aws-region <REGION>
```

Put the same three keys in `values.yml` as well, so a later regeneration agrees with the table.

### Spend and storage

The same agent carries the account's spend and the ONTAP storage levels once the cluster-manager collectors are on. Both are settings of the cluster-manager module, off by default, and both publish through `metrics.provider`, so they work with any provider.

Spend comes from Cost Explorer, commercial partition only: every trailing full day, re-read on each run so revisions land. `idea.cost.amortized` and `idea.cost.unblended` are partitioned by `module`, `project` and `owner` from the cost allocation tags; `idea.cost.by_service.*` by `module` and `service`; `idea.cost.storage.*` by `service` and `usage_type` for FSx and EFS, tag-blind so history from before the tags survives. Spend that carries no module tag is `module:unknown`, which is where savings plan and reservation credits land. Each point is one day's total stamped at that day, so historical metrics ingestion must be on for the `idea.cost` prefix in Datadog before the first run, or the points are dropped silently.

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=cluster-manager.metrics.cost.enabled,Type=bool,Value=true'
./idea-admin.sh deploy --cluster-name <CLUSTER_NAME> --aws-region <REGION> --upgrade cluster-manager
```

The deploy grants the cluster-manager role `ce:GetCostAndUsage`, `ce:GetTags` and `ce:GetDimensionValues`; the tag keys default to `idea:ModuleId`, `idea:Project` and `idea:JobOwner` and follow `cluster-manager.metrics.cost.*`.

Storage levels come from each FSx for NetApp ONTAP file system in `shared-storage` that carries metrics credentials: an ONTAP user that can read `/api/storage/quota/reports` and `/api/storage/volumes` on the SVM management endpoint, its password in a Secrets Manager secret tagged `idea:ClusterName=<CLUSTER_NAME>` and `idea:ModuleName=cluster-manager`, which the cluster-manager role can already read. `idea.storage.used_bytes` and `idea.storage.files_used` are per `user`, `volume` and `qtree`; `idea.storage.volume_size_bytes`, `idea.storage.volume_used_bytes` and `idea.storage.volume_tier_bytes` (`tier:ssd`, `tier:capacity_pool`) per volume; every point carries `svm` and `filesystem`.

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=shared-storage.<NAME>.fsx_netapp_ontap.metrics.username,Type=str,Value=<ONTAP_USER>' \
  'Key=shared-storage.<NAME>.fsx_netapp_ontap.metrics.password_secret_arn,Type=str,Value=<SECRET_ARN>' \
  'Key=cluster-manager.metrics.storage.enabled,Type=bool,Value=true'
```

The collectors pick the settings up on the next cluster-manager restart; `deploy --upgrade cluster-manager` is one.

### Rotating the key

Write the new value into the same secret, then restart the daemon so its tasks read it: `aws ecs update-service --force-new-deployment` on the datadog service of the cluster's ECS cluster, whose name is the `ecs.cluster_name` setting. Nothing in IDEA changes.

## Routine upgrades from here

Point `ecs.image` at the new release's image and run `upgrade-cluster` without `--drain`. Each service rolls to a new task definition revision behind its load balancer; a job running through the scheduler roll finishes normally. The upgrade refuses any change that would replace or remove a stateful resource and names it, and accepts a task definition revision by name, since the previous revision is kept.
