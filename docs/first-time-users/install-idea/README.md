# Install IDEA

New clusters use the container control plane only. The installer creates the shared container host pool and deploys the cluster manager, scheduler, virtual desktop controller, broker, gateway, and bastion as container services. It does not publish unused bastion host bootstrap packages.

Do not use the installer to convert an existing host-based cluster. Follow [Move the control plane to containers](../cluster-operations/update-idea-cluster/move-to-containers.md) and run `upgrade-cluster --drain` so the scheduler closes submission and drains existing jobs before cutover.

Step 1: Make sure you have the [pre-requisites.md](pre-requisites.md "mention") installed on your system.

Step 2: Follow the [standard-installation.md](standard-installation.md "mention") guide.
