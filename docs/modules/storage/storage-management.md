# Storage Management

{% hint style="info" %}
Refer to [.](./ "mention")to learn more about Shared Storage configuration on IDEA.
{% endhint %}

## Apps and Data Storage (Required)

For the IDEA Cluster to function, shared storage configuration must include Apps and Data storage configurations. Both **Apps** and **Data** are **cluster** scoped file systems and are mounted automatically on all applicable infrastructure hosts, eVDI linux sessions and SOCA Compute Nodes.

#### Apps

* Apps shared-storage is used to save critical cluster configuration scripts, files and logs.
* For Scale-Out computing workloads, additional Applications (eg. OpenMPI or IntelMPI, Python, Solvers etc) can be installed on shared-storage, and can be leveraged by Compute Nodes.
* Default Configuration:
  * Apps storage is mounted on **/apps** mount path, and is configurable.
  * Amazon EFS is used as the default storage provider for Apps storage.
  * A custom CloudWatch monitoring rules and Lambda function is deployed for EFS Apps storage volumes, which help monitor the throughput of the file system and dynamically adjust the throughput mode to **provisioned** or **bursting**.

#### Data

* Data storage is primarily used to store User Home Directories.
* Additional directories for project/group level file shares can be created on Data Storage.
* Default Configuration:
  * Data storage is mounted on **/data** mount path, and is configurable.
  * Amazon EFS is used as default storage provider for Apps.
  * To save cost, EFS Lifecycle policy is set to move data to Infrequently Accessed storage class after **30 days.**

## Scope

A notion of **scope** is introduced in IDEA to enable cluster administrators manage multiple file systems and specify mount criteria based on access, use-case and workload needs. Shared Storage mounts can be scoped based on:

{% tabs %}
{% tab title="Cluster" %}
**Cluster**

Cluster scoped shared storage mounts are applied across all nodes in the cluster. These include applicable infrastructure nodes, SOCA Compute Nodes and eVDI Hosts.
{% endtab %}

{% tab title="Module" %}
**Module**

Module scoped shared storage mounts are applicable across all nodes for the module, including applicable infrastructure nodes and eVDI or Compute Nodes.
{% endtab %}

{% tab title="Project" %}
**Project**

Project scoped shared storage mounts are applicable for Compute Nodes or eVDI Hosts, if they are launched for an applicable Project.
{% endtab %}
{% endtabs %}

**Scale-Out Computing: Queue Profiles**

Queue Profile scoped shared storage mounts are applicable for all Compute Nodes launched for Jobs submitted to the queues configured under a Queue Profile.

| <ul><li><strong>Project</strong> and <strong>Module</strong> scopes can be combined to create an AND condition.</li><li><strong>Queue Profile</strong> and <strong>Project</strong> scopes can be combined to create an AND condition.</li></ul> |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |

## Add or Attach Shared Storage to Cluster

The **idea-admin.sh shared-storage** utility enables admins to generate configurations for:

* Provisioning new file systems
* Re-use existing file systems

Either of the use-cases can be executed **prior to initial cluster deployment** OR **after cluster deployment**.

{% hint style="info" %}
If shared storage configurations are updated **after** an IDEA Cluster is deployed, depending upon the Scope, manual actions will be required to mount the file system on applicable existing cluster nodes. All new hosts launched after the configuration update will automatically mount the configured file systems. See below for example(s)
{% endhint %}

### Provision new File System

{% hint style="info" %}
Shared Storage config generation for provisioning **new** file systems is only supported for Amazon EFS at the moment.
{% endhint %}

To generate configurations for provisioning new file systems you can use the idea-admin.sh shared-storage add-file-system command as below:

| <pre><code>$ ./idea-admin.sh shared-storage add-file-system --help                                          
Usage: idea-admin shared-storage add-file-system [OPTIONS]

  add new shared-storage file-system

Options:
  --cluster-name TEXT  Cluster Name
  --aws-region TEXT    AWS Region  [required]
  --aws-profile TEXT   AWS Profile Name
  --kms-key-id TEXT    KMS Key ID
  -h, --help           Show this message and exit.
</code></pre> |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |

**Example**

| <pre class="language-bash"><code class="lang-bash">./idea-admin.sh shared-storage add-file-system \
   --aws-region &#x3C;REGION>  \
   --cluster-name &#x3C;CLUSTER_NAME>  
