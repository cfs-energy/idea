# Full IDEA Upgrade (idea-admin.sh upgrade-cluster)

The control plane runs only on ECS. Every deployed module must be at **26.10.0 or newer**. Older clusters must first use the 26.10.9 administrator and complete its ECS migration before using this release. See [ECS control-plane operations](ecs-control-plane.md).

### Recovery with the 26.10.9 administrator

From a checkout containing `idea-admin.sh`, replace the placeholders and run:

```bash
IDEA_REVISION=v26.10.9 ./idea-admin.sh upgrade-cluster --cluster-name <name> --aws-region <region>
```

`IDEA_REVISION` selects the published 26.10.9 administrator container; 26.10.9 and later releases
record their image in `candidate.json`. For an older revision set `IDEA_IMAGE_DIGEST` to the digest
of that release's image as well (see [mirrored registries and offline hosts](#mirrored-registries-and-offline-hosts)). Complete that release's
upgrade and ECS migration before retrying 26.11.2. Clusters on 25.11.0 or newer upgrade to 26.10.9
directly; only clusters older than 25.11.0 follow each intervening release's Upgrade Instructions
first. If an upgrade is unfinished, the refusal lists the leftover
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
Root and cluster administrators remain exempt. The configured image-pipeline validation user
(default `idea-validate`) is also exempt so post-upgrade image canaries can run; see
[maintenance admission](../../../modules/cluster-manager/maintenance-banner.md). Running and already-queued jobs are untouched
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

### Before You Upgrade

Run these checks on each cluster before upgrading to 26.11.2. Every command only reads. They are ordered by what can stop new hosts or hold jobs first. Replace `<CLUSTER_NAME>` and `<REGION>`; table names use the module ids of a default cluster (`scheduler`, `vdc`).

