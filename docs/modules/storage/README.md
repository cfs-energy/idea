# Shared Storage

IDEA supports multiple type of storage backend such as:

* Elastic Block Storage ([https://aws.amazon.com/ebs/](https://aws.amazon.com/ebs/))
* Elastic File System ([https://aws.amazon.com/efs/](https://aws.amazon.com/efs/))
* FSx for Lustre ([https://aws.amazon.com/fsx/lustre/](https://aws.amazon.com/fsx/lustre/))
* FSx for OpenZFS ([https://aws.amazon.com/fsx/openzfs/](https://aws.amazon.com/fsx/openzfs/))
* FSx for NetApp OnTap ([https://aws.amazon.com/fsx/netapp-ontap/](https://aws.amazon.com/fsx/netapp-ontap/))
* FSx for Windows File Server ([https://aws.amazon.com/fsx/windows/](https://aws.amazon.com/fsx/windows/))

You can choose to deploy your file-system partition logic via the "Shared Storage" module on IDEA. You can let IDEA creates the storage backend (when possible) or use existing storage solutions running on your AWS account.

Additionally[, you can map your mount point based on IDEA Project membership](../cluster-manager/projects-management.md) such as:

* Mount `/team1/project_name` (FSx for Lustre) for all Linux [virtual-desktop-interfaces](../virtual-desktop-interfaces/ "mention") launched for Project A
* Mount `\\windows\project_name` (FSx for Windows File Server)for all Windows [virtual-desktop-interfaces](../virtual-desktop-interfaces/ "mention")
* Mount `/team2/shared` Elastic File System for all [virtual-desktop-interfaces](../virtual-desktop-interfaces/ "mention") launched by team B
* Mount `/fsx-scratch` FSx for OpenZFS for all compute nodes provisioned to run [hpc-workloads](../hpc-workloads/ "mention") on `queue1` or `queue2`

Refer to [storage-management.md](storage-management.md "mention") to learn more about the integration of Shared Storage module onto the IDEA ecosystem
