# Queue Profiles

IDEA let you create queues and queue profiles. To access this section, click "**Scale-Out Compute**" > "**Queue Profiles**" on the left sidebar

{% hint style="info" %}
You must have admin permission to the Scale-Out Compute module
{% endhint %}

<figure><img src="../.gitbook/assets/Screen Shot 2022-11-01 at 9.35.16 PM.png" alt=""><figcaption><p>Default queue profiles</p></figcaption></figure>

A queue is is a resource that can handle and execute user jobs. A queue profile extends queue functionalities by specifying default compute/storage values. You can have multiple queues registered to one queue profile.

Each profile have a set of default values (instance\_type, instance\_ami ...) that are automatically applied to the simulation jobs if not specified by the users during the job submission.

Example1:

```bash
qsub -q normal -- myscript.sh
```

In this example, no job resources are specified during the qsub command. IDEA will determine instance type, AMI and all other required parameters based on the default values specified on [#instance-info](queue-profiles.md#instance-info "mention") for the queue called "normal".

Example 2:&#x20;

```
qsub -q normal \
     -l instance_type=c5.xlarge \
     -l instance_ami=ami-abcde123 \
     -l scratch_size=500 \
     myscript.sh
```

In this example, IDEA honors the job resources specified by the user (`instance_type`, `instance_ami` and `scratch_size`) Other required parameters will be based on the default values specified on [#instance-info](queue-profiles.md#instance-info "mention") for the queue called "normal".

{% hint style="info" %}
As administrator, you can prevent users to specify specific job resources via [#restricted-parameters](queue-profiles.md#restricted-parameters "mention")
{% endhint %}

## Create a queue profile

To create a queue profile, click "**Create Queue Profile**" button.

## Edit a queue profile

To edit a queue profile, select the profile and click "**Action**" > "**Edit Queue Profile**"

## Enable/Disable a queue profile



## Queue Profile Parameters

### Basic Info

#### Name

Choose a name for the queue profile. Name must be all lowercase and should not contain spaces or special characters except (-)

#### Title

Choose a user friendly title for the queue profile

#### Projects

Select applicable projects for the queue profile. Refer to [[Cluster Manager](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/GtBrWw9T1qCJK2QCOTW2/ "mention") ](https://docs.ide-on-aws.com/cluster-manager/menu/projects-management)to learn more about how projects work.

### Operating Modes

#### Scheduler Queues

Select the queue(s) to add to this queue profile.&#x20;

{% hint style="info" %}
* A queue cannot belong to more than one queue profile
* IDEA will automatically create the queue if needed
{% endhint %}

#### Queue Mode

Select the queue(s) mode.&#x20;

* First-In First-Out (FIFO): Default, jobs are processed in the order they have been send to the queue
* License Optimized: IDEA will try to maximize licenses consumption and run as many job as possible based on license availabilities. Job order may not be honored.
* Fair Share: IDEA starts jobs based on your own fair share formula

#### Keep Forever

Choose whether or not you want the compute nodes to be automatically deleted once the simulation(s) are complete

#### Scaling Mode

Select the scaling more for the queue(s).

* Single Job: Run one job per compute node (EC2 machine)
* Batch: Run multiple jobs per compute node (EC2 machine)

### Queue Limits

#### Max Running Jobs

Select the number of maximum concurrent running jobs (0 implies no limits)

#### Ma Provisioned Instances

Select the number of maximum concurrent provisioned instances (0 implies no limits)

### Queue ACLs

#### Allow Job Submissions without Project

Select whether or not you want to allow your users to submit their simulation jobs without specify a project (-P \<project\_name>)

#### Allowed Instance Types

List of instance type or instance families allowed to be provisioned by your users for their jobs.

Example:&#x20;

* c5.large,m5: End users can provision a c5.large or any m5 instance types (e.g: m5.large, m5.xlarge ..) for their jobs.

#### Excluded Instance Types

List of instance type or instance families your users are not authorized to provision for their jobs.

Example:&#x20;

* c5.large,m5: End users are not authorized to provision c5.large or any m5 instance types (e.g: m5.large, m5.xlarge ..) for their jobs.

#### Restricted Parameters

List of jobs parameters your users are not able to customize.

Example:

* scratch\_size, instance\_type: End users are not authorized to override `scratch_size` and `instance_type` parameters for their jobs and have to use the defaults value specified at the queue profile

#### Allowed Security Groups

List of additional security group(s) users can override via [#security\_groups](../user-documentation/supported-ec2-parameters.md#security\_groups "mention") parameter.

{% hint style="info" %}
You can assign up to 4 additional security groups per job

You must ensure your security groups have been created with the correct TCP inbound/outbound rules. Failure to do so may prevent your jobs to start
{% endhint %}

#### Allowed Instance Profiles

List of additional IAM instance profile(s) users can override via [#instance\_profile](../user-documentation/supported-ec2-parameters.md#instance\_profile "mention") parameter.

{% hint style="info" %}
You must ensure your IAM role(s) have been created with the correct policies. Failure to do so may prevent your jobs to start
{% endhint %}

### Instance Info

#### Compute Node OS

#### Instance AMI

Choose the default AMI to use for the compute nodes.

Associated Job Resource: [#instance\_ami](../user-documentation/supported-ec2-parameters.md#instance\_ami "mention")

#### &#x20;Instance Types

Choose the default instance type to provision for the compute nodes. Multiple instance types if provided will be used as weighted capacities. Order is important and must be provided in increasing CPU capacity (e.g: c5.large, c5.xlarge)

Associated Job Resource: [#instance\_type](../user-documentation/supported-ec2-parameters.md#instance\_type "mention")

#### Root Storage Size

Select the size of the root partition for the compute nodes.

Associated Job Resource: [#root\_size](../user-documentation/supported-ec2-parameters.md#root\_size "mention")

#### Keep EBS Volumes ?

Choose whether or not you want to retain ephemeral EBS disks associated to the compute nodes once the simulation has completed.&#x20;

Associated Job Resource: [#keep\_ebs](../user-documentation/supported-ec2-parameters.md#keep\_ebs "mention")

#### Enable EFA Support ?

Choose whether or not you want to enable EFA support. You must be using a supported instance type ([https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html#efa-instance-types](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html#efa-instance-types))

Associated Job Resource: [#efa\_support](../user-documentation/supported-ec2-parameters.md#efa\_support "mention")

#### Enable Hyper-Threading ?

Choose whether or not you want to enable hyper threading

Associated Job Resource: [#ht\_support-1](../user-documentation/supported-ec2-parameters.md#ht\_support-1 "mention")

#### Force Reserved Instances ?

Choose whether or not you want to restrict your job to Reserved Instance

Associated Job Resource: [#force\_ri](../user-documentation/supported-ec2-parameters.md#force\_ri "mention")

### Spot Fleet

#### Use Spot Fleet?

Choose whether or not you want to enable support for Spot Fleet.

#### Spot Price

Select the maximum bid for your spot instances.

Associated Job Resource: [#spot\_price](../user-documentation/supported-ec2-parameters.md#spot\_price "mention")

#### Spot Allocation Count

Select the spot allocation logic for your spot instances.

Associated Job Resource: [#spot\_allocation\_count](../user-documentation/supported-ec2-parameters.md#spot\_allocation\_count "mention")

#### Spot Allocation Strategy

Select the spot allocation strategy for your spot instances.

Associated Job Resource: [#spot\_allocation\_strategy](../user-documentation/supported-ec2-parameters.md#spot\_allocation\_strategy "mention")

### Network and Security

#### Subnet IDs

If needed, restrict the default subnet ids that can be used to provision the compute resources. If not set, IDEA will randomly choose on of the privat subnets:

Associated Job Resource: [#subnet\_id](../user-documentation/supported-ec2-parameters.md#subnet\_id "mention")

#### Security Groups

Specify a custom security group if you do not want to use the default one created by IDEA

#### Instance Profile

Specify a custom IAM instance profile if you do not want to use the default one created by IDEA

#### Enable Placement Group?

Choose whether or not you want to enable placement group ([https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-groups.html](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-groups.html))

Associated Job Resource: [#placement\_group](../user-documentation/supported-ec2-parameters.md#placement\_group "mention")

### Scratch Storage

#### Enable Scratch Storage

Choose whether or not you want to automatically allocate a local scratch partition to the compute nodes.

Associated Job Resource: [#scratch\_size](../user-documentation/supported-ec2-parameters.md#scratch\_size "mention")

#### Scratch Provider

Choose what type of storage provider you want to use for your scratch partition (EBS, FSx ...)

Associated Job Resource: None if EBS, [#fsx\_lustre](../user-documentation/supported-ec2-parameters.md#fsx\_lustre "mention")/ [#fsx\_lustre\_deployment\_type](../user-documentation/supported-ec2-parameters.md#fsx\_lustre\_deployment\_type "mention") for FSx

#### Scratch Storage Size

Choose the size (in GB) of the /scratch partition you are about to provision.

Associated Job Resource: [#scratch\_size](../user-documentation/supported-ec2-parameters.md#scratch\_size "mention") / [#fsx\_lustre\_size](../user-documentation/supported-ec2-parameters.md#fsx\_lustre\_size "mention")

#### Scratch Storage Provisioned IOPS

Select the provisioned IOPs you want to allocate. We recommend to set this value as 3X the scratch size

Associated Job Resource: [#scratch\_iops](../user-documentation/supported-ec2-parameters.md#scratch\_iops "mention") for EBS / [#fsx\_lustre\_per\_unit\_throughput](../user-documentation/supported-ec2-parameters.md#fsx\_lustre\_per\_unit\_throughput "mention") for FSx

### Metrics

#### Enable System Metrics?

Select whether or not you want to enable system metrics collection

Asociated Job Resource: [#system\_metrics](../user-documentation/supported-ec2-parameters.md#system\_metrics "mention")

#### Enable Anonymous Metrics?

Select whether or not you want to anonymous metrics collection [https://docs.aws.amazon.com/solutions/latest/scale-out-computing-on-aws/collection-of-operational-metrics.html](https://docs.aws.amazon.com/solutions/latest/scale-out-computing-on-aws/collection-of-operational-metrics.html)

Associated Job Resource: [#anonymous\_metrics](../user-documentation/supported-ec2-parameters.md#anonymous\_metrics "mention")

