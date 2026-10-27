# Full IDEA Upgrade (idea-admin.sh upgrade-cluster)

The control plane runs only on ECS. Every deployed module must be at **26.10.0 or newer**. Older clusters must first use the 26.10.9 administrator and complete its ECS migration before using this release. See [ECS control-plane operations](ecs-control-plane.md).

### Recovery with the 26.10.9 administrator

From a checkout containing `idea-admin.sh`, replace the placeholders and run:

```bash
IDEA_REVISION=v26.10.9 ./idea-admin.sh upgrade-cluster --cluster-name <name> --aws-region <region>
```

`IDEA_REVISION` selects the published 26.10.9 administrator container. Complete that release's
upgrade and ECS migration before retrying 26.11.1. For older releases, follow each intervening
release's Upgrade Instructions. If an upgrade is unfinished, the refusal lists the leftover
`<module>.maintenance.upgrade_baseline` or `cluster.upgrade_historical_pending` keys; use the
26.10.9 administrator to recover that upgrade rather than deleting its recovery state.

## Full Cluster Upgrade

### Overview

The `upgrade-cluster` command combines multiple steps that were previously separate into a single operation:

1. Updating the base OS configuration
2. Updating infrastructure AMIs
3. Backing up and regenerating global settings
4. Deploying all modules with the `--upgrade` flag

### Upgrade floor: 26.10.0

The command checks every deployed module's release before making changes. A missing, unreadable, or older release stops the run with the affected modules and the required administrator version. `--force` cannot bypass this check. Host control-plane deployment and host-to-ECS migration are no longer available.

After deployment, settings and module versions are read back before success. A failed verification requires repair and a rerun.

### values.yml Restore and Save

The first phase reads `~/.idea/clusters/<cluster-name>/<aws-region>/values.yml` to set the new Base
OS. If that file is missing locally, the command downloads `values/values.yml` from the cluster S3
bucket, writes it to that path and continues, so an upgrade can run from a workstation that never
held the original file. If the S3 object is missing, the underlying S3 error stops the command;
it does not provide a diagnostic naming both paths. Restore a known-good copy to the local path
and run `./idea-admin.sh config save-values --cluster-name <CLUSTER_NAME> --aws-region <REGION>`
before retrying. When both copies exist, a raw-text difference produces a generic warning, not a
key-by-key comparison; the local copy wins.

After every module deploys successfully, the command uploads the local `values.yml` back to
`values/values.yml` in the cluster bucket. An upload failure produces a warning with a
`config save-values` recovery command. Save the local file before using another workstation,
which could otherwise restore stale values.

### Compute Node Image

The upgrade moves `scheduler.compute_node_ami` to the release's AMI for the cluster's Base OS when it still names a vendor image. It keeps the current image, and reports it, when the image is one IDEA built (`idea-compute-node-*`), when `scheduler.images.default_image_pinned` is `true`, or when the image cannot be described. A kept built image is replaced only when the compute image pipeline switches to a newly validated image.

### Images After the Upgrade

After the upgrade, every managed desktop and compute image is rebuilt from the newest vendor image and validated, and new desktops and jobs switch to an image only after it passes every check. This runs in the background and does not hold up the upgrade. A bake takes about 45 minutes, four at a time by default. Check **Administration → Images and applications → Images** later and act on any **Failed** row; new launches stay on the previous image until the row succeeds. Pinned rows and pinned targets are skipped, and on 26.10.1 a base software stack that launches from an image an administrator set by hand is pinned before anything switches. See [Images](../../../modules/hpc-workloads/admin-documentation/custom-amis.md).

#### One-time builder image cleanup (upgrading to 26.10.1)

Cleanup removes only builder images that carry the cluster's tag. Desktop and compute builder images made before 26.10.1 have no cluster tag, so they stay until you remove them. After this upgrade, review the account's builder images (names starting `idea-dcv-host-` and `idea-compute-node-`) and deregister the ones nothing uses, with their snapshots. Keep any image that a software stack, queue profile, launch template or `scheduler.compute_node_ami` still names, and if other clusters share the account, any image they use. You only need to do this once.

### Container Host Instance Type

Clusters run control-plane tasks on the shared container host pool. The default is `m7g.xlarge`, configured by `ecs.hosts.instance_type`. Confirm that the type is available in every selected Availability Zone before installing a cluster. Recheck capacity after changing task sizes, task counts, or the metrics agent.

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

If an OpenSearch service software update is available or pending, the upgrade keeps the current data-node type and prints the update that must finish first. Apply that service update, wait for it to complete, then run `upgrade-cluster` again to apply the instance-type change.

### Pre-deploy protection and stack cleanup

Before deployment, the change-set check refuses removal of a VPC, subnet, internet or NAT gateway, route, route table, or Elastic IP. Treat the refusal as a network-preservation stop: review why the template no longer contains the resource before allowing any change.

Stack cleanup can continue after a failed or rolled-back deployment. `upgrade-cluster` waits up to four hours for cleanup to finish before continuing or reporting the timeout. A stack whose only change is its cluster-settings resource is still updated: that resource records the module version and publishes the module's settings.

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

