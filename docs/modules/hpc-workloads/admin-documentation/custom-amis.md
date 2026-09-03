# Custom AMIs

Scale-Out Computing > Custom AMIs lists the images your cluster launches from and lets an administrator build new ones from the portal. Use it to see which operating systems have a built image and which still launch from a stock vendor image.

## What the page shows

Two tables, one row per base OS.

**Compute images** are what jobs run on. The row shows the image the cluster uses for that OS today: the scheduler default (`scheduler.compute_node_ami`) first, then any queue profile whose `instance_ami` names an image of that OS. Operating systems nothing references show the newest `idea-compute-node-<os>-*` image in the account, if there is one.

**Desktop images** are the `ss-base-<os>-<arch>-base` software stacks eVDI desktops launch from. Windows is not listed; there is no build for it. Each base stack carries two images: `ami_id`, what desktops launch from, and `base_ami_id`, the stock image the next build starts from (shown as the base subline). The Refresh Base Stack AMIs action always moves `base_ami_id` to the newest stock image and moves `ami_id` only while it is still a stock image; a stack launching from a built image keeps it until you rebuild, and shows `Built (base outdated)` when the base has moved past what the image was built from. **Use built image** on a row points the stack back at its last completed build without rebuilding.

Each row carries:

* **State**: `Built` when the image was built by IDEA (`idea-compute-node-*` or `idea-dcv-host-*`), `Stock` when it is a vendor image, `Missing` when the referenced AMI no longer exists in the account, `None` when nothing references the OS, `Building` while a build is running.
* **Build date**: parsed from the image name.
* **Referenced by**: the scheduler default and queue profiles for compute; the base stack and the number of custom stacks sharing the image for desktops.
* **Last build**: how the most recent build for that OS ended, with the builder instance id and the error when it failed. Builds started with `ideactl` show up here too.

## Build

Build launches a builder instance from a stock image, runs the instance-agnostic half of the node bootstrap (packages, system updates, drivers, DCV for desktops), snapshots it and terminates the builder. It takes about 20 minutes and costs one instance hour plus the snapshot. The base image defaults to the newest stock image the vendor publishes for that OS; a build never starts from a previous build. You can override the base AMI and the instance type in the dialog.

* **Compute**: the EFA and FSx for Lustre drivers are included by default; uncheck them only for an image that will never touch them. The new image is not made the default automatically. When it is ready, **Set as default** on the row updates `scheduler.compute_node_ami`. Queue profiles pinned to an explicit `instance_ami` are edited in Queue Profiles.
* **Desktop**: the base stack is repointed at the new image and reindexed when the build finishes (on by default). Running desktops are unaffected; new desktops from that stack use the new image and reach READY in a few minutes instead of 15 or more. **Build all desktop images** on the desktop table starts one build per base stack in parallel with no inputs; rows already building are skipped and each stack is repointed only after its own build succeeds.

A build that fails leaves its builder instance stopped, not terminated, so the bootstrap logs stay available under `/apps/<cluster>/<module>/ami_builder/`. Terminate it once you have what you need. Only one build per OS and architecture runs at a time; a row still building three hours after it started is marked failed, which usually means the module restarted mid-build.

## Command line equivalents

```bash
# on the scheduler host, as root
ideactl ami-builder build --base-os rocky9 --base-ami <stock ami> --enable-driver efa --enable-driver fsx_lustre --force

# on the virtual desktop controller host, as root
ideactl build-desktop-image --base-os rocky9 --base-ami <stock ami> --update-stack --force
```

Both write the same build record the page reads, so a scripted build and a clicked one look identical afterwards.
