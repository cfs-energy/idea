# Full IDEA Upgrade (idea-admin.sh upgrade-cluster)

## Full Cluster Upgrade

### Overview

The `upgrade-cluster` command combines multiple steps that were previously separate into a single operation:

1. Updating the base OS configuration
2. Updating infrastructure AMIs
3. Backing up and regenerating global settings
4. Deploying all modules with the `--upgrade` flag

### Direct upgrades from 25.11.0

Every deployed module must have a readable release of 25.11.0 or newer. A cluster on 25.11.0
upgrades in one run. A run starting below 26.09.0 requires all deployed modules, including the
DCV policy transition. Before mutation it reads settings, values, deployed templates, queue
profiles, desktop stacks and sessions, IAM attachments and quotas, image metadata and instance
protection. Missing module rows or unreadable versions stop the run.

Historical upgrades always replace global settings, add missing configuration, and apply paired
OS/AMI and conditional instance-type updates. Phase prompts and skip flags cannot omit these
steps. Drift acceptance and EOL refusal still apply: a cluster more than one release behind
holds the previous release's generated defaults in many rows (GPU driver versions, package
lists, DCV package URLs), which the preview lists as differing from generated configuration,
so a historical run needs `--accept-config-drift` after reviewing that preview. An operator
pointing `ecs.image` at a private registry before the run (partitions without a public
registry) keeps that row; the run registers the container module around it. With `enable_ecs: true`, the bastion instance is retired into an SSH service on the shared host pool; no bastion replacement override is needed. Public clusters receive fixed Elastic IPs on a dedicated NLB, and SSH host keys persist in Secrets Manager. The cutover changes the address and fingerprint once; later task replacements preserve both. Private clusters use an internal NLB. Existing SSH sessions must reconnect after task replacement. With containers disabled, AMI and instance-type moves still replace the host bastion and require `--allow-replacement bastionhostinstance`. The scheduler's old periodic-check interval
is copied to the reconciler interval only if the latter is absent. Conflicts are reported and
preserved, and the old key remains for older running code. Existing lists keep their custom values.
Before success, settings and deployed module versions are read back. Rows a stack published before
the upgrade must still exist unless the deployed template no longer names them in its settings
resource (the metrics stack drops its CloudWatch dashboard once the provider is DogStatsD), which the
read-back reports and accepts. This includes the retired bastion instance ID, private IP and instance-profile ARN; its module ID and stack remain in place. A failed verification requires repair and a rerun; it does not reopen
submission.

### values.yml Restore and Save

The first phase reads `~/.idea/clusters/<cluster-name>/<aws-region>/values.yml` to set the new Base
OS. If that file is missing locally, the command downloads `values/values.yml` from the cluster S3
bucket, writes it to that path and continues, so an upgrade can run from a workstation that never
held the original file. If the S3 object is missing, the underlying S3 error stops the command;
it does not provide a diagnostic naming both paths. Restore a known-good copy to the local path
and run `./idea-admin.sh config save-values --cluster-name <CLUSTER_NAME> --aws-region <REGION>`
before retrying. When both copies exist, a raw-text difference produces a generic warning, not a
key-by-key comparison; the local copy wins.

For the first container migration, download and edit the local file before starting, as described
in [Move to containers](move-to-containers.md). The early trunking and scheduler-cutover gates do
not wait for S3 restoration.

After every module deploys successfully, the command uploads the local `values.yml` back to
`values/values.yml` in the cluster bucket. An upload failure produces a warning with a
`config save-values` recovery command. Save the local file before using another workstation,
which could otherwise restore stale values. A successful verified deployment still restores
the saved maintenance baseline.

### Compute Node Image

The upgrade moves compute nodes onto the release's AMI for the cluster's Base OS, unless
each scheduler module's `compute_node_ami` names an image built from **Administration → Images and applications → Custom images** that is newer than the
release image, which is kept and reported. An older built image is replaced, and can be rebuilt
from **Custom images** after the upgrade.

### Module Host Instance Type

New clusters run the module hosts on `m7i.large`, which replaces the `m6i.large` default. It is
offered in all 28 of the 29 regions in `region_ami_config.yml` that can be checked, which is why it
is preferred over the newer `m8i.large`; the twenty-ninth, me-south-1, is an opt-in region that
could not be queried. An upgrade moves a host whose stored instance type is still `m6i.large` onto
the new type, after one check that the region offers it, and leaves any other stored value alone
regardless of whether you explicitly chose `m6i.large` or inherited it as a default. The setting is what the launch template renders, so a moved host
runs the new type when its instance is next replaced rather than during the upgrade.

