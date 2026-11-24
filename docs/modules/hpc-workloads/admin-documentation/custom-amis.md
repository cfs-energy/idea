# Images

**Administration → Images and applications → Images** shows the images desktops and jobs launch from. It has two tabs:

* **Managed images**: one image per operating system, architecture and GPU variant. IDEA rebuilds these from the newest vendor image, validates each one, and switches new desktops and jobs to it only when every check passes.
* **Custom images**: images you build yourself. They are not validated, and the managed pipeline never moves or replaces them.

Running desktops and jobs are never touched by anything on this page. A new image applies to the next desktop or job that launches.

Desktop rows need the desktop administration privilege and compute rows need the job administration privilege. You see the rows your privileges cover.

## Managed images tab

### Rows

Each row is one desktop or compute image, keyed by base OS, architecture (`x86_64` or `arm64`) and variant: **CPU**, **GPU (NVIDIA)** or **GPU (AMD)**. GPU rows exist only where the cluster has a GPU software stack for that OS. Windows Server 2019, 2022 and 2025 desktop images are managed like the Linux ones.

Columns:

* **Image**: base OS, architecture and variant.
* **Kind**: Desktop or Compute.
* **Status**: see below.
* **Current image**: the AMI new launches use.
* **Previous image**: the last validated AMI before it, which **Roll back** returns to.
* **Last check**: when the row last finished or started a bake.

Use the text filter or the **Kind**, **OS family**, **Architecture**, **GPU variant** and **Status** filters to narrow the table. While any row is in progress, the table reloads every 30 seconds.

The line above the table shows the vendor image check schedule, when it last ran and when it runs next.

### Statuses

| Status | Meaning |
| --- | --- |
| **Current – validated** *date* **(release** *version***)** | The current image passed every check on that date, on that IDEA release. |
| **Baking: queued** | Waiting for a free bake slot. |
| **Baking:** *step* **(step** *n* **of 5)** | In progress. The steps are finding the newest vendor image, building, checking, test-launching, and switching new launches. |
| **Failed:** *reason* **– still on** *image* | A step or check failed. New launches stay on the image shown. **View log** opens the builder's log. |
| **Waiting for capacity – retry** *time* | EC2 had no capacity for the builder or the test launch. The row retries at the time shown. |
| **Pinned to** *image* | The row is pinned. It is not rebuilt or switched until you unpin it. |
| **Unsupported in this region** | The vendor does not publish this OS in the region. |

Rocky Linux publishes no images in the AWS GovCloud (US) partition, so there its compute and desktop rows read "Not available in this partition" and are never queued, refreshed or baked. A pinned or custom Rocky image is not affected.

