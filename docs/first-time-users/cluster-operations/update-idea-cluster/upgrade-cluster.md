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