If you install into a region that does not offer `m7i.large`, pick an instance type that region does
offer, such as `m6i.large`, and the upgrade keeps `m6i.large` only while `m7i.large` is unavailable (or its availability cannot be read).

### Analytics Data Node Instance Type

New clusters run the analytics OpenSearch data nodes on `m7g.large.search`, which has the same 2
vCPU and 8 GiB as the `m5.large.search` it replaces. An upgrade moves a cluster whose
`analytics.opensearch.data_node_instance_type` is still `m5.large.search` onto the new type, after
checking that the region offers it for the engine version the domain runs. When the region does not
offer it, the setting is left alone and the upgrade prints why. Any other stored value is a type you
chose and is kept, so an upgrade never resizes a domain you tuned yourself.

The instance type is part of the domain cluster configuration, so changing it updates the domain in
place rather than replacing it. OpenSearch Service applies the change as a blue/green deployment:
it brings up the new nodes, migrates the shards and retires the old nodes. This typically takes tens
of minutes and the domain stays available throughout, with no downtime and no data loss.

### Rolling Service Updates

For container deployments, the cluster manager, virtual desktop controller, DCV broker,
connection gateway and SSH bastion retain their desired healthy task count while replacement
tasks pass health checks. With the default task count, each service can add one replacement
task at a time. Running jobs and desktops remain on their existing compute hosts.

The scheduler uses exactly one task. Its old task stops before the replacement starts against
persistent scheduler state, leaving a gap in scheduler API availability and submissions. The
length of that gap depends on startup and readiness checks; in release testing it was about a minute
and a half. Jobs already running on compute nodes continue, and the portal retries scheduler requests. An SSH connection through a
replaced bastion task must reconnect after draining; it uses the same address and host key.

### Borrowed Hosts

While services roll, the container host group may grow one host past its minimum. Nothing moves
tasks off a host on its own, so the upgrade ends by draining the newest extra host, waiting for
its tasks to move, and shrinking the group with that host unprotected. Pass `--keep-borrowed-hosts`
to leave the extra host in service; run `return-hosts` later to give it back.

### DCV Broker Table Billing Mode

The DCV broker creates its own DynamoDB tables at boot with a fixed provisioned capacity of five
read and five write units per table. Those tables hold broker state such as key pairs, health
checks and pending session requests, and their measured traffic is a small fraction of one unit.
After the upgrade the virtual-desktop-controller moves each of them to on demand billing when it
starts, and again whenever a broker instance reports that its boot completed, so the cluster pays
per request rather than for idle capacity. Both paths run because a rolling update can hand the
broker boot event to a controller task that is still draining on the previous release, which leaves
the tables provisioned. The read and write capacity autoscaling policies are not applied while on
demand billing is in use.

This is a billing mode change only. Table contents, keys, indexes and encryption are untouched, and
a table moved to on demand serves at least 4,000 write and 12,000 read units per second
immediately, far above what the broker uses. DynamoDB limits how often a table may change billing
mode; when that limit is reached the controller logs a warning and tries again at its next start or
the next broker boot instead of failing the upgrade.

To keep provisioned capacity and the autoscaling policies, write the stored module ID prefix,
`vdc` for the generated desktop module. Configuration readers translate the logical name
`virtual-desktop-controller`, but `config set` writes the key literally; use `list-modules` to
check a customized module ID.

```bash
./idea-admin.sh config set --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  'Key=vdc.dcv_broker.dynamodb_table.on_demand,Type=bool,Value=false'
```

### Before You Start

Turn the maintenance banner on before you close the scheduler, and off after you have verified the
upgraded cluster. While it is on, every portal page carries your message and new job submissions are
refused with it instead of a generic failure. Neither change needs a redeploy.

```bash
./idea-admin.sh config \
  set "Key=cluster-manager.maintenance.enabled,Type=bool,Value=true" \
      "Key=cluster-manager.maintenance.message,Type=string,Value=HPC scheduler is closed for a cluster upgrade." \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
```

The same three settings are editable from Administration, then Settings, then Maintenance
notice. See [Maintenance Banner](../../../modules/cluster-manager/maintenance-banner.md) for the
optional end time and for what the banner does not cover.