**Rolled back: automatic updates paused** under a status means the row was rolled back. See [Roll back and pin](#roll-back-and-pin).

### Row actions

* **Rebuild**: refresh and validate this row.
* **Force rebake**: rebuild even if the row was already baked today. Cluster administrators only.
* **Roll back**: switch new launches to the previous validated image.
* **Pin** / **Unpin**: stop or resume automatic updates for the row.
* **Details**: the vendor image, candidate image, trigger, requester, attempts, start time, and every check with its result, detail and duration.

Rows that are in progress, pinned or unsupported cannot be selected or rebuilt.

## How images are refreshed

A row is rebuilt when one of these happens:

1. **Upgrade.** After an upgrade to a new release, every row is rebuilt and validated. The upgrade does not wait for this; check the Images page afterwards.
2. **Monthly rebuild.** On the first Sunday of each month at 02:00 cluster time, IDEA rebuilds every row, whether or not its vendor image changed, so images pick up package fixes. It waits for any bake in flight, and pinned and held rows are skipped. Each month's rebuild runs once, even if the controller restarts.
3. **Refresh and validate.** Use the buttons above the table:
   * **Refresh and validate all** queues every row. With a filter set, it reads **Refresh and validate shown (***n***)** and queues only the rows shown.
   * **Refresh and validate selected** queues the selected rows.
   * **Force rebake selected** queues the selected rows even if they were baked today. Cluster administrators only.

Each refresh builds from the newest vendor image, test-launches a desktop or job from the result, and switches new launches to it only if every check passes. Rows already in progress are skipped. Expect about 45 minutes per image, with four bakes at a time by default.

### Once a day per image

A row is baked at most once a day (cluster time). The upgrade trigger, the monthly check and **Refresh and validate** all skip a row that was already baked today, and the result message lists those rows as already updated today. To bake one again, use **Force rebake** on the row or **Force rebake selected**.

### Change the schedule

Click **Edit** on the schedule line to turn the monthly check on or off and choose the hour. The day is set in cluster settings (see [Settings](#settings)).

## What validation checks

Any failed check blocks the switch, records a plain reason on the row, and leaves new launches on the current image. Every timeout is a failure.

### In-bake checks

These run on the builder before the snapshot. On Linux:

* every bootstrap stage succeeded and the bootstrap log shows no errors;
* the running kernel is the default boot kernel;
* the Lustre module resolves for the running kernel (when the cluster uses FSx for Lustre, or the compute image includes the Lustre driver);
* desktops: the display manager, DCV server and session manager agent are installed;
* `sssd`, `realmd` and `adcli` are installed;
* the OpenPBS client is installed (OpenPBS clusters);
* the SSM agent is installed and enabled;
* GPU variants: the NVIDIA or AMD driver responds.

Before a bake installs a kernel it removes rescue images and, on a bake, kernels older than the running one. If `/boot` still cannot hold the new kernel the bake stops with `kernel-install-failed` and says to use a base image with a larger `/boot`.

On a cluster that uses FSx for Lustre, an EL 9 or 10 image that boots a kernel series newer than the newest the FSx client supports (Rocky 10.3 today) moves to the newest kernel of the newest supported series and reboots before the client installs. Rocky takes that kernel from its vault, so builders need access to `dl.rockylinux.org`. The host then stays on that kernel, which Rocky no longer updates, until FSx publishes a client for the newer series. If no supported kernel can be installed, the row fails with `lustre-module-missing` and keeps its previous image.

On Windows: the bootstrap succeeded with a clean log, the SSM agent is running, the DCV server and session manager agent services are installed, host utilities are installed, EC2Launch v2 is present for sysprep, the baked release matches, and GPU variants have a display driver.

The image is generalized before the snapshot: no domain membership, machine ID, SSH host keys or directory cache are kept, and Windows images are sysprepped.

### Test launch (desktops)

A copy of the base software stack points at the candidate image. A dedicated validation user in a hidden validation project launches a desktop from it through the normal desktop creation path. The production stack is not changed during the test. The test checks that:

* the desktop reaches Ready within the gate: **10 minutes on Linux, 15 minutes on Windows**, measured from the request;
* no bootstrap failure is recorded;
* the session has connection information and the gateway accepts it;
* `dcv list-sessions` shows the session;
* a directory user resolves (Linux), or the domain secure channel works (Windows);
* every configured shared file system is mounted and passes a write, fsync, read and delete probe;
* the bootstrap completion line reaches CloudWatch (a missing log is a failure);
* GPU variants: `nvidia-smi` lists a GPU, or the AMD driver is loaded;
* after one reboot, the desktop reaches Ready again within the gate.

The test desktop and the stack copy are deleted afterwards, whether the test passed or not.

### Test job (compute)

A real job runs on a hidden copy of a queue that uses the candidate image. It must land on that AMI, mount every shared file system, produce the expected output and exit 0.

When the job does not finish, the failure names the phase it reached:

* No node launched: the environment is the cause, not the image. The reason includes the scheduler's refusal when it gave one, such as no capacity or a limit.
* A node launched on the candidate image but never registered: the image is the likely cause, and the failure carries the node's log.
* The job started on a host but did not finish: a slow or unanswering file system on the node is the likely cause.
* The output file could not be read within the limit: the shared home is the likely cause, not the image.

A rebake checks that the shared home answers once and every row waits on that answer. A home that does not answer fails each row with "The home directory ... did not answer within 60 seconds" before any test node launches. The overall time limit is unchanged.

### Switching to the new image

A validated desktop image is written to the row's `ss-base-*` software stacks. A validated compute image replaces `scheduler.compute_node_ami` and the `instance_ami` of any queue profile that named the old image. Pinned targets are skipped, and custom software stacks are never changed. See [Virtual Desktop Images (Software Stacks)](../../virtual-desktop-interfaces/admin-documentation/virtual-desktop-images-software-stacks.md#two-images-on-a-base-stack).

Every path that changes a managed image, including `ideactl update-base-stacks`, applies the same rule: only a validated image can be used.

## Roll back and pin

**Roll back** switches new launches from the current image to the previous validated image. It does not rebuild or re-check the previous image. Automatic updates for that row pause until the next manual refresh of the row (**Rebuild**, **Refresh and validate**, or **Force rebake**).

**Pin** stops all automatic updates for a row: the upgrade trigger, the monthly check and **Refresh and validate** skip it. **Unpin** resumes them.

You can also pin a target instead of a row:

* **Desktop software stack**: a pinned `ss-base-*` stack keeps its image when its row switches. Changing a base stack's image to one the pipeline did not validate pins the stack automatically.
* **Queue profile**: a pinned queue profile keeps its `instance_ami`.
* **Scheduler default**: set `scheduler.images.default_image_pinned` to `true` to keep `scheduler.compute_node_ami`.

## Cleanup

Cleanup runs about every 30 minutes on the virtual desktop controller and the scheduler. Each sweep logs a one-line summary, even when it removes nothing.

* **Managed images**: each row keeps its current and previous validated images. Older managed images that nothing references are deregistered, with their snapshots.
* **Builder instances**: leftover builder instances are terminated.
* **Other builder images**: desktop and compute builder images that carry this cluster's tag but were not baked by the pipeline (for example, custom builds) are deregistered once they are older than `images.legacy_cleanup_min_age_days` (30 days by default) and nothing references them. A reference is any setting, queue profile, software stack, image row, instance or launch template that names the image. Each sweep removes at most 20, oldest first, logs each one, and deletes snapshots no remaining image uses. Set the value to `0` to keep them. Images without this cluster's tag are never touched.

{% hint style="warning" %}
**One-time manual review after upgrading to 26.10.1**

Desktop and compute builder images made before 26.10.1 carry no cluster tag, so cleanup never removes them. After the upgrade, list the builder images in your account (names starting `idea-dcv-host-` and `idea-compute-node-`) and deregister the ones you no longer need. Keep any image that a software stack, queue profile, launch template or `scheduler.compute_node_ami` still uses.
{% endhint %}

## Troubleshoot a failed row

1. Read the reason in the **Status** column. **Details** shows which check failed and its detail.
2. Click **View log**. It opens the builder's log stream, `bootstrap_<builder instance id>`, in the `/<cluster>/<module>/ami-builder` log group. The stream holds the bake log and the in-bake check results.
3. If the test launch failed, open the test desktop's stream, `bootstrap_<instance id>`, in the `/<cluster>/vdc/dcv-host` log group. The failure detail names the host.
4. Fix the cause, then click **Rebuild** on the row. If the row was already baked today, use **Force rebake**.

`<module>` is `vdc` for desktop rows and `scheduler` for compute rows, unless your cluster uses other module IDs.

New launches stay on the current image while a row is failed, so a failed row does not need urgent action.

## Add your own software to managed images

Put site software in a bake hook, not in a hand-built image. The pipeline runs the hook in every bake (each upgrade, the monthly check, **Rebuild** and **Refresh and validate**), validates the result, and moves queues and desktops to it like any other managed image. Your software stays current with no image to maintain.

Hooks are plain bash scripts on the cluster's shared `/apps` file system and apply to Linux images; Windows images don't run them. Edit them from any host that mounts `/apps`, such as the bastion. A missing file is skipped.

| Hook | Runs | Use it for |
|---|---|---|
| `/apps/<cluster>/scheduler/ami_builder/userdata_customizations.sh` | while each compute image bakes | software every job needs |
| `/apps/<cluster>/vdc/ami_builder/userdata_customizations.sh` | while each desktop image bakes | software every desktop needs |
| `/apps/<cluster>/scheduler/compute_node/userdata_customizations.sh` | on every compute node boot | per-node setup that must not be baked in |
| `/apps/<cluster>/dcv_host/userdata_customizations.sh` | once, while a new desktop is provisioned; a resumed or rebooted desktop does not run it again | per-desktop setup that must not be baked in |
| `/apps/<cluster>/bastion-host/userdata_customizations.sh` | at every start of the login node (bastion) container, for at most two minutes; when `bastion-host.site_hook_sha256` is set, only if the hook matches it | tools users expect on the login node; it is arm64 Amazon Linux 2023 |

* **Hook checksums (recommended).** Each hook can be pinned to its SHA-256, computed with `sha256sum <hook file>`. A setting key starts with the module's **id**, which `config set` writes as given. The desktop controller's id is `vdc` on a default cluster, not its name. Find a module's id with `./idea-admin.sh config show --query 'global-settings\.module_sets\..*\.module_id' --cluster-name <CLUSTER_NAME> --aws-region <REGION>`, then set the checksum with `./idea-admin.sh config set "Key=<module id>.<setting>,Type=string,Value=<digest>" --cluster-name <CLUSTER_NAME> --aws-region <REGION>`. The value must be empty or 64 hex characters (case and surrounding spaces ignored); a deploy refuses anything else, and warns when a checksum was set under the module name instead of its id. Without a checksum the hook runs as before and the log warns with the value to set. Either way the hook runs from a private copy of the file, the one whose checksum is logged or checked, so `$0` is the copy's path. The portal's settings page writes the right key itself.

  | Hook | Setting (default module id) |
  |---|---|
  | login node | `bastion-host.site_hook_sha256` |
  | compute image bake | `scheduler.ami_builder_site_hook_sha256` |
  | desktop image bake | `vdc.ami_builder_site_hook_sha256` |
  | compute node boot | `scheduler.compute_node_site_hook_sha256` |
  | new desktop (at provisioning) | `vdc.dcv_host_site_hook_sha256` |

  **A mismatch behaves differently by host.** Values are compared without surrounding spaces and without regard to case on every host.
  - **Login node:** a hook that does not match its checksum is skipped, both digests are logged, and the login node still starts.
  - **Compute nodes and image bakes:** a mismatch stops that host's bootstrap with `site-hook-mismatch`, naming both digests.
  - **Desktops:** the check runs just before the remote display server starts on a new desktop, so a mismatch stops that desktop while it is still provisioning, before its owner can connect, and the session is failed with a message. A resumed or rebooted desktop does not run or re-check the hook.

  So editing a pinned hook without updating its checksum stops every new compute node, new desktop and bake until the checksum is set again. Update the checksum in the same change as the hook.
* Hooks run as root with the cluster environment (`IDEA_CLUSTER_HOME`, `IDEA_BASE_OS`, …) and run for every Linux OS, so check `IDEA_BASE_OS` when a step only applies to one.
* A bake hook that exits non-zero fails that bake. The row stays on its last good image, and the error links to the hook's log. A boot hook that fails logs a warning and the node, desktop or login node still starts.
* After editing a bake hook, select the rows it affects and choose **Force rebake** (or wait for the next refresh) to bake it in.

### Queues follow the managed image

A queue profile or the cluster default compute image moves to each new compute image only when it already points at a managed image or a stock vendor image. A queue on an image IDEA didn't build in the pipeline, such as an older hand-built image, stays where it is. Point it at the row's current image once (edit the queue profile), and it follows from then on. Until then every row can read current while that queue launches the older image: `idea.health.bake.targets_off_current` counts such queues and the scheduler log names them. To keep a queue on a fixed image on purpose, pin it (see [Roll back and pin](#roll-back-and-pin)).

## Custom images tab

Use this tab for images outside the managed pipeline. A custom build is not validated, and the managed refresh never moves or replaces it.

**Compute images** lists the images jobs run on: the scheduler default and any queue profile that names an image.

* **Build** builds a new image for the row's OS and architecture.
* **Add image** builds one for an OS or architecture that has no compute image yet.
* **Set as default** sets `scheduler.compute_node_ami` to the row's completed build. It is offered only for an image with the same OS and architecture as the current default. Queue profiles with their own `instance_ami` are not changed.

**Desktop images** lists builds from the base OS images. A build here does not change any software stack. To use it, create a software stack from it.

The **Build** dialog takes an optional **Base AMI** (default: the newest vendor image for the OS) and **Builder instance type**. Pick a GPU instance type to build GPU drivers in. For compute images, leave **EFA** and **FSx for Lustre client** checked unless the image will never use them. A build takes about 20 minutes and one instance hour.

**Last custom build** shows how the most recent build ended, with the builder instance ID and the error when it failed.

{% hint style="info" %}
A custom build that nothing references is removed by cleanup once it is older than `images.legacy_cleanup_min_age_days`. Reference it from a software stack, queue profile or `scheduler.compute_node_ami` before then to keep it.
{% endhint %}

### Command line

Custom builds can also run from the command line, as root:

```bash
# scheduler
ideactl ami-builder build --base-os rocky9 --base-ami <vendor ami> --enable-driver efa --enable-driver fsx_lustre --force

# virtual desktop controller
ideactl build-desktop-image --base-os rocky9 --base-ami <vendor ami> --force
```

`build-desktop-image` no longer accepts `--update-stack`: base stacks only move to validated images.

## Settings

| Key | Default | Purpose |
| --- | --- | --- |
| `virtual-desktop-controller.software_stacks.image_refresh_schedule.enabled` | `true` | Run the monthly rebuild. Applies to desktop and compute rows. |
| `virtual-desktop-controller.software_stacks.image_refresh_schedule.day` | `first sunday` | `first`, `second`, `third`, `fourth` or `last`, then a weekday. |
| `virtual-desktop-controller.software_stacks.image_refresh_schedule.hour` | `2` | Hour in cluster time. |
| `virtual-desktop-controller.software_stacks.image_pipeline.ready_gate_seconds_linux` | `600` | Linux test launch limit from request to Ready. Also limits the compute test job. |
| `virtual-desktop-controller.software_stacks.image_pipeline.ready_gate_seconds_windows` | `900` | Windows test launch limit. |
| `virtual-desktop-controller.software_stacks.image_pipeline.max_concurrent_bakes` | `32` | Desktop bakes at a time. Each Linux bake runs an 8 vCPU builder by default (Windows 2), from the same On-Demand vCPU quota as jobs. |
| `scheduler.images.max_concurrent_bakes` | `32` | Compute bakes at a time. |
| `virtual-desktop-controller.images.legacy_cleanup_min_age_days` | `30` | Age before untracked desktop builder images are removed. `0` keeps them. |
| `scheduler.images.legacy_cleanup_min_age_days` | `30` | The same for compute builder images. |
| `scheduler.images.default_image_pinned` | `false` | Keep `scheduler.compute_node_ami` when a compute row switches. |
| `virtual-desktop-controller.software_stacks.image_pipeline.builder_instance_types.x86_64` | empty | Builder for x86_64 desktop and compute bakes. Empty uses `c7i.2xlarge` for Linux images and `m7i.large` for Windows. |
| `virtual-desktop-controller.software_stacks.image_pipeline.builder_instance_types.arm64` | empty | Builder for arm64 desktop and compute bakes. Empty uses `c8g.2xlarge`. |

### Builder size

With no type set, Linux bakes run on `c7i.2xlarge` (x86_64) or `c8g.2xlarge` (arm64), which took 31 to 41 percent less builder time than `large` in testing; Windows bakes stay on `m7i.large`. Where the region does not offer the 2xlarge type, the bake uses the previous default (`c7i.large` for compute, `m7i.large` for desktops, `m8g.large` for arm64) and the module log says so. When EC2 refuses the 2xlarge launch for capacity or the vCPU quota, the bake launches once on the previous default and the row records it. A full rebake needs 8 On-Demand vCPUs per Linux row baking at once (2 per Windows row): 42 rows all at once (39 Linux, 3 Windows) take about 320 vCPUs from the quota jobs use, so set `max_concurrent_bakes` (for example 30, about 240 vCPUs) where the quota is tight. Each pipeline has its own cap, `scheduler.images.max_concurrent_bakes` for compute images and `virtual-desktop-controller.software_stacks.image_pipeline.max_concurrent_bakes` for desktop images, and the builders of both count against the same On-Demand vCPU quota, so a cap of 30 on each can draw about 480 vCPUs when both pipelines are busy.

Both pipelines read the builder type for CPU bakes from `builder_instance_types`; GPU desktop images keep their GPU builder (`g4dn.xlarge`, `g5g.xlarge`, `g4ad.xlarge`). The x86_64 key takes `c7i`, `c7a` and `m7i` from `large` to `8xlarge` (and `c6i`, `m6i` `large` or `xlarge`); the arm64 key takes `c8g` and `m8g` from `large` to `8xlarge` (and `c6g`, `m6g` `large` or `xlarge`). The settings page refuses a type of the other architecture. Set it on the cluster settings page (Deployment, Installed deployment, advanced settings), or with `./idea-admin.sh config set Key=vdc.software_stacks.image_pipeline.builder_instance_types.x86_64,Type=string,Value=c7i.4xlarge --cluster-name <cluster> --aws-region <region>`; a value written with `config set` is not checked until the next bake, which fails with the reason. The next bake uses it; a bake already running keeps its builder. A larger builder draws on the same On-Demand vCPU quota as jobs, so lower `max_concurrent_bakes` (desktop and compute) when the quota is tight.

Every builder root disk, Linux and Windows, is gp3 at 16000 IOPS and 1000 MiB/s, or the most its size allows (500 IOPS per GiB, 0.25 MiB/s per IOPS; a 20 GiB desktop builder gets 10000 IOPS). The image keeps those figures in its root mapping, so desktops and compute nodes set their root volume to gp3 at 3000 IOPS and 125 MiB/s when they launch, as before.

The stored key prefix is the module ID, `vdc` for the default desktop module. Use `list-modules` to check a customized module ID before writing a key with `config set`.