Add Shared Storage to an IDEA Cluster

Shared Storage Settings

? [Name] Enter the name of the shared storage file system  (Must be all lower case, no spaces or special characters) testefs
? [Title] Enter a friendly title for the file system "New Shared EFS for Project A"
? [Shared Storage Provider] Select a provider for the shared storage file system Amazon EFS
? [Mount Directory] Location of the mount directory. eg. /my-mount-dir /custom_path
? [Mount Scopes] Select the mount scope for file system Cluster

New Amazon EFS Settings

? [Throughput Mode] Select the throughput mode Bursting
? [Performance Mode] Select the performance mode General Purpose
? [Enable CloudWatch Monitoring] Enable cloudwatch monitoring to manage throughput? No
? [Lifecycle Policy] Transition to infrequent access (IA) storage? Transition to IA Disabled
? [EFS Mount Options] Enter mount options nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0

Shared Storage Config ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

testefs:
  title: '"New Shared EFS for Project A"'
  provider: efs
  scope:
  - cluster
  mount_dir: /custom_path
  mount_options: nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0
  efs:
    kms_key_id: ~
    encrypted: true
    throughput_mode: bursting
    performance_mode: generalPurpose
    removal_policy: DESTROY
    cloudwatch_monitoring: false
    transition_to_ia: ~

──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
? How do you want to proceed further? Update Cluster Settings and Exit
sync config entries to db. overwrite: True
updating config: shared-storage.testefs.title = "New Shared EFS for Project A"
updating config: shared-storage.testefs.provider = efs
updating config: shared-storage.testefs.scope = ['cluster']
updating config: shared-storage.testefs.mount_dir = /custom_path
updating config: shared-storage.testefs.mount_options = nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0
updating config: shared-storage.testefs.efs.kms_key_id = None
updating config: shared-storage.testefs.efs.encrypted = True
updating config: shared-storage.testefs.efs.throughput_mode = bursting
updating config: shared-storage.testefs.efs.performance_mode = generalPurpose
updating config: shared-storage.testefs.efs.removal_policy = DESTROY
updating config: shared-storage.testefs.efs.cloudwatch_monitoring = False
updating config: shared-storage.testefs.efs.transition_to_ia = None
</code></pre> |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |

`idea-admin.sh` utility will automatically update your IDEA cluster environment if you select " Update Cluster Settings and Exit". You can also choose to automatically "Deploy" the cluster which will automatize the steps mentioned below. For this demo, we are just Updating Cluster Settings and will proceed to a manual deployment afterwards.

Once done, you can validate your new mount point in the web interface via "**Cluster Management**" > "**Settings**" > "**Shared Storage**"

<figure><img src=".gitbook/assets/Screen Shot 2022-12-05 at 6.55.39 PM.png" alt=""><figcaption><p>IDEA is now configured with the new mount but EFS has not been created yet</p></figcaption></figure>