### Usage

The basic syntax for the upgrade command is:

```bash
./idea-admin.sh upgrade-cluster [OPTIONS] [MODULES...]
```

If no modules are specified, all modules will be upgraded automatically.

#### Required Parameters

* `--cluster-name`: Name of your IDEA cluster
* `--aws-region`: AWS region where the cluster is deployed

#### Optional Parameters

* `MODULES`: List of modules to upgrade (e.g., `cluster`, `metrics`, `scheduler`, etc.). If not specified, all modules will be upgraded.
* `--base-os`: Base OS to upgrade to. If not specified, the cluster keeps the Base OS it already runs, read from its cluster settings and printed before the upgrade starts. Pass this option only to change the Base OS. Supported options are:
  * `amazonlinux2023`
  * `rhel8`
  * `rhel9`
  * `rhel10`: refused if virtual-desktop-controller is deployed, including scoped upgrades
  * `rocky8`
  * `rocky9`
  * `rocky10`: refused if virtual-desktop-controller is deployed, including scoped upgrades
* `--aws-profile`: AWS profile to use for the operation
* `--termination-protection`: Set CloudFormation stack termination protection (default: true)
* `--force-build-bootstrap`: Re-build bootstrap package even if directory exists
* `--rollback/--no-rollback`: Enable/disable stack rollback on failure (default: true)
* `--optimize-deployment`: Deploy applicable stacks in parallel to speed up the process
* `--force`: Skip phase confirmation prompts; differing overwritten rows still require explicit drift acceptance
* `--accept-config-drift`: Accept overwriting rows that differ from generated configuration, after reviewing `./idea-admin.sh config preview-upgrade`; separate from `--force`
* `--skip-global-settings-update`: Skip the global settings update if you've already done it
* `--module-set`: Name of the module set to use (default: default)
* `--deployment-id`: UUID to identify the deployment
* `--disable-eol-stacks-in-use`: Disable, rather than delete, end-of-life virtual desktop software stacks that a live session still uses
* `--drain`: Before the scheduler moves from a host to a container, close job submission and wait for the host scheduler to finish every job it holds
* `--drain-timeout-minutes`: How long `--drain` waits before stopping with submission still closed (default: 240)
* `--skip-drain-check`: Skip the host scheduler's job inventory check but close submission for the whole run; any job it still holds is lost

When the scheduler is part of the upgrade and ECS will be enabled at synthesis, a scheduler stack
that still has an EC2 host needs a cutover. ECS can be enabled by `enable_ecs: true` in `values.yml`,
or by an ECS module row with `ecs.enabled: true` in settings. This includes a scheduler-only run
after ECS capacity has already deployed. The order is maintenance writes, inventory/drain, OS and
end-of-life validation, configuration preview and applicable confirmations, then Phase 0 DNS retention
and the upgrade phases. DNS retention prevents host removal from deleting the container scheduler's name.

The container scheduler starts with an empty job database, so a job the host still holds is lost.
The upgrade closes job submission before reading the host scheduler's inventory over Systems
Manager, even if the queue is empty. Submission remains closed throughout the upgrade. A non-empty
queue without `--drain` restores the previous maintenance state and refuses deployment. Pass
`--drain` to wait for the queue to empty. The maintenance flag is honoured by both the portal and
`qsub`. `--skip-drain-check` skips the inventory read but still closes submission for the run.

The original maintenance enabled flag and message are saved as JSON in
`cluster-manager.maintenance.upgrade_baseline` before submission closes. A failed run leaves
submission closed and preserves that baseline. Re-run the upgrade to completion to restore both
original values and delete the baseline after the final values upload. This also works after the
scheduler host is gone, and preserves maintenance that was already enabled before the upgrade.
A refused retry keeps an earlier failed run's closure and baseline until a run completes.

ECS module-set rows are held until cluster-manager's modules-table row records the target release
as deployed. Deploying the ECS stack alone does not make the old portal recognize ECS. A retry
after a cluster-manager deployment failure continues holding those rows, and a scoped run that
does not deploy cluster-manager does not publish them.

Job ids start again from zero on the container scheduler, as they did after every scheduler host
replacement before. From then on the job database lives on the scheduler's file system and
survives later upgrades. Once the scheduler host is gone, upgrades skip the inventory gate and DNS
retention step, but a successful retry still restores any saved maintenance baseline.

