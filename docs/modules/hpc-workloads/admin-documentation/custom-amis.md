# Images

New clusters use the container control plane only. Move an existing host-based control plane with `upgrade-cluster --drain`; follow the [container upgrade guide](../../../first-time-users/cluster-operations/update-idea-cluster/move-to-containers.md).

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
2. **Monthly vendor check.** On the first Sunday of each month at 02:00 cluster time, IDEA looks for newer vendor images. Only rows whose vendor published a newer base image are rebuilt. Each month's check runs once, even if the controller restarts.
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
| `virtual-desktop-controller.software_stacks.image_refresh_schedule.enabled` | `true` | Run the monthly vendor check. Applies to desktop and compute rows. |
| `virtual-desktop-controller.software_stacks.image_refresh_schedule.day` | `first sunday` | `first`, `second`, `third`, `fourth` or `last`, then a weekday. |
| `virtual-desktop-controller.software_stacks.image_refresh_schedule.hour` | `2` | Hour in cluster time. |
| `virtual-desktop-controller.software_stacks.image_pipeline.ready_gate_seconds_linux` | `600` | Linux test launch limit from request to Ready. Also limits the compute test job. |
| `virtual-desktop-controller.software_stacks.image_pipeline.ready_gate_seconds_windows` | `900` | Windows test launch limit. |
| `virtual-desktop-controller.software_stacks.image_pipeline.max_concurrent_bakes` | `4` | Desktop bakes at a time. |
| `scheduler.images.max_concurrent_bakes` | `4` | Compute bakes at a time. |
| `virtual-desktop-controller.images.legacy_cleanup_min_age_days` | `30` | Age before untracked desktop builder images are removed. `0` keeps them. |
| `scheduler.images.legacy_cleanup_min_age_days` | `30` | The same for compute builder images. |
| `scheduler.images.default_image_pinned` | `false` | Keep `scheduler.compute_node_ami` when a compute row switches. |

The stored key prefix is the module ID, `vdc` for the default desktop module. Use `list-modules` to check a customized module ID before writing a key with `config set`.