Turn maintenance on before draining the scheduler, and off after you have verified the upgraded
cluster. Every portal page carries your message. Portal and `qsub` submissions from non-admin users are refused
with "Cluster is in maintenance: <message>", plus the end time in UTC when `ends_at` is set.
Root and cluster administrators remain exempt. Running and already-queued jobs are untouched
and can finish draining. Use this flag instead of manually disabling queues with qmgr. Clearing
the flag restores submission after config propagation (about half a minute), without a restart
or redeploy.

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
* `--optimize-deployment/--no-optimize-deployment`: Deploy the stacks of one priority group in parallel, for example the scheduler beside the desktop stack (default: on)
* `--force`: Skip phase confirmation prompts; overwritten rows with no source marker that differ from generated configuration still require drift acceptance
* `--accept-config-drift`: Accept overwriting differing rows with no source marker after reviewing `./idea-admin.sh config preview-upgrade`; release image moves, template default updates and rows written from the portal or the command line (which keep their values) do not need this flag
* `--skip-global-settings-update`: Skip the global settings update if you've already done it
* `--module-set`: Name of the module set to use (default: default)
* `--deployment-id`: UUID to identify the deployment
* `--disable-eol-stacks-in-use`: Disable, rather than delete, end-of-life virtual desktop software stacks that a live session still uses
* `--refresh-hosts`: Replace the container hosts when any runs an older launch template (instance refresh of the whole group, new hosts first, managed draining moves the tasks) after the ecs stack deploys. Without it the upgrade stops right after the ecs stack when such a host has not joined the directory, and names the hosts

The end-of-life check changes no software stacks before confirmation. It lists stacks it will delete or disable. Changes apply only after confirmation, or when `--force` is supplied.

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
ECS Exec in the VDC application container and run
`idea-vdc-cli reindex-software-stacks --reset`. The shared image also provides
`idea-cluster-manager-cli` and `idea-scheduler-cli` for their respective modules; these commands
are independent of package installation order.

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

#### Release Images and Template Defaults

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
selected reference so later upgrades can advance it. `ecs.previous_release_image` preserves
ownership of the prior digest while the new marker is written before `ecs.image`, so an
interruption between writes remains recoverable. An operator-set digest that differs from
both markers stays pinned. Digest resolution is required even with `--skip-release-image-check`
on this public fallback path. This protects task replacements from a release tag being moved.

When `ecs.image` names this partition's release repository at an older release tag, the upgrade
moves it to the tool's release tag and prints the move. It does not count that move as drift or
require a manual image setting. Images outside the configured repository, operator-set digests, build tags and newer tags stay
unchanged and remain visible in the preview.

Global settings last written by the settings sync have `source=template`. Changed defaults are
listed as "Template defaults updated" and rewritten without drift acceptance. The add-only sync
still preserves existing non-global settings. CLI edits stamp `source=cli`, API edits stamp
`source=api`, and stack writes use `source=stack`. Rows marked `cli` or `api` are the operator's:
the upgrade keeps their values and markers and lists them as "Operator values kept". Differing
rows with no marker (or a `stack` marker) that the upgrade would overwrite still require
confirmation or `--accept-config-drift`; `--force` alone does not accept them.

The 26.09.4 sync did not set a source marker: new rows had none, and existing markers were retained.
The first upgrade therefore still asks about differing legacy defaults, since their last writer
cannot be established. Review and accept those rows once. The sync then stamps its writes as
`template`, so subsequent template default changes proceed without drift acceptance. Existing
`stack` markers are not treated as proof that a row was never edited.

#### Unattended Upgrade After Drift Review

Review `./idea-admin.sh config preview-upgrade --cluster-name <CLUSTER_NAME> --aws-region <REGION>` first.
Only after accepting its listed overwrites, run:

```bash
IDEA_ADMIN_NO_TTY=true ./idea-admin.sh upgrade-cluster \
  --cluster-name <CLUSTER_NAME> --aws-region <REGION> --force --accept-config-drift
```

With `--force` alone, differing overwritten operator or unknown-source rows stop the upgrade
before settings changes. Reconcile those rows or explicitly accept the reviewed drift and retry.

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

#### Deploy One Stack at a Time

Stacks of one priority group deploy in parallel by default. To deploy them one after another:

```bash
./idea-admin.sh upgrade-cluster --base-os amazonlinux2023 \
  --no-optimize-deployment \
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

### Bastion SSH source addresses

`ssh_client_ip` in values.yml is the source of truth for `cluster.network.ssh_client_ip`.
Each upgrade replaces the stored list, including removing addresses. An empty list or an
omitted key restores the default cluster and operator prefix lists for SSH. The upgrade
preview labels changed lists as `VALUES_UPDATE` and shows the old and new addresses. Replacing a differing stored list requires drift confirmation (or `--accept-config-drift` for unattended runs). Both IP settings must be lists of IPv4 addresses or network CIDRs without host bits; addresses normalize to `/32` and duplicates are removed. `ideactl config set` can change the setting
immediately, but the next upgrade restores values.yml; edit that file for a lasting change.
