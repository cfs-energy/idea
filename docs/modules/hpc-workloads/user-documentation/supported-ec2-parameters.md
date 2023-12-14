# Supported EC2 parameters

IDEA made job submission on EC2 very easy and is fully integrated with EC2. Below is a list of parameters you can specify when you request your simulation to ensure the hardware provisioned will exactly match your simulation requirements.

## Compute <a href="#compute" id="compute"></a>

#### **base\_os**

* Description: Reference to the base OS of the AMI you are using
* Allowed Values: `amazonlinux2` `centos7` `rhel7`
* Default: If not specified, value default to the OS of the install AMI
* Examples:
  * `-l base_os=centos7`: Instances provisioned will be deployed against CentOS manifest

#### **ht\_support**

_Disabled by default_

* Description: Enable support for hyper-threading
* Allowed Values: `yes` `true` `no` `false` (case insensitive)
* Examples:
  * `-l ht_support=True`: Enable hyper-threading for all instances
  * `-l ht_support=False`: Disable hyper-threading for all instances (default)

#### **instance\_ami**

* Description: Reference to a custom AMI you want to use
* Default: If not specified, value default to the AMI specified during installation
* Examples:
  * `-l instance_ami=ami-abcde123`: Capacity provisioned for the job will use the specific AMI

{% hint style="info" %}
If you are planning to use an AMI which is _not using the same OS_ as the scheduler, you will need to specify `base_os` parameter
{% endhint %}

#### **instance\_profile**

* Description: Reference to a custom IAM role to use. Make sure to specify the Instance Profile name and not IAM role name. Refer to the pre-requisites below before using custom IAM role.
* Default: Use the default IAM instance profile configured by IDEA
* Examples:
  * `-l instance_profile=CustomInstanceProfileName`: Compute nodes will a custom IAM Role.

{% hint style="warning" %}
Pre-requisites

