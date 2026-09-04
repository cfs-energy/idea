# Full IDEA Upgrade (idea-admin.sh upgrade-cluster)

## Full Cluster Upgrade

### Overview

The `upgrade-cluster` command combines multiple steps that were previously separate into a single operation:

1. Updating the base OS configuration
2. Updating infrastructure AMIs
3. Backing up and regenerating global settings
4. Deploying all modules with the `--upgrade` flag

### values.yml Restore and Save

The first phase reads `~/.idea/clusters/<cluster-name>/<aws-region>/values.yml` to set the new Base
OS. If that file is missing locally, the command downloads `values/values.yml` from the cluster S3
bucket, writes it to that path and continues, so an upgrade can run from a workstation that never
held the original file. If the bucket has no copy either, the command stops and names both the local
path and the S3 URI it checked; upload an existing copy with `idea-admin.sh config save-values`
before retrying. When both copies exist and differ, the command reports which keys differ and uses
the local copy.

After every module deploys successfully, the command uploads the local `values.yml` back to
`values/values.yml` in the cluster bucket so the next upgrade can restore it. A failed upload logs a
warning naming the `idea-admin.sh config save-values` command to run and does not fail the upgrade,
because static STS credentials often expire before the last phase of a long run finishes.

### Compute Node Image

The upgrade moves compute nodes onto the release's AMI for the cluster's Base OS, unless
`scheduler.compute_node_ami` names an image built from the Custom AMIs page that is newer than the
release image, which is kept and reported. An older built image is replaced, and can be rebuilt
from Custom AMIs after the upgrade.

### Module Host Instance Type

New clusters run the module hosts on `m7i.large`, which replaces the `m6i.large` default. It is
offered in all 28 of the 29 regions in `region_ami_config.yml` that can be checked, which is why it
is preferred over the newer `m8i.large`; the twenty-ninth, me-south-1, is an opt-in region that
could not be queried. An upgrade moves a host whose stored instance type is still `m6i.large` onto
the new type, after one check that the region offers it, and leaves any other stored value alone
because it is a type you chose. The setting is what the launch template renders, so a moved host
runs the new type when its instance is next replaced rather than during the upgrade.

If you install into a region that does not offer `m7i.large`, pick an instance type that region does
offer, such as `m6i.large`, and the upgrade leaves that cluster on the type you chose.

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

Set `virtual-desktop-controller.dcv_broker.dynamodb_table.on_demand` to false to keep provisioned
capacity and the autoscaling policies.

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

The same three settings are editable from Cluster Management, then Settings, then the Maintenance
tab. See [Maintenance Banner](../../../modules/cluster-manager/maintenance-banner.md) for the
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
  * `rhel10`
  * `rocky8`
  * `rocky9`
  * `rocky10`
* `--aws-profile`: AWS profile to use for the operation
* `--termination-protection`: Set CloudFormation stack termination protection (default: true)
* `--force-build-bootstrap`: Re-build bootstrap package even if directory exists
* `--rollback/--no-rollback`: Enable/disable stack rollback on failure (default: true)
* `--optimize-deployment`: Deploy applicable stacks in parallel to speed up the process
* `--force`: Skip all confirmation prompts
* `--skip-global-settings-update`: Skip the global settings update if you've already done it
* `--module-set`: Name of the module set to use (default: default)
* `--deployment-id`: UUID to identify the deployment
* `--disable-eol-stacks-in-use`: Disable, rather than delete, end-of-life virtual desktop software stacks that a live session still uses

The end-of-life check runs before the upgrade is confirmed and changes nothing: it lists the
software stacks it will delete or disable, prefixed with `will delete` or `will disable`. Those
changes are applied only after you confirm the upgrade, or immediately when you pass `--force`.

By default the upgrade stops before making any change when a virtual desktop session still runs on
a software stack whose base OS has reached end-of-life, and lists the sessions that block it.
Passing `--disable-eol-stacks-in-use` sets `enabled` to false on each of those stacks instead and
continues. The stack record is kept, so running desktops are unaffected, but no new session can be
launched from a disabled stack. End-of-life stacks that no live session uses are still deleted, and
end-of-life references in cluster settings and HPC queue profiles remain a hard stop that this flag
does not change.

A stack is disabled in DynamoDB, while the portal lists software stacks from the search index, so it
keeps reading as enabled until the index catches up. The virtual-desktop-controller redeploy later in
the same upgrade reconciles the index at startup. To reindex sooner, run
`ideactl reindex-software-stacks --reset` on the virtual-desktop-controller host.

### Examples

#### Full Upgrade Keeping the Current Base OS

Without `--base-os` the upgrade keeps the Base OS the cluster already runs. It reads that value from
the cluster settings, prints it, and refuses to start if the settings hold no Base OS or more than
one, because guessing would redeploy every module onto an OS nobody asked for. That matters most
with `--force`, which answers every confirmation prompt for you. Passing `--base-os` changes the
Base OS and prints what it is changing from.

The simplest way to upgrade all infrastructure components:

```bash
./idea-admin.sh upgrade-cluster \
  --aws-region us-east-2 \
  --cluster-name idea-test1 \
  --aws-profile default
```

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