The end-of-life check itself changes no software stacks and runs before its confirmation, but
the cutover gate may already have written maintenance settings. The check lists the
software stacks it will delete or disable, prefixed with `will delete` or `will disable`. Those
changes are applied only after you confirm the upgrade, or immediately when you pass `--force`.

By default the upgrade stops before changing software stacks when a virtual desktop session still runs on
a software stack whose base OS has reached end-of-life, and lists the sessions that block it.
Passing `--disable-eol-stacks-in-use` sets `enabled` to false on each of those stacks instead and
continues. The stack record is kept, so running desktops are unaffected, but no new session can be
launched from a disabled stack. End-of-life stacks that no live session uses are still deleted, and
end-of-life references in cluster settings and HPC queue profiles remain a hard stop that this flag
does not change.

A stack is disabled in DynamoDB, while the portal lists software stacks from the search index, so it
keeps reading as enabled until the index catches up. The virtual-desktop-controller redeploy later in
the same upgrade reconciles the index at startup. To reindex sooner, run
`ideactl reindex-software-stacks --reset` on a host deployment. For containers, use ECS Exec in the VDC application container to invoke its application CLI.

### Examples

#### Full Upgrade Keeping the Current Base OS

Without `--base-os` the upgrade keeps the Base OS the cluster already runs. It reads that value from
the cluster settings, prints it, and refuses to start if the settings hold no Base OS or more than
one, because guessing would redeploy every module onto an OS nobody asked for. That matters most
with `--force`, which accepts phase prompts but does not accept configuration drift. Passing `--base-os` changes the
Base OS and prints what it is changing from.

The simplest way to upgrade all infrastructure components:

```bash
./idea-admin.sh upgrade-cluster \
  --aws-region us-east-2 \
  --cluster-name idea-test1 \
  --aws-profile default
```

#### Unattended Upgrade After Drift Review

Review `./idea-admin.sh config preview-upgrade --cluster-name <CLUSTER_NAME> --aws-region <REGION>` first.
Only after accepting its listed overwrites, run:

```bash
IDEA_ADMIN_NO_TTY=true ./idea-admin.sh upgrade-cluster \
  --cluster-name <CLUSTER_NAME> --aws-region <REGION> --force --accept-config-drift
```

With `--force` alone, differing overwritten rows stop the upgrade even if the cutover gate has
already closed submission. Reconcile those rows or explicitly accept the reviewed drift and retry.

#### Full Upgrade with Explicit Base OS

Move the cluster to a different Base OS:

```bash
./idea-admin.sh upgrade-cluster --base-os amazonlinux2023 \
  --aws-region us-east-2 \
  --cluster-name idea-test1 \
  --aws-profile default
```

#### Upgrade Only Specific Modules

To upgrade only the scheduler and cluster-manager components:

```bash
./idea-admin.sh upgrade-cluster scheduler cluster-manager \
  --base-os amazonlinux2023 \
  --aws-region us-east-2 \
  --cluster-name idea-test1 \
  --aws-profile default
```

#### Skip Global Settings Update

If you've already updated global settings and want to skip that step:

```bash
./idea-admin.sh upgrade-cluster --skip-global-settings-update \
  --aws-region us-east-2 \
  --cluster-name idea-test1 \
  --aws-profile default
```

#### Optimize for Speed (experimental)

Use parallel deployment where possible:

```bash
./idea-admin.sh upgrade-cluster --base-os amazonlinux2023 \
  --optimize-deployment \
  --aws-region us-east-2 \
  --cluster-name idea-test1 \
  --aws-profile default
```

### ECR Credentials Reset

Before launching the container, `idea-admin.sh` resets its public ECR credentials, which needs
`dig`. A stock Amazon Linux 2023 host does not ship `dig`; when it is missing the reset is skipped
with a warning and the command continues. Install `bind-utils` to restore the reset, or set
`IDEA_ECR_CREDS_RESET=false` to skip it without the warning.

### Troubleshooting

If the upgrade fails during the pre-upgrade configuration stage:

1. Make sure your values.yml file correctly reflects your desired configuration
2. Verify AMI IDs are available in your target region

If the upgrade fails during deployment:

1. Check the CloudFormation console for error details
2. Fix any issues and retry with the same command
3. Use `--no-rollback` to prevent stack rollback for easier debugging