[Click here to learn how to enable instance\_profile option](https://awslabs.github.io/scale-out-computing-on-aws/security/use-custom-sgs-roles/)
{% endhint %}

#### **instance\_type**

* Description: The type of instance to provision for the simulation
* Examples:
  * `-l instance_type=c5.large`: Provision a c5.large for the simulation
  * `-l instance_type=c5.large+m5.large`: Provision c5.large and m5.large (if needed) for the simulation.

{% hint style="info" %}
You can specify multiple instances type using "+" sign. When using more than 1 instance type, AWS will prioritize the capacity based on the order (eg: launch c5.large first and switch to m5.large if AWS can't provision c5.large anymore)
{% endhint %}

#### **nodes**

* Description:The number of EC2 instance to provision
* Examples:
  * `-l nodes=5`: Provision 5 EC2 instances

#### **force\_ri**

* Description: Restrict a job to run on [Reserved Instance](https://aws.amazon.com/ec2/pricing/reserved-instances/)
* Allowed Values: `True` `False`
* Default: `False`
* Examples:
  * `-l force_ri=False`: Job can use RI, On-Demand or Spot
  * `-l force_ri=True`: Job will only use Reserved Instance. Job will stay in the queue if there is not enough reserved instance available

#### **security\_groups**

* Description: Attach additional security groups (max 4) to the compute nodes
* Allowed Values: `sg-xxxxxx`
* Default: `False`
* Examples:
  * `-l security_groups=sg-abcde`: Will attach `sg-abcde` on top of the default existing security group (ComputeNodeSG)
  * `-l security_groups=sg-abcd+sg-efgh`: Will attach `sg-abcde` and `sg-efgh` on top of the default existing security group (ComputeNodeSG)

{% hint style="warning" %}
Pre-requisites

[Click here to learn how to enable security\_groups option](https://awslabs.github.io/scale-out-computing-on-aws/security/use-custom-sgs-roles/)
{% endhint %}

{% hint style="info" %}
You can specify a maximum of 4 additional security groups
{% endhint %}

#### **spot\_allocation\_count**

* Description: Specify the number of SPOT instances to launch when provisioning both OD (On Demand) and SPOT instances
* Allowed Values: Integer
* Examples:
  * `-l nodes=10 -l spot_price=auto -l spot_allocation_count=8`: Provision 10 instances, 2 OD and 8 SPOT with max spot price capped to OD price
  * `-l nodes=10 -l spot_price=1.4 -l spot_allocation_count=5`: Provision 10 instances, 5 OD and 5 SPOT with max spot price set to $1.4
  * `-l nodes=10 -l spot_price=auto`: Only provision SPOT instances
  * `-l nodes=10`: Only provision OD instances

{% hint style="info" %}
This parameter is ignored if `spot_price` is not specified `spot_allocation_count` must be lower that the total number of nodes you are requesting (eg: you can not do `-l nodes=5 -l spot_allocation_count=15`)
{% endhint %}

#### **spot\_allocation\_strategy**

* Description: Choose allocation strategy when using multiple SPOT instances type
* Allowed Valuess: `capacity-optimized` or `lowest-price` or `diversified` (only for SpotFleet deployments)
* Default Value: `capacity-optimized`
* Examples:
  * `-l spot_allocation_strategy=capacity-optimized`: AWS will provision Spot compute nodes for both EC2 Auto Scaling and EC2 Fleet from the most-available Spot Instance pools by analyzing capacity metrics.

#### **spot\_price**

* Description: Enable support for SPOT instances
* Allowed Values: any float value or `auto`
* Examples:
  * `-l spot_price=auto`: Max price will be capped to the On-Demand price
  * `-l spot_price=1.4`: Max price you are willing to pay for this instance will be $1.4 an hour.

{% hint style="info" %}
`spot_price` is capped to On-Demand price (e.g: Assuming you are provisioning a t3.medium, AWS will default maximum spot price to 0.418 (OD price) even though you specified `-l spot_price=15`)
{% endhint %}

#### **subnet\_id**

* Description: Reference to a subnet ID to use
* Default: If not specified, value default to one of the three private subnets created during installation
* Examples:
  * `-l subnet_id=sub-123`: Will provision capacity on sub-123 subnet
  * `-l subnet_id=sub-123+sub-456+sub-789`: + separated list of private subnets. Specifying more than 1 subnet is useful when requesting large number of instances
  * `-l subnet_id=2`: IDEA will provision capacity in 2 private subnets chosen randomly

{% hint style="info" %}
If you specify more than 1 subnet and have `placement_group` set to True, IDEA will automatically provision capacity and placement group on the first subnet from the list
{% endhint %}

## Storage <a href="#storage" id="storage"></a>

{% hint style="info" %}
[Refer to the Shared-Storage module](https://docs.ide-on-aws.com/shared-storage/introduction) if you are looking for more persistent / non-job specify storage backed.
{% endhint %}

### EBS <a href="#ebs" id="ebs"></a>

#### **keep\_ebs**

_Disabled by default_

* Description: Retain or not the EBS disks once the simulation is complete
* Allowed Values: `yes` `true` `false` `no` (case insensitive)
* Default Value: `False`
* Example:
  * `-l keep_ebs=False`: (Default) All EBS disks associated to the job will be deleted
  * `-l keep_ebs=True`: Retain EBS disks after the simulation has terminated (mostly for debugging/troubleshooting procedures)

#### **root\_size**

* Description: Define the size of the local root volume
* Unit: GB
* Example: `-l root_size=300`: Provision a 300 GB SSD disk for `/` (either `sda1` or `xvda1`)

#### **scratch\_size**

* Description: Define the size of the local root volume
* Unit: GB
* Example: `-l scratch_size=500`: Provision a 500 GB SSD disk for `/scratch`

{% hint style="info" %}
scratch disk is automatically mounted on all nodes associated to the simulation under `/scratch`
{% endhint %}

#### **instance\_store**

{% hint style="info" %}
* IDEA automatically mount instance storage when available.
* [For instances having more than 1 volume, SOCA will create a raid device](https://awslabs.github.io/scale-out-computing-on-aws/storage/backend-storage-options/#instance-store-partition)
* In all cases, instance store volumes will be mounted on `/scratch`
{% endhint %}

#### **scratch\_iops**

* Description: Define the number of provisioned IOPS to allocate for your `/scratch` device
* Unit: IOPS
* Example: `-l scratch_iops=3000`: Your EBS disks provisioned for `/scratch` will have 3000 dedicated IOPS

{% hint style="info" %}
It is recommended to set the IOPs to 3x storage capacity of your EBS disk
{% endhint %}

### FSx for Lustre <a href="#fsx-for-lustre" id="fsx-for-lustre"></a>

#### **fsx\_lustre**

**WITH NO S3 BACKEND**

* Example: `-l fsx_lustre=True`: Create a new FSx for Lustre and mount it accross all nodes

{% hint style="info" %}
* FSx partitions are mounted as `/fsx`. This can be changed if needed
* If `fsx_lustre_size` is not specified, default to 1200 GB
{% endhint %}

**WITH S3 BACKEND**

* Example: `-l fsx_lustre=my-bucket-name` or `-l fsx_lustre=s3://my-bucket-name` : Create a new FSx for Lustre and mount it across all nodes

{% hint style="info" %}
* FSx partitions are mounted as `/fsx`. This can be changed if needed
* [You need to give IAM permission first](https://awslabs.github.io/scale-out-computing-on-aws/storage/launch-job-with-fsx/#how-to-provision-an-ephemeral-fsx-with-s3-backend)
* If not specified, IDEA automatically prefix your bucket name with `s3://`
* If `fsx_lustre_size` is not specified, default to 1200 GB
* [You can configure custom ImportPath and ExportPath](https://awslabs.github.io/scale-out-computing-on-aws/storage/launch-job-with-fsx/#setup)
{% endhint %}

**MOUNT EXISTING FSX**

* Description: Mount an existing FSx to all compute nodes if `fsx_lustre` points to a FSx filesystem's DNS name
* Example: `-l fsx_lustre=fs-xxxx.fsx.region.amazonaws.com`

{% hint style="info" %}
* FSx partitions are mounted as `/fsx`. This can be changed if needed
* Make sure your FSx for Luster configuration is correct (use IDEA VPC and correct IAM roles)
* [Make sure to use the Filesytem's DNS name](https://awslabs.github.io/scale-out-computing-on-aws/storage/launch-job-with-fsx/#how-to-connect-to-a-permanentexisting-fsx)
{% endhint %}

#### **fsx\_lustre\_size**

* Description: Create an ephemeral FSx for your job and mount the S3 bucket specified
* Unit: GB
* Example: `-l fsx_lustre_size=3600`: Provision a 3.6TB EFS disk

{% hint style="info" %}
If `fsx_lustre_size` is not specified, default to 1200 GB (smallest size supported)
{% endhint %}

{% hint style="warning" %}
This parameter is ignored unless you have specified `fsx_lustre=True`
{% endhint %}

#### **fsx\_lustre\_deployment\_type**

* Description: Choose what type of FSx for Lustre you want to deploy
* Allowed Valuess: `SCRATCH_1` `SCRATCH_2` `PERSISTENT_1` (case insensitive)
* Default Value: `SCRATCH_2`
* Example: `-l fsx_lustre_deployment_type=scratch_2`: Provision a FSx for Lustre with SCRATCH\_2 type

{% hint style="info" %}
If `fsx_lustre_size` is not specified, default to 1200 GB (smallest size supported)
{% endhint %}

{% hint style="warning" %}
This parameter is ignored unless you have specified `fsx_lustre=True`
{% endhint %}

#### **fsx\_lustre\_per\_unit\_throughput**

* Description: Select the baseline disk throughput available for that file system
* Allowed Values: `50` `100` `200`
* Unit: MB/s
* Example: `-l fsx_lustre_per_unit_throughput=250`:

{% hint style="info" %}
Per Unit Throughput is only avaible when using `PERSISTENT_1` FSx for Lustre
{% endhint %}

{% hint style="warning" %}
This parameter is ignored unless you have specified `fsx_lustre=True`
{% endhint %}

## Network <a href="#network" id="network"></a>

#### **efa\_support**

* Description: Enable EFA support
* Allowed Values: yes, true, True
* Example: `-l efa_support=True`: Deploy an EFA device on all the nodes

{% hint style="info" %}
You must use an EFA compatible instance, otherwise your job will stay in the queue
{% endhint %}

#### **ht\_support**

_Disabled by default_

* Description: Enable support for hyper-threading
* Allowed Values: `yes` `true` (case insensitive)
* Example: `-l ht_support=True`: Enable hyper-threading for all instances

#### **placement\_group**

_Enabled by default_

* Description: Disable placement group
* Allowed Values: `yes` `true` (case insensitive)
* Example: `-l placement_group=True`: Instances will use placement groups

{% hint style="info" %}
Placement group is enabled by default as long as the number of nodes provisioned is greater than 1
{% endhint %}

### Others <a href="#others" id="others"></a>

#### **system\_metrics**

_Default to False_

* Description: Send host level metrics to your OpenSearch (formerly Elasticsearch) backend
* Allowed Values: `yes` `no` `true` `false` (case insensitive)
* Example: `-l system_metrics=False`

{% hint style="danger" %}
Enabling system\_metrics generate a lot of data (especially if you are tracking 1000s of nodes). If needed, [you can add more storage to your AWS OpenSearch (formerly Elasticsearch) cluster](https://aws.amazon.com/premiumsupport/knowledge-center/add-storage-elasticsearch/)
{% endhint %}

#### **anonymous\_metrics**

_Default to the value specified during SOCA installation_

* Description: [Send anonymous operational metrics to AWS](https://docs.aws.amazon.com/solutions/latest/scale-out-computing-on-aws/appendix-d.html)
* Allowed Values: `yes` `true` `no` `false` (case insensitive)
* Example: `-l anonymous_metrics=True`