At this point, the FileSystem ID is empty because you asked to provision a brand new EFS. To update the backend infrastructure and trigger the EFS creation, you must run `deploy` command ([see this page for more details about deploy utility](https://docs.ide-on-aws.com/idea/first-time-users/cluster-operations/update-idea-cluster/update-idea-backend-resource-idea-admin.sh-deploy)).

First, run the `idea-admin.sh cdk diff` to confirm the new EFS will be created:

```
./idea-admin.sh cdk diff shared-storage \
   --cluster-name <CLUSTER_NAME> \
   --aws-region <REGION> 
Stack <CLUSTER_NAME>-shared-storage
IAM Statement Changes
┌───┬────────────────────────────┬────────┬────────────────────────────────────┬───────────┬──────────────────────────────────────────────────────┐
│   │ Resource                   │ Effect │ Action                             │ Principal │ Condition                                            │
├───┼────────────────────────────┼────────┼────────────────────────────────────┼───────────┼──────────────────────────────────────────────────────┤
│ + │ ${testefs-storage-efs.Arn} │ Allow  │ elasticfilesystem:ClientMount      │ AWS:*     │ "Bool": {                                            │
│   │                            │        │ elasticfilesystem:ClientRootAccess │           │   "elasticfilesystem:AccessedViaMountTarget": "true" │
│   │                            │        │ elasticfilesystem:ClientWrite      │           │ }                                                    │
└───┴────────────────────────────┴────────┴────────────────────────────────────┴───────────┴──────────────────────────────────────────────────────┘
(NOTE: There may be security-related changes not in this list. See https://github.com/aws/aws-cdk/issues/1299)

Resources
[+] AWS::EFS::FileSystem testefs-storage-efs testefsstorageefs
[+] AWS::EFS::MountTarget testefs-storage-efs/testefs-storage-efs-mount-target-1 testefsstorageefstestefsstorageefsmounttarget15EF95F6B
[+] AWS::EFS::MountTarget testefs-storage-efs/testefs-storage-efs-mount-target-2 testefsstorageefstestefsstorageefsmounttarget20E352012
[+] AWS::EFS::MountTarget testefs-storage-efs/testefs-storage-efs-mount-target-3 testefsstorageefstestefsstorageefsmounttarget32982F891
[~] Custom::ClusterSettings <CLUSTER_NAME>-shared-storage-settings ideapatchusharedstoragesettings
 └─ [~] settings
     ├─ [~] .deployment_id:
     │   ├─ [-] d99dccd4-0c05-4535-a13f-8dda34662848
     │   └─ [+] 918057b8-b0c3-4ea5-a92a-6da5569920f5
     ├─ [+] Added: .testefs.efs.dns
     └─ [+] Added: .testefs.efs.file_system_id



```

This command confirmed the change and CDK will proceed to the EFS creation once you will run the actual `deploy` command:

```
$ ./idea-admin.sh deploy shared-storage \
   --cluster-name <CLUSTER_NAME> \
   --aws-region <REGION> \ 
   --upgrade
deploying module: shared-storage, module id: shared-storage

✨  Synthesis time: 19.54s

<CLUSTER_NAME>-shared-storage: building assets...

[0%] start: Building d70814031a62eca4c91303efaad90b00703c3d9adbcc3b64c4f3de07322adf24:<REDACTED>-us-east-2
[100%] success: Built d70814031a62eca4c91303efaad90b00703c3d9adbcc3b64c4f3de07322adf24:<REDACTED>-us-east-2

<CLUSTER_NAME>-shared-storage: assets built

<CLUSTER_NAME>-shared-storage: deploying...
[0%] start: Publishing d70814031a62eca4c91303efaad90b00703c3d9adbcc3b64c4f3de07322adf24:<REDACTED>-us-east-2
[100%] success: Published d70814031a62eca4c91303efaad90b00703c3d9adbcc3b64c4f3de07322adf24:<REDACTED>-us-east-2
<CLUSTER_NAME>-shared-storage: creating CloudFormation changeset...
<CLUSTER_NAME>-shared-storage | 0/7 | 7:04:12 PM | UPDATE_IN_PROGRESS   | AWS::CloudFormation::Stack | <CLUSTER_NAME>-shared-storage User Initiated
<CLUSTER_NAME>-shared-storage | 0/7 | 7:04:17 PM | CREATE_IN_PROGRESS   | AWS::EFS::FileSystem    | testefs-storage-efs (testefsstorageefs)
<CLUSTER_NAME>-shared-storage | 0/7 | 7:04:18 PM | CREATE_IN_PROGRESS   | AWS::EFS::FileSystem    | testefs-storage-efs (testefsstorageefs) Resource creation Initiated
<CLUSTER_NAME>-shared-storage | 1/7 | 7:04:22 PM | CREATE_COMPLETE      | AWS::EFS::FileSystem    | testefs-storage-efs (testefsstorageefs)
<CLUSTER_NAME>-shared-storage | 1/7 | 7:04:23 PM | CREATE_IN_PROGRESS   | AWS::EFS::MountTarget   | testefs-storage-efs/testefs-storage-efs-mount-target-3 (testefsstorageefstestefsstorageefsmounttarget32982F891)
<CLUSTER_NAME>-shared-storage | 1/7 | 7:04:23 PM | CREATE_IN_PROGRESS   | AWS::EFS::MountTarget   | testefs-storage-efs/testefs-storage-efs-mount-target-2 (testefsstorageefstestefsstorageefsmounttarget20E352012)
<CLUSTER_NAME>-shared-storage | 1/7 | 7:04:23 PM | CREATE_IN_PROGRESS   | AWS::EFS::MountTarget   | testefs-storage-efs/testefs-storage-efs-mount-target-1 (testefsstorageefstestefsstorageefsmounttarget15EF95F6B)
<CLUSTER_NAME>-shared-storage | 1/7 | 7:04:23 PM | UPDATE_IN_PROGRESS   | Custom::ClusterSettings | <CLUSTER_NAME>-shared-storage-settings/Default (ideapatchusharedstoragesettings)
<CLUSTER_NAME>-shared-storage | 1/7 | 7:04:25 PM | CREATE_IN_PROGRESS   | AWS::EFS::MountTarget   | testefs-storage-efs/testefs-storage-efs-mount-target-2 (testefsstorageefstestefsstorageefsmounttarget20E352012) Resource creation Initiated
<CLUSTER_NAME>-shared-storage | 1/7 | 7:04:25 PM | CREATE_IN_PROGRESS   | AWS::EFS::MountTarget   | testefs-storage-efs/testefs-storage-efs-mount-target-1 (testefsstorageefstestefsstorageefsmounttarget15EF95F6B) Resource creation Initiated
<CLUSTER_NAME>-shared-storage | 2/7 | 7:04:27 PM | UPDATE_COMPLETE      | Custom::ClusterSettings | <CLUSTER_NAME>-shared-storage-settings/Default (ideapatchusharedstoragesettings)
<CLUSTER_NAME>-shared-storage | 2/7 | 7:04:32 PM | CREATE_IN_PROGRESS   | AWS::EFS::MountTarget   | testefs-storage-efs/testefs-storage-efs-mount-target-3 (testefsstorageefstestefsstorageefsmounttarget32982F891) Resource creation Initiated
2/7 Currently in progress: <CLUSTER_NAME>-shared-storage, testefsstorageefstestefsstorageefsmounttarget32982F891, testefsstorageefstestefsstorageefsmounttarget20E352012, testefsstorageefstestefsstorageefsmounttarget15EF95F6B
<CLUSTER_NAME>-shared-storage | 3/7 | 7:05:50 PM | CREATE_COMPLETE      | AWS::EFS::MountTarget   | testefs-storage-efs/testefs-storage-efs-mount-target-3 (testefsstorageefstestefsstorageefsmounttarget32982F891)
<CLUSTER_NAME>-shared-storage | 4/7 | 7:05:58 PM | CREATE_COMPLETE      | AWS::EFS::MountTarget   | testefs-storage-efs/testefs-storage-efs-mount-target-1 (testefsstorageefstestefsstorageefsmounttarget15EF95F6B)
<CLUSTER_NAME>-shared-storage | 5/7 | 7:05:59 PM | CREATE_COMPLETE      | AWS::EFS::MountTarget   | testefs-storage-efs/testefs-storage-efs-mount-target-2 (testefsstorageefstestefsstorageefsmounttarget20E352012)
<CLUSTER_NAME>-shared-storage | 6/7 | 7:06:00 PM | UPDATE_COMPLETE_CLEA | AWS::CloudFormation::Stack | <CLUSTER_NAME>-shared-storage
<CLUSTER_NAME>-shared-storage | 7/7 | 7:06:01 PM | UPDATE_COMPLETE      | AWS::CloudFormation::Stack | <CLUSTER_NAME>-shared-storage

 ✅  <CLUSTER_NAME>-shared-storage

✨  Deployment time: 124.62s

Stack ARN:
arn:aws:cloudformation:us-east-2:<REDACTED>:stack/<CLUSTER_NAME>-shared-storage/b7ada700-73d6-11ed-a507-0645fa8e8430

✨  Total time: 144.16s

```

Now that the deployment command is complete, go back to the web interface and validate the new EFS has been created and now has valid FileSystem ID assigned.

<figure><img src=".gitbook/assets/Screen Shot 2022-12-05 at 7.41.19 PM.png" alt=""><figcaption><p>New mount now has a valid FileSystem ID</p></figcaption></figure>

To further validate our new mount point, we can submit a test job which will output `df` command

`qsub -- /bin/df -h`

The job output should display the mount point (custom/path) for your new filesystem

```
Filesystem                                          Size  Used Avail Use% Mounted on
devtmpfs                                            1.9G     0  1.9G   0% /dev
tmpfs                                               1.9G     0  1.9G   0% /dev/shm
tmpfs                                               1.9G  408K  1.9G   1% /run
tmpfs                                               1.9G     0  1.9G   0% /sys/fs/cgroup
/dev/nvme0n1p1                                       10G  3.2G  6.9G  32% /
fs-0175f5f6e34dd73ee.efs.us-east-2.amazonaws.com:/  8.0E  278M  8.0E   1% /apps
fs-0782abfc0e273d46d.efs.us-east-2.amazonaws.com:/  8.0E     0  8.0E   0% /data
fs-01db45fc6a9eaf20a.efs.us-east-2.amazonaws.com:/  8.0E     0  8.0E   0% /custom_path
```

### Attach existing File System

To generate configurations for attaching an existing file system, you can use the idea-admin.sh shared-storage attach-file-system command as below. This utility will automatically search for existing backed storage (FSx for Lustre/NetApp/OpenZFS/Windows, EFS) running in your VPC.

| <pre><code> ./idea-admin.sh shared-storage attach-file-system --help
Usage: idea-admin shared-storage attach-file-system [OPTIONS]

  attach existing shared-storage file-system

Options:
  --cluster-name TEXT  Cluster Name
  --aws-region TEXT    AWS Region  [required]
  --aws-profile TEXT   AWS Profile Name
  --kms-key-id TEXT    KMS Key ID
  -h, --help           Show this message and exit.
</code></pre> |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |

**Example**

| <pre class="language-bash"><code class="lang-bash">$  ./idea-admin.sh shared-storage attach-file-system \
   --aws-region &#x3C;REGION> \
   --cluster-name &#x3C;CLUSTER_NAME>
Add Shared Storage to an IDEA Cluster

Shared Storage Settings

? [Name] Enter the name of the shared storage file system  (Must be all lower case, no spaces or special characters) demo
? [Title] Enter a friendly title for the file system Demo FS
? [VPC] Select the VPC from which an existing file system can be used vpc-0cb462f0bfc14526b (10.0.0.0/16) [&#x3C;CLUSTER_NAME>-vpc]
? [Shared Storage Provider] Select a provider for the shared storage file system Amazon FSx for Lustre
? [Mount Directory] Location of the mount directory. eg. /my-mount-dir /demo
? [Mount Scopes] Select the mount scope for file system Cluster

Existing FSx for Lustre Settings

? [Existing FSx for Lustre] Select an existing Lustre file system fsx-lustre (FileSystemId: fs-01a2ccc035f0f007c, Provider: fsx_lustre)
? [Mount Options] Enter /etc/fstab mount options lustre defaults,noatime,flock,_netdev 0 0

Shared Storage Config -----------------------------------------------------------------------------------------------------------------

demo:
  title: Demo FS
  provider: fsx_lustre
  scope:
  - cluster
  mount_dir: /demo
  mount_options: lustre defaults,noatime,flock,_netdev 0 0
  fsx_lustre:
    use_existing_fs: true
    file_system_id: fs-01a2ccc035f0f007c
    dns: fs-01a2ccc035f0f007c.fsx.us-east-1.amazonaws.com
    mount_name: drohpbev
    version: '2.10'

----------------------------------------------------------------------------------------------------------------------------------------
? How do you want to proceed further?
</code></pre> |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |

## Remove a File System

Run `./idea-admin.sh config delete shared-storage.<filesystem_name>` to remove a shared filesystem from IDEA.

```
./idea-admin.sh config delete shared-storage.testefs \
 --cluster-name <CLUSTER_NAME> \
 --aws-region <REGION_NAME>

searching for config entries with prefix: shared-storage.testefs
found 14 config entries matching: shared-storage.testefs
deleting config entry - shared-storage.testefs.efs.performance_mode = generalPurpose
deleting config entry - shared-storage.testefs.efs.dns = fs-01db45fc6a9eaf20a.efs.us-east-2.amazonaws.com
deleting config entry - shared-storage.testefs.efs.throughput_mode = bursting
deleting config entry - shared-storage.testefs.efs.transition_to_ia = None
deleting config entry - shared-storage.testefs.title = "New Shared EFS for Project A"
deleting config entry - shared-storage.testefs.mount_options = nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0
deleting config entry - shared-storage.testefs.efs.removal_policy = DESTROY
deleting config entry - shared-storage.testefs.mount_dir = /custom_path
deleting config entry - shared-storage.testefs.efs.kms_key_id = None
deleting config entry - shared-storage.testefs.efs.encrypted = True
deleting config entry - shared-storage.testefs.provider = efs
deleting config entry - shared-storage.testefs.efs.file_system_id = fs-01db45fc6a9eaf20a
deleting config entry - shared-storage.testefs.efs.cloudwatch_monitoring = False
deleting config entry - shared-storage.testefs.scope = ['cluster']
deleted 14 config entries
```

{% hint style="info" %}
Removing a file system from IDEA won't trigger a file system deletion. Make sure to re-deploy the `shared-storage` module if you want to remove a filesystem previously created by IDEA
{% endhint %}

## Shared Storage Providers

### Amazon EFS

#### New EFS Configuration

| <pre><code>demo:
  title: Demo FS
  provider: efs
  scope:
  - cluster
  mount_dir: /demo
  mount_options: nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0
  efs:
    kms_key_id: ~
    encrypted: true
    throughput_mode: bursting
    performance_mode: generalPurpose
    removal_policy: DESTROY
    cloudwatch_monitoring: false
    transition_to_ia: ~
</code></pre> |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |

#### Existing EFS Configuration

| <pre><code>demo:
  title: Demo FS
  provider: efs
  scope:
    - cluster
  mount_dir: /demo
  mount_options: nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0
  efs:
    use_existing_fs: true
    file_system_id: fs-05b067d4a78e0fb29
    dns: fs-05b067d4a78e0fb29.efs.us-east-1.amazonaws.com
    encrypted: true
</code></pre> |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |

### Amazon FSx for Lustre

#### Existing FSx for Lustre Configuration

| <pre><code>demo:
  title: Demo FS
  provider: fsx_lustre
  scope:
    - cluster
  mount_dir: /demo
  mount_options: lustre defaults,noatime,flock,_netdev 0 0
  fsx_lustre:
    use_existing_fs: true
    file_system_id: fs-01a2ccc035f0f007c
    dns: fs-01a2ccc035f0f007c.fsx.us-east-1.amazonaws.com
    mount_name: drohpbev
    version: '2.10'
</code></pre> |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |

### Amazon FSx for NetApp ONTAP

#### Existing FSx for NetApp ONTAP

| <pre><code>demo:
  title: Demo FS
  provider: fsx_netapp_ontap
  scope:
    - cluster
  mount_drive: Z
  mount_dir: /demo
  mount_options: nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0
  fsx_netapp_ontap:
    use_existing_fs: true
    file_system_id: fs-09753a84872d3209b
    svm:
      svm_id: svm-064990494a2dbd4c2
      smb_dns: IDEA-DEV-SVM1.IDEA.LOCAL
      nfs_dns: svm-064990494a2dbd4c2.fs-09753a84872d3209b.fsx.us-east-1.amazonaws.com
      management_dns: svm-064990494a2dbd4c2.fs-09753a84872d3209b.fsx.us-east-1.amazonaws.com
      iscsi_dns: iscsi.svm-064990494a2dbd4c2.fs-09753a84872d3209b.fsx.us-east-1.amazonaws.com
    volume:
      volume_id: fsvol-0f791716b33592fff
      volume_path: /
      security_style: MIXED
</code></pre> |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |

### Amazon FSx for OpenZFS

#### Existing FSx for OpenZFS

| <pre><code>demo:
  title: Demo FS
  provider: fsx_openzfs
  scope:
    - cluster
  mount_dir: /demo
  mount_options: nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,timeo=600 0 0
  fsx_openzfs:
    use_existing_fs: true
    file_system_id: fs-09e1b30aab982aa34
    dns: fs-09e1b30aab982aa34.fsx.us-east-1.amazonaws.com
    volume_id: fsvol-00d420eeb064ac36a
    volume_path: /fsx
</code></pre> |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |

### Amazon FSx for Windows File Server

#### Existing FSx for Windows File Server

| <pre><code>demo:
  title: Demo FS
  provider: fsx_windows_file_server
  scope:
    - cluster
  mount_drive: Z
  fsx_windows_file_server:
    use_existing_fs: true
    file_system_id: fs-0c1f74968df26462e
    dns: amznfsx0wallrm9.idea.local
    preferred_file_server_ip: 10.0.113.174
</code></pre> |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |

## Visualize Cluster Settings

Shared storage settings can be viewed via **Web Portal** and **IDEA** **CLI**.

### Web Portal

Navigate to "**Cluster Management**" > "**Settings**" > "**Shared Storage**"

<figure><img src=".gitbook/assets/Screen Shot 2022-12-05 at 4.57.08 PM.png" alt=""><figcaption></figcaption></figure>

### **IDEA CLI**

| <pre><code>$ ./idea-admin.sh config show --query "shared-storage.*" \
   --cluster-name &#x3C;CLUSTER_NAME> \
  --aws-region &#x3C;REGION> \
   --format yaml
   
shared-storage: 
  apps: 
    efs: 
      cloudwatch_monitoring: true
      dns: fs-07db0063b17b52abd.efs.us-east-1.amazonaws.com
      encrypted: true
      file_system_id: fs-07db0063b17b52abd
      kms_key_id: null
      performance_mode: generalPurpose
      removal_policy: DESTROY
      throughput_mode: bursting
      transition_to_ia: null
    mount_dir: /apps
    provider: efs
  data: 
    efs: 
      cloudwatch_monitoring: false
      dns: fs-0de603380ef4bb8a0.efs.us-east-1.amazonaws.com
      encrypted: true
      file_system_id: fs-0de603380ef4bb8a0
      kms_key_id: null
      performance_mode: generalPurpose
      removal_policy: DESTROY
      throughput_mode: bursting
      transition_to_ia: AFTER_30_DAYS
    mount_dir: /data
    provider: efs
  demo: 
    efs: 
      dns: fs-05b067d4a78e0fb29.efs.us-east-1.amazonaws.com
      encrypted: true
      file_system_dns: fs-05b067d4a78e0fb29.efs.us-east-1.amazonaws.com
      file_system_id: fs-05b067d4a78e0fb29
      use_existing_fs: true
    mount_dir: /demo
    mount_options: nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0
    provider: efs
    scope: 
      - cluster
    title: Demo FS
  deployment_id: 9cd9615d-03d0-4402-996e-b78c5a13b660
  lustre: 
    fsx_lustre: 
      dns: fs-01a2ccc035f0f007c.fsx.us-east-1.amazonaws.com
      file_system_id: fs-01a2ccc035f0f007c
      mount_name: drohpbev
      use_existing_fs: true
      version: 2.10
    mount_dir: /fsx-lustre
    mount_options: lustre defaults,noatime,flock,_netdev 0 0
    provider: fsx_lustre
    scope: 
      - cluster
    title: FSx Lustre Demo
  ontap: 
    fsx_netapp_ontap: 
      file_system_id: fs-08b38f09448c07cb2
      svm: 
        iscsi_dns: iscsi.svm-0132d31f6667399a7.fs-08b38f09448c07cb2.fsx.us-east-1.amazonaws.com
        management_dns: svm-0132d31f6667399a7.fs-08b38f09448c07cb2.fsx.us-east-1.amazonaws.com
        nfs_dns: svm-0132d31f6667399a7.fs-08b38f09448c07cb2.fsx.us-east-1.amazonaws.com
        smb_dns: IDEA-DEV-SVM1.idea.local
        svm_id: svm-0132d31f6667399a7
      use_existing_fs: true
      volume: 
        security_style: MIXED
        volume_id: fsvol-0d4e5200c01a7b533
        volume_path: /vol1
    mount_dir: /fsx-netapp-ontap
    mount_drive: Z
    mount_options: nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0
    provider: fsx_netapp_ontap
    scope: 
      - cluster
    title: FSx for NetApp ONTAP
  openzfs: 
    fsx_openzfs: 
      dns: fs-09e1b30aab982aa34.fsx.us-east-1.amazonaws.com
      file_system_id: fs-09e1b30aab982aa34
      use_existing_fs: true
      volume_id: fsvol-00d420eeb064ac36a
      volume_path: /fsx
    mount_dir: /fsx-openzfs
    mount_options: nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport 0 0
    provider: fsx_openzfs
    scope: 
      - cluster
    title: FSx for OpenZFS
  security_group_id: sg-0ad8671840e2ed4e6
  windows_file_server: 
    fsx_windows_file_server: 
      dns: amznfsx0wallrm9.idea.local
      file_system_id: fs-0c1f74968df26462e
      preferred_file_server_ip: 10.0.113.174
      use_existing_fs: true
    mount_drive: P
    projects: 
      - default
    provider: fsx_windows_file_server
    scope: 
      - project
    title: FSx Windows File Server

</code></pre> |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |

\