1. **Download locations.** New hosts check what they download as root. A location changed from the shipped default needs its pin.

   ```bash
   ./idea-admin.sh config show --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
     --query 'package_config\.(prometheus|efs_utils|amazon_cloudwatch_agent|openpbs)'
   ```

   Bad result, any of:
   - A Prometheus URL other than release 2.37.0, 2.53.4 or 2.53.5, or a node_exporter URL other than 1.3.1, 1.9.1 or 1.12.1, with `x86_64_sha256` or `aarch64_sha256` empty beside it.
   - `efs_utils.version` other than `v3.3.0` with `efs_utils.commit` empty.
   - A CloudWatch agent `download_link` or `download_link_pattern` whose package has no `.sig` file next to it that hosts can reach, with `amazon_cloudwatch_agent.sha256` empty. On GovCloud, check from a host that the package URL plus `.sig` answers.
   - An `openpbs.commit` that does not exist in `openpbs.repo_url`. A commit hash builds exactly that commit. A branch or tag name builds whatever it names when each host bootstraps, and the bootstrap log says so with the commit it built; set the setting to that commit to pin the build.

   What happens: new compute nodes, desktops and image bakes stop with `idea:BootstrapStatus=download-unverified` (or `download-failed` when the location cannot be reached; the defaults for efs-utils and OpenPBS are on github.com). What to do: set the missing value with `config set` (the archive's SHA-256, the commit the efs-utils tag names, one SHA-256 per CloudWatch package your hosts resolve, separated by spaces or commas), or return the location to the default. Read the values back after the upgrade.

2. **Shared storage scopes and mount values.**

   ```bash
   ./idea-admin.sh config show --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
     --query '^shared-storage\..*\.(provider|scope|modules|projects|queue_profiles|mount_dir|mount_options|dns|mount_name)$'
   ```

   Bad result, any of:
   - **A share whose `scope` is `module` with a `modules` list.** Each control-plane task now mounts it only when the list names that task's own module: `bastion-host` (login node), `cluster-manager` (portal file browser and storage usage), `scheduler`, `virtual-desktop-controller` (controller and broker). On 26.11.1 all of them mounted it when the list named any one of the four. A share with `modules: [scheduler]` therefore disappears from the login node and the portal at upgrade. No data is lost. If the login node or the portal must keep the mount, add `bastion-host` or `cluster-manager` to the share's `modules` before upgrading. A share with an empty `modules` list, an empty scope or scope `cluster` is unchanged; a share scoped by `project` or queue profile was never mounted in a task.
   - **A share whose `scope` lists two or more of `module`, `project` and `scheduler:queue-profile`.** Every listed scope must now match. `[module, scheduler:queue-profile]` and `[module, project, scheduler:queue-profile]` no longer mount on desktops, and mount on compute nodes only for the listed `queue_profiles` (an empty list means all). `[module, project]` and `[project, scheduler:queue-profile]` are unchanged. Confirm the narrower result is wanted, or drop a scope.
   - **A `mount_dir`, endpoint or `mount_options` of an FSx share with characters outside letters, digits and `_.,:=@/+-`, or options without the four fstab fields** (type, options, dump, pass). The container stack deploy stops and names the setting, after earlier stacks have been upgraded. Edit the value first. Other stored mount values the new rules refuse stay in service and are logged as errors at cluster manager start.
   - **`shared-storage.apps.provider` or `shared-storage.data.provider` of `fsx_lustre` with no `fsx_lustre.dns` and `fsx_lustre.mount_name`.** The deploy or upgrade is refused before it changes anything. Point that storage at an EFS file system. FSx for Lustre stays available for job scratch.

   Also new: a host stops its bootstrap when a share in its scope does not mount, where it used to carry on. Check that every share listed still exists and is reachable from the subnets hosts launch in. A new or moved FSx share needs `--refresh-hosts` before the bastion, cluster manager, scheduler or desktop controller deploy.

3. **Site hook checksums.**

   ```bash
   ./idea-admin.sh config show --cluster-name <CLUSTER_NAME> --aws-region <REGION> --query 'site_hook_sha256$'
   ```

   Expected: every value empty (nothing changes at upgrade), or equal to `sha256sum <hook file>` and stored under the module's id (`vdc.…` on a default cluster, not `virtual-desktop-controller.…`). Bad result: a value that differs from the file. New compute nodes, desktops and image bakes then stop with `site-hook-mismatch`, and the login node skips its hook. Set the value again, or clear it. See [Site Hook Checksums](#site-hook-checksums).

4. **Directory options.**

   ```bash
   ./idea-admin.sh config show --cluster-name <CLUSTER_NAME> --aws-region <REGION> --query '^directoryservice\.ldap_options$'
   ```

   Expected: no row, an empty list, or entries that are each a plain dictionary literal with an integer `code` and a `value`, such as `{'name': 'OPT_REFERRALS', 'code': 8, 'value': 0}`. Bad result: an entry holding anything else, such as a function call or a name. The cluster manager stops at start on such an entry. Rewrite it as a literal before upgrading.

5. **Desktop controller role.** The role gains one managed policy of IDEA's own (three in all), next to `cluster.iam.ec2_managed_policy_arns`. IAM allows 10 by default.

   ```bash
   aws iam list-roles --query "Roles[?contains(RoleName, '<CLUSTER_NAME>-vdc-controller-task-role')].RoleName" --output text
   aws iam list-attached-role-policies --role-name <ROLE_NAME> --query 'length(AttachedPolicies)'
   ```

   Bad result: 10 or more. The controller stack update fails. Remove an operator policy from `cluster.iam.ec2_managed_policy_arns` or raise the account's quota first.

6. **Queue limits that never held.** `max_provisioned_instances` now counts every instance of a queue profile, and batch profiles hold a whole batch to it and to `max_running_jobs`.

   ```bash
   aws dynamodb scan --table-name <CLUSTER_NAME>.scheduler.queue-profiles --region <REGION> \
     --filter-expression "max_provisioned_instances > :z OR max_running_jobs > :z" \
     --expression-attribute-values '{":z":{"N":"0"}}' \
     --projection-expression "#n, queues, max_provisioned_instances, max_running_jobs" \
     --expression-attribute-names '{"#n":"name"}'
   ```

   Bad result: a limit below what the queue really runs today. Jobs past it wait after the upgrade. Raise the value, or set it to 0 for no limit. No built-in profile sets either.

7. **Queue defaults that start to apply.**

   ```bash
   aws dynamodb scan --table-name <CLUSTER_NAME>.scheduler.queue-profiles --region <REGION> \
     --filter-expression "attribute_exists(param_fsx_lustre_existing_fsx) OR attribute_exists(param_walltime) OR attribute_exists(param_keep_ebs_volumes)" \
     --projection-expression "#n, param_scratch_provider, param_fsx_lustre_enabled, param_fsx_lustre_existing_fsx, param_walltime, param_keep_ebs_volumes" \
     --expression-attribute-names '{"#n":"name"}'
   ```

   - A row with `param_fsx_lustre_existing_fsx`, with `param_fsx_lustre_enabled` not `false` and `param_scratch_provider` missing or `fsx-lustre-existing`: that file system is mounted on every new job of the queue that names none. Until now the value was stored and not used. Bad result: the file system no longer exists, is not meant for every job on the queue, or the value is not the file system's DNS name. A node that cannot mount it stops and its job is held; a value that is not a DNS name refuses the queue's jobs. Clear the value or correct it.
   - A row with `param_keep_ebs_volumes`: those jobs' EBS volumes are kept from now on. A row with `param_walltime` replaces the one-hour default in the job's cost and budget estimate. Only profiles written through the API carry either.

8. **License check commands.** They run as the `ideaserviceaccount` system account, which exists in the 26.11.2 image. On 26.11.1 they ran as the service's own identity.

   ```bash
   aws dynamodb scan --table-name <CLUSTER_NAME>.scheduler.license-resources --region <REGION> \
     --projection-expression "#n, availability_check_cmd" --expression-attribute-names '{"#n":"name"}'
   ```

   Bad result: a command whose script, tool or license file only root can read or run. A failed check counts as no free licenses, so licensed jobs wait with "Not enough … licenses". Make each file the command needs readable (and the script executable) by other accounts before upgrading; the command keeps the service's environment and `PATH`. After the upgrade, check each license resource's availability from the Licenses page under Administration > Manage jobs. Only cluster administrators can create, change or delete license resources from now on.

9. **Queues stored in two queue profiles, and keep-forever queues open to several projects.**

   ```bash
   aws dynamodb scan --table-name <CLUSTER_NAME>.scheduler.queue-profiles --region <REGION> \
     --projection-expression "#n,queues" --expression-attribute-names '{"#n":"name"}' --output json \
     | jq -r '[.Items[] | {n: .name.S, q: [.queues.L[]?.S]}] as $p
              | ($p | map(.q[]) | group_by(.) | map(select(length > 1) | .[0])) as $dup
              | $p[] | select([.q[] | IN($dup[])] | any) | "\(.n): \(.q | join(","))"'
   ```

   Bad result: any line. Each is a profile that shares a queue with another. Such profiles keep working and stay editable, but a missing built-in profile whose queues appear here is not re-created at start (the scheduler logs `is not re-created`), and adding a queue another profile lists is refused. Remove the queue from all but one profile when convenient.

   ```bash
   aws dynamodb scan --table-name <CLUSTER_NAME>.scheduler.queue-profiles --region <REGION> \
     --projection-expression "#n,keep_forever,project_ids" --expression-attribute-names '{"#n":"name"}' --output json \
     | jq -r '.Items[] | select(.keep_forever.BOOL == true and ((.project_ids.L // []) | length) > 1) | "\(.name.S): \([.project_ids.L[].S] | join(","))"'
   ```

   Bad result: any line. That keep-forever queue is open to more than one project, and after the upgrade its pool refuses jobs from a project whose instance profile differs from the pool's. Give those projects their own queue, or the same instance profile.

   The check also applies to pools built before the upgrade. For each keep-forever queue, read the instance profile recorded when its pool was built (the `# IDEA Job` comment block of the pool's stack template, `params.instance_profile`). `<STACK_UUID>` is the queue profile's `stack_uuid` in lower case with dashes and underscores removed:

   ```bash
   aws cloudformation get-template --region <REGION> \
     --stack-name <CLUSTER_NAME>-keepforever-<STACK_UUID> --query TemplateBody --output text | grep instance_profile
   ```

   Compare it with the profile the queue's jobs resolve to now: the project's own profile when it has one, else the queue's default instance profile, else the cluster's compute node profile. Bad result: they differ, because the pool was built without a project, before the project had its own profile, or before the queue default changed. The pool then refuses its queue's jobs after the upgrade. Rebuild the pool before or right after upgrading, then submit one job per project to the queue and confirm it is accepted.

10. **Job script templates.** Jinja2 templates render with a fixed set of filters, tests and methods.

    ```bash
    aws dynamodb scan --table-name <CLUSTER_NAME>.scheduler.applications --region <REGION> \
      --projection-expression "title, job_script_type, job_script_template" --output json \
      | jq -r '.Items[] | select(.job_script_type.S=="jinja2") | [.title.S, .job_script_template.S] | @tsv' \
      | grep -nE 'cycler\(|is callable|\|\s*(forceescape|random|urlize)\b|\.(map|filter|forEach|reduce|some|every|find)\(|\{%-?\s*(include|import|from|extends)\b|\.(push|pop|shift|unshift|sort|reverse|splice)\('
    ```

    Bad result: any line. That application's form fails to render its script after the upgrade. Rewrite the template with the `replace` filter, `concat`, or a list built with `{% set out = [] %}`. A hit on `push`, `pop` and the like fails only when called on a form value; copy the value first with `slice(0)`. See [job submission workflows](../../../modules/hpc-workloads/admin-documentation/create-web-based-job-submission-workflows.md).

11. **Disabled groups attached to projects.** A disabled group grants no project access, and project rows are recomputed from enabled groups at every cluster manager start. Members of a disabled group lose access to its projects; running jobs and desktops are not stopped, but a desktop whose owner lost the project cannot be started or connected to until an administrator moves it or the owner is added back. List the disabled groups, then find them in each project's `ldap_groups`:

    ```bash
    aws dynamodb scan --table-name <CLUSTER_NAME>.accounts.groups \
      --filter-expression "enabled = :f" --expression-attribute-values '{":f":{"BOOL":false}}' \
      --projection-expression group_name --region <REGION>
    aws dynamodb scan --table-name <CLUSTER_NAME>.projects \
      --projection-expression "#n, ldap_groups" --expression-attribute-names '{"#n":"name"}' --region <REGION>
    ```

    Bad result: a disabled group that people still rely on for a project. Enable it, or add them through another group, before upgrading.

12. **Accounts that are already disabled.** Their `~/.ssh/authorized_keys` is renamed to `authorized_keys.disabled` by a background pass that runs at every start (each account re-checked first, 20 s each, 10 min in all). Hand-added keys are included, and the file is renamed back when the account is enabled. A key put back by hand for a disabled account is set aside again at the next start. List the disabled accounts:

    ```bash
    aws dynamodb scan --table-name <CLUSTER_NAME>.accounts.users \
      --filter-expression "enabled = :f" --expression-attribute-values '{":f":{"BOOL":false}}' \
      --projection-expression username --region <REGION>
    ```

    Bad result: an account in the list that still signs in with a key on purpose. Enable it in the portal.

13. **Gateway certificate for native DCV clients.** Where you provided the gateway certificate, a `.dcv` file downloaded after the upgrade leaves the certificate check to the client.

    ```bash
    ./idea-admin.sh config show --cluster-name <CLUSTER_NAME> --aws-region <REGION> --query 'dcv_connection_gateway\.certificate\.provided$'
    openssl s_client -connect <GATEWAY_HOST>:443 -showcerts </dev/null | grep -c 'BEGIN CERTIFICATE'
    ```

    Bad result: `provided` is true and the count is 1, or the certificate's name does not match the gateway host. Native clients then prompt or refuse. Serve the leaf with its intermediate certificate. With `provided` false nothing changes.

14. **API clients that delete folders.** `FileBrowser.DeleteFiles` removes a folder only when the request sets `include_folders: true`. Search your own scripts for `DeleteFiles`; a client that sends folders without the flag gets them refused. The portal is not affected.

15. **Pinned control-plane image.** An `ecs.image` the upgrade leaves in place must be a 26.11.2 build: see [release images and template defaults](#release-images-and-template-defaults).

16. **Groups and projects the start-time refresh cannot account for (optional, informational).** The refresh at every start removes access only on an explicit `enabled: false`. Run this read-only check to see what it will leave alone.

    ```bash
    C=<CLUSTER_NAME>; R=<REGION>; jq -rn --argjson g "$(aws dynamodb scan --region $R --table-name $C.accounts.groups --projection-expression 'group_name,enabled' --output json)" --argjson p "$(aws dynamodb scan --region $R --table-name $C.projects --projection-expression 'project_id,#n,enabled,ldap_groups' --expression-attribute-names '{"#n":"name"}' --output json)" '[$g.Items[].group_name.S] as $all | ($g.Items[]|select(.enabled.BOOL==null)|"group without enabled: \(.group_name.S)"), ($p.Items[]|select(.enabled.BOOL==null)|"project without enabled: \(.name.S) \(.project_id.S)"), ($p.Items[] as $x|$x.ldap_groups.L[]?.S|select(IN($all[])|not)|"project \($x.name.S) names a group with no record: \(.)"), "groups \($g.Count), projects \($p.Count)"'
    ```

    A group without `enabled` still grants access. A project without `enabled` is one nobody has enabled yet, which is normal; the refresh neither adds nor removes its rows. A project that names a group with no record keeps its rows. Nothing needs fixing before the upgrade, but a project in the list that people rely on should be enabled in the portal.

### What Changes After the Upgrade

In plain terms, most disruptive first.

**Can stop new hosts**

- **A file system may no longer be where people expect it.** A share scoped to modules is mounted only in the tasks of the modules it names (check 2), so it can be missing from the login node or the portal's file browser. To bring it back, add `bastion-host` or `cluster-manager` to the share's `modules` and deploy that module.
- A host stops, with a status tag and the reason in its bootstrap log, when a checked download fails, a share in its scope does not mount, a job's FSx for Lustre is not available, the directory join fails or a directory secret cannot be read. Before, such a host could carry on half set up.
- Shared storage and job scratch mount `nodev` on new hosts, unless the stored options name `dev` or `nodev`. Set-id programs keep working. Running hosts keep their mounts until replaced.
- Only a blank disk of the requested size becomes job scratch.
- New Windows desktops stop in error when the directory join or the bootstrap package download fails, where they could reach READY in a bad state. The cause is at Error level in the desktop's bootstrap log.

**Can hold or refuse jobs**

- Queue limits hold (check 6), and queue position orders provisioning: every job of a profile's first queue before any job of its second.
- A queue's stored default existing FSx for Lustre mounts on its new jobs (check 7). A job whose file system, new or existing, does not mount is held with the reason.
- Licensed jobs wait when a check command does not work as `ideaserviceaccount` (check 8).
- At most half the scheduler's request workers (8 by default, `scheduler.server.max_workers`) take portal and API submissions at once. The next gets "The scheduler is busy with other job submissions. Try again in a few seconds." A bash submission script is ended after 300 s.
- A job asking for a `terminate_when_idle` above its queue's is refused at submission; on the built-in `job-shared` queue that is above 3. Jobs already queued keep their value.
- Queue profiles created from now on do not let jobs create a new FSx for Lustre until an administrator ticks **Jobs may create a new FSx for Lustre**. Existing and built-in profiles keep allowing it; clear the box on those where it is not wanted.
- A job whose node stops with `shared-storage-unavailable` (a cluster share did not mount) is held at once, with no retries.
- A job that took its queue's default existing FSx cannot be moved with `qmove` into a queue that restricts `fsx_lustre` and has a different default or none.
- Turning scratch off in a queue profile and saving clears its stored scratch and FSx defaults.
- Job script templates that use a removed construct do not render (check 10).
- A `job_uid` may hold letters, digits, underscores and dashes, up to 64 characters. A job queued before the upgrade is held with the reason only if its stored `job_uid` or `job_group` fails the submission rule; the other jobs go on. Delete it and submit it again.

**Accounts and access**

- Project membership follows enabled groups, at every start (check 11). Disabled accounts' SSH keys are set aside (check 12).
- Deleting an account renames its home aside as `<username>_<time>`, readable by root only, on a directory IDEA can write to. A home that is a link or its own mount, and any home on a read-only directory, stays in place.
- A re-enabled account without single sign-on gets a password reset email.
- Only cluster administrators can name a user id, create accounts or groups in privileged groups, change storage or scratch mounts, network settings, the sign-in logo, or site hook checksums. Managers lose these. Module administrators reach only their own module's settings.
- From 26.11.2 on, ending a user's sessions (sign out everywhere, password reset, disable) also stops the API tokens created before that moment. Sessions ended before the upgrade do not affect existing tokens.
- Ending a user's sessions also ends their browser terminals within 15 seconds.
- A single sign-on that is under way during the upgrade is lost; start it again.

**Desktops**

- A desktop whose owner is no longer a member of its project, or whose project is disabled, cannot be started or connected to, and its scheduled starts fail. A running session is not cut off. Only cluster administrators can move a stopped desktop to another of the owner's projects; the move puts the host on that project's instance profile, and changes nothing if that fails (the portal also shows the action to managers, who are refused).
- Desktops can be shared with a group of up to 100 members. Shares end at their expiry.
- With the stopped desktop cleanup on, a desktop whose owner cannot be emailed for a lasting reason (cluster email off, notifications off, the cleanup notice off, the owner disabled, removed or without an address) is still deleted once the warning window has passed. A send that fails is retried, and the warning window starts again. When email is on but its sender address, region or the cleanup template is missing, or the mail service keeps refusing the message, the desktop is warned every window and never deleted: look for `warning again` in the desktop controller log and `failed to send email notification` in the cluster manager log. A notice recorded as not sendable still decides the deletion if email is turned on, or the owner enabled, before the window ends. See [Stopped desktop cleanup](../../../modules/virtual-desktop-interfaces/admin-documentation/stopped-desktop-cleanup.md).
- The desktop gateway sees client addresses, so the cluster's prefix lists apply to desktop connections. The cluster's NAT gateways are admitted. With an existing VPC, add its NAT addresses to a prefix list, or desktops and hosts inside the VPC cannot open a session through the public address.
- Every running Linux desktop is refreshed once more. `virtual-desktop-controller.events.max_receive_count` is no longer read: a failing desktop event goes to the dead-letter queue after 16 receives.
- On Windows desktops with a local account, the owner sets a password from the **Set my Windows password** shortcut. Desktop bootstrap packages made before the upgrade stay under `idea/<vdc module id>/dcv-host-bootstrap/` in the cluster bucket (`aws s3 ls s3://<cluster bucket>/idea/vdc/dcv-host-bootstrap/ --recursive` lists them); delete the ones no running desktop needs.

**Login node and files**

- `su`, `newgrp`, `sg`, `passwd`, `gpasswd`, `mount`, `umount`, `write`, `wall`, `at` and `crontab` run only with the caller's own rights. Passwords are changed in the portal. tmux sessions end when the bastion task is replaced, as before; new ones live under `/run/idea-tmux`.
- A pinned site hook runs from a copy, so `$0` is the copy's path. A hook that finds its own folder through `$0` should name the shared path.
- The `DeleteFiles` API removes a folder only with `include_folders: true`. A folder lists its first 10,000 entries. One person can have 2 download archives being prepared at once. The editor opens files up to 5 MB.
- In a browser that cannot start the portal's service worker (a private window, an untrusted certificate), reloading the page asks the person to sign in again. The session is kept in the page and is not written to browser storage.

**Upgrade and roles**

- An upgrade of analytics alone is refused while a deployed service has no search rule of its own. An unknown module id is refused.
- The upgrade ends with a second cluster stack deploy: see [Desktop Broker Rules at the End of the Upgrade](#desktop-broker-rules-at-the-end-of-the-upgrade).
- If you answer No to the AMI and settings updates while `ecs.image` still names an older release, the upgrade stops before deploying anything. Run it again and answer Yes.
- Before each service deploys, the upgrade checks the shared storage mounts of every active container host, also when `--refresh-hosts` is given. A host built before 26.11.2 is checked against the shares its launch template mounts. If a control-plane share was added or moved since that host was built, the upgrade stops and prints a command that replaces the hosts (it includes the ecs module and `--refresh-hosts`).
- A container instance that is still registered but can take no task (its agent is disconnected, or it is no longer in the host group) does not stop the upgrade; the upgrade prints the command that deregisters it.
- Operator rows (`cli`, `api`) of global settings survive the end-of-upgrade cleanup. Rows with no source marker under a retired key are still removed.
- At the end of the upgrade, the rows of every module set the cluster has are kept for the modules this release still has. Rows for a module the release no longer has are removed in every set.
- Role changes are listed in the [changelog](../../../changelog.md). The gateway task role no longer carries `cluster.iam.ec2_managed_policy_arns`.

**Checks after the upgrade**

1. The upgrade log shows the second cluster pass and "Completion verified".
2. The scheduler service shows no task replacement in its ECS events for 15 minutes. Its health check now also needs `pbs_sched` running and a successful job listing.
3. Read the pins of check 1 back.
4. For a day, look for `AccessDenied` or `UnauthorizedOperation` in the module logs or CloudTrail for the cluster's task and host roles.
5. A new compute node and a new desktop of each OS reach service, and carry no failed `idea:BootstrapStatus` tag. One compute and one desktop image bake completes.
6. A job stack created before the upgrade is deleted when its job ends. A new job's log group has a retention.
7. A desktop connects through the gateway from outside and from a host inside the VPC.
8. The login node and the portal file browser show every file system people use there.
9. `cluster-manager.notifications.email.enabled` exists (`config show --query 'notifications\.email\.enabled$'`). With the stopped desktop cleanup on and that row missing, desktops are deleted after the window with no email.
10. With a customer DynamoDB key, the `<CLUSTER_NAME>.vdc.desktop-cleanup` table exists after the controller starts.

### Desktop Broker Rules at the End of the Upgrade

When the desktop controller stack holds its own broker listener rules, `upgrade-cluster` deploys the cluster stack once more at the end, which removes the VPC-wide broker client and gateway rules, and reads the template back. If that pass fails, every module is still upgraded and serving with the wide rules in place; the rest of the upgrade completes and the command reports the failure: re-run `upgrade-cluster`. After rolling a desktop controller stack back by hand (a run with `--no-rollback` that failed), deploy the cluster module before anything else, so the wide rules come back while the controller has none of its own.

### Deploy Order, Timing and Verification

Stacks of one priority group deploy together, at most `--max-parallel` (default 4) at a time, and groups run in priority order: the cluster stack; the metrics, directory, identity provider and (once every deployed cluster manager, scheduler and desktop controller holds its own search rule) analytics stacks; shared storage; the container stack; the cluster manager; then the desktop controller, scheduler and bastion together. The bastion follows the cluster manager because its hosts need the terminal CA and the directory joins that the cluster manager serves. When a deployed service still has no search rule of its own, analytics deploys after the services. `--serial` keeps the earlier groups, with analytics and the bastion each on their own; `--no-optimize-deployment` deploys one stack at a time. Lines printed by stacks deploying together start with the stack name.

When a stack fails, the other stacks of its group finish, no further stack starts, values.yml is saved to the cluster bucket, the verification below runs, and the error names every failed stack. Termination protection stays cleared, as before, and is restored by the next successful run.

Each phase and each stack prints a timestamp and its elapsed time, and the run ends with a timing table (phase, stack, seconds).

After the last deploy the upgrade runs `ideactl verify-cluster` once: stack status, ECS service running and desired counts, rollout state and image, target health, scheduler and bastion container health, and AccessDenied-class CloudTrail events from the cluster's roles since the upgrade started. It reads AWS APIs only and changes nothing, so you can re-run it alone, also when the VPN dropped during the upgrade. A failed line makes the upgrade exit non-zero after every other step has run; a transient state (a task still starting) passes on a re-run of `verify-cluster`. CloudTrail delivers events up to about 15 minutes late, and a lookup your credentials may not make reads `not checked`.

### Interrupted Upgrades

An upgrade interrupted by a lost connection or a sleeping laptop leaves the stacks it already deployed on the new release, the stack it was deploying finishing on its own in CloudFormation, termination protection cleared on the module instances, and the `cluster.upgrade_module_set_owners` row in place. Run the same `upgrade-cluster` command again. It waits for a stack still updating, reuses the interrupted run's deployment id (printed before the deploy starts), so each stack already deployed shows `no changes` and is not updated again, deploys the rest, and restores termination protection at the end. Analytics is updated on every run, because its dashboard target group takes a new name each time. Pass `--deployment-id` only to choose the id yourself.

### Site Hook Checksums

Nothing changes at upgrade: a site hook without a configured checksum runs as before, and the log warns with the checksum to set. To pin a hook (recommended), compute `sha256sum <hook file>` and set its setting under the module's id (the desktop controller's is `vdc` on a default cluster): `bastion-host.site_hook_sha256` (login node), `scheduler.compute_node_site_hook_sha256`, `scheduler.ami_builder_site_hook_sha256`, `vdc.dcv_host_site_hook_sha256`, `vdc.ami_builder_site_hook_sha256`. Each is empty or 64 hex characters (case and surrounding spaces ignored); the deploy warns when one is set under the module name instead. Set the checksum again after every edit of the hook. A mismatch skips the login node's hook (the login node still starts). It stops a compute node's or image bake's bootstrap with `site-hook-mismatch`, and a new desktop's while it is still provisioning; resumed or rebooted desktops do not re-check it. Values are compared without surrounding spaces and without regard to case. See [Custom AMIs](../../../modules/hpc-workloads/admin-documentation/custom-amis.md).

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
* `--max-parallel`: Stacks of one priority group deployed at once (default: 4). See [Deploy Order, Timing and Verification](#deploy-order-timing-and-verification)
* `--serial`: Keep the earlier priority groups, with analytics and the bastion each deployed on their own
* `--force`: Skip phase confirmation prompts; overwritten rows with no source marker that differ from generated configuration still require drift acceptance
* `--accept-config-drift`: Accept overwriting differing rows with no source marker after reviewing `./idea-admin.sh config preview-upgrade`; release image moves, template default updates and rows written from the portal or the command line (which keep their values) do not need this flag
* `--skip-global-settings-update`: Skip the global settings update if you've already done it
* `--module-set`: Name of the module set to use (default: default)
* `--deployment-id`: UUID to identify the deployment. Without it, a re-run after an interrupted upgrade reuses that run's id; see [Interrupted Upgrades](#interrupted-upgrades)
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
unchanged and remain visible in the preview. Such an image must be a 26.11.2 build before you upgrade: the 26.11.2 bastion
health check runs a script older images do not have. When an operator digest names this repository at an older release or
candidate tag (`<repository>:26.11.1@sha256:...`), the preview reports it as blocking and the upgrade refuses before it
changes anything. The upgrade cannot read the release of a bare digest, another repository or a build tag; if such an image
is older, the bastion deployment rolls back and the upgrade stops.

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

### Mirrored registries and offline hosts

`idea-admin.sh` runs the control-plane image by its digest, never by a tag a registry can move. It
takes the digest from the first of:

1. `IDEA_IMAGE_DIGEST`, when set.
2. The `idea-admin.sh` attached to the release, which carries its release's digest (unless
   `IDEA_REVISION` is set).
3. The `image` field of the release's `candidate.json` on GitHub
   (`https://github.com/cfs-energy/idea/releases/tag/v<VERSION>`).
4. Only when that file cannot be read: a copy of `<repository>:<revision>` already pulled on this
   host, with a warning that it could not be checked against the release record.

It pulls only an image that is not on the host. It tags `<repository>:<revision>` only for the image
the release record names, never for one chosen by `IDEA_IMAGE_DIGEST` or a release-attached script
(a candidate's script carries a candidate image that reports the same release). A local
`<repository>:<revision>` that names another image than the record is refused with the command that
removes it. If it cannot choose a digest at all, it stops and names the variable to set and where
to get the value.

**Mirrored registry** (for example a private ECR repository in GovCloud):

1. On a machine with internet access, read the digest: the `image` field of `candidate.json` on the
   release page, `public.ecr.aws/s5o2b4m0/idea-control-plane@sha256:<digest>`.
2. Copy the image with its digest unchanged, for example
   `skopeo copy --preserve-digests docker://public.ecr.aws/s5o2b4m0/idea-control-plane@sha256:<digest> docker://<mirror>/idea-control-plane:<VERSION>`.
   A copy that rewrites the manifest gets a new digest: use the mirror's digest in the next step.
3. On the administration host, run every command with
   `IDEA_DOCKER_REPO=<mirror>/idea-control-plane IDEA_IMAGE_DIGEST=sha256:<digest>`, and
   `IDEA_ECR_CREDS_RESET=false` when you sign in to the mirror yourself.
4. Point the cluster at the mirror with `ecs.image` as described under
   [release images and template defaults](#release-images-and-template-defaults).

**Host with no registry access**:

1. On a machine with access, `docker pull public.ecr.aws/s5o2b4m0/idea-control-plane@sha256:<digest>`,
   then `docker save -o idea-control-plane-<VERSION>.tar public.ecr.aws/s5o2b4m0/idea-control-plane@sha256:<digest>`,
   and record `sha256sum idea-control-plane-<VERSION>.tar` and
   `docker image inspect --format '{{json .RootFS.Layers}}' public.ecr.aws/s5o2b4m0/idea-control-plane@sha256:<digest>`.
2. Move the file to the host, check its `sha256sum`, run `docker load -i idea-control-plane-<VERSION>.tar`,
   and check that `docker image inspect --format '{{json .RootFS.Layers}}' <loaded image>` prints the
   same list.
3. A loaded image carries no registry digest, so run it by name:
   `docker tag <loaded image> idea-control-plane:v<VERSION>`, then run every command with
   `IDEA_LOCAL_IMAGE=true IDEA_ECR_CREDS_RESET=false`.

Network access the upgrade needs from the administration host: GitHub (`github.com` and its release
download host) unless `IDEA_IMAGE_DIGEST` or the release-attached `idea-admin.sh` gives the digest (offline, a pulled copy is used with a warning); the image registry only when the
image is not on the host yet; and, as before, the AWS APIs of the cluster and the public ECR lookup
of `upgrade-cluster`'s release image check (`--skip-release-image-check` for a mirror).

### ECR Credentials Reset

Before pulling the image, `idea-admin.sh` resets its public ECR credentials, which needs
`dig`. A stock Amazon Linux 2023 host does not ship `dig`; when it is missing the reset is skipped
with a warning and the command continues. Install `bind-utils` to restore the reset, or set
`IDEA_ECR_CREDS_RESET=false` to skip it without the warning.

### Going Back to 26.11.1

This is also the way back when a narrower 26.11.2 role is missing a grant: redeploying one module with the 26.11.1 administrator alone keeps the 26.11.2 image on that module's 26.11.1 role.

Go back with the 26.11.1 administrator and the 26.11.1 control-plane image together. Neither is enough alone. The 26.11.1 upgrade leaves an `ecs.image` newer than itself where it is, so its stacks would run the 26.11.2 image: the cluster-manager role loses the Secrets Manager grants that image hands passwords over with, and every password change, first sign-in and forgotten-password reset fails with "The password could not be set right now. Try again." The 26.11.2 administrator with a 26.11.1 image gives the bastion a health check script that image does not have, so the bastion deployment rolls back and the deploy stops.

The commands below set `IDEA_REVISION=v26.11.1`. If you set `IDEA_IMAGE_DIGEST` for a mirror,
set it to the 26.11.1 image's digest for these commands, or unset it (`env -u IDEA_IMAGE_DIGEST`):
`idea-admin.sh` refuses to run an image whose release differs from `IDEA_REVISION`. The same holds
for the 26.10.9 recovery command.

1. Read the image settings:

   ```bash
   IDEA_REVISION=v26.11.1 ./idea-admin.sh config show --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
     --query '^ecs\.(image|previous_release_image)$'
   ```

2. Set `ecs.image` to the 26.11.1 image: the same repository at tag `26.11.1`. On GovCloud pulling the public image, the upgrade wrote a `26.11.2@sha256:...` digest. Use `ecs.previous_release_image` instead of the tag only if it reads `...:26.11.1@sha256:...`; the upgrade records it only when the image before was a release digest, so after a candidate or operator image it holds an older release or nothing. A mirror needs its own 26.11.1 image.

   ```bash
   IDEA_REVISION=v26.11.1 ./idea-admin.sh config set "Key=ecs.image,Type=string,Value=<REPOSITORY>:26.11.1" \
     --cluster-name <CLUSTER_NAME> --aws-region <REGION>
   ```

3. Delete the job cache copy interval 26.11.2 added at 3600 seconds, unless you set it yourself. 26.11.1 reads it, and without it copies every 5 minutes. A 26.11.1 scheduler task that dies without an orderly stop starts again from that copy, up to an hour old, and repeats the completion emails and metrics of the jobs that finished since. The next 26.11.2 upgrade adds the row again at 3600 seconds.

   ```bash
   IDEA_REVISION=v26.11.1 ./idea-admin.sh config delete scheduler.job_cache.backup_interval_seconds \
     --cluster-name <CLUSTER_NAME> --aws-region <REGION>
   ```

4. Run the 26.11.1 upgrade straight away, so no 26.11.2 deploy runs on the 26.11.1 image:

   ```bash
   IDEA_REVISION=v26.11.1 ./idea-admin.sh upgrade-cluster --cluster-name <CLUSTER_NAME> --aws-region <REGION>
   ```

5. Once it has finished, delete the password secrets 26.11.2 left. 26.11.1 neither reads nor removes them, and each holds a password in clear:

   ```bash
   aws secretsmanager list-secrets --region <REGION> \
     --filters Key=name,Values=<CLUSTER_NAME>-password-sync- --query 'SecretList[].ARN' --output text
   aws secretsmanager delete-secret --region <REGION> --secret-id <ARN> --force-delete-without-recovery
   ```

What going back leaves:

* **Queued passwords**: a password still queued for the directory when the 26.11.1 tasks take over is lost. 26.11.1 cannot read the secret it points to, retries it until the queue moves it to `<CLUSTER_NAME>-cluster-manager-tasks-dlq.fifo`, and the user sets the password again. A password set while the 26.11.2 cluster-manager tasks drain can fail with "The password could not be set right now. Try again." and must be set again. 26.11.1 runs a user's account tasks in order, so the failing message holds that user's later account tasks, the password set again among them, for about 40 minutes: 16 tries, each 150 seconds apart. Every other user's queued account tasks run on 26.11.1 as before.
* **Settings**: the settings 26.11.2 added stay. Apart from the job cache copy deleted in step 3, 26.11.1 does not read them, and they apply again after the next upgrade. `cluster-manager.metrics.cost.lookback_days` stays at 7 where 26.11.2 moved it; 26.11.1 re-reads seven days of Cost Explorer with it.
* **Desktop and compute images**: 26.11.1 rebuilds every managed image that is not pinned, as after any upgrade. Until a rebuild passes, new launches use the image 26.11.2 baked, whose CloudWatch agent and rsyslog 26.11.1's bootstrap starts.
* **Cost records**: see below.

26.11.2 stores a user's desktop disk inventory when it changes rather than every 15 minutes. 26.11.1 reads each stored row as one 15 minute sample, so after going back it undercounts disk costs on any day 26.11.2 wrote. A day is recomputed on every collector run until it is more than 3 days old and is stored after that, so return to 26.11.2 within 3 days and these days are computed correctly again. A day stored while on 26.11.1 stays low until the month-close pass on the 5th of the next month recomputes it on 26.11.2.

Finish a rolling upgrade in one go for the same reason: a 26.11.1 collector that regains leadership during it reads the same rows as gaps. After going back and upgrading again, delete the `head-subjects-seeded` row (subject `!collector`) from the personal costs table, so heads written meanwhile are found.

### Load Balancer Log Bucket Policy

The cluster bucket's policy for load balancer access logs (writes only to this account's `AWSLogs/` path) is installed by `ideactl bootstrap`, not by `upgrade-cluster`. An upgraded cluster keeps its previous bucket policy until the first bootstrap after the upgrade, which is the same run as the log archive expiry below.

### Scheduler Log Archive Expiry

The first `ideactl bootstrap` after upgrading applies the 400 day expiry on `logs/<scheduler module id>/` in the cluster bucket and deletes older archives, with no prompt. `upgrade-cluster` and `ideactl deploy`, even `ideactl deploy all`, do not apply it. Each scheduler module has its own `<module id>.log_archive.s3_retention_days`. To keep archives for ever, run this before that first bootstrap:

```bash
./idea-admin.sh config set "Key=scheduler.log_archive.s3_retention_days,Type=int,Value=0" --cluster-name <CLUSTER_NAME> --aws-region <REGION>
```

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
