# Submit a job

{% hint style="info" %}
Things to know before you start

* Jobs start on average 5 minutes after submission (this value may differ depending on the number and type of compute resource you need to be provisioned). You can reduce this cold-start by pre-configuring your AMI
* Nodes are ephemeral and tie to a given job id. If needed, you can launch 'AlwaysOn' instances that will be running 24/7.
* If your simulation requires a lot of disk I/O, it's recommended to use high performance SSD-NVMe disks (using /scratch location) and not default $HOME path unless your are using FSxL as $HOME backend
{% endhint %}

{% hint style="success" %}
IDEA supports multiple way to submit jobs:

* Regular `qsub` (see below)
* Web based job submission: [create-web-based-job-submission-worfklows.md](../admin-documentation/create-web-based-job-submission-worfklows.md "mention")
* HTTP Rest API ([https://docs.ide-on-aws.com/idea/first-time-users/access-your-idea-cluster/apis)](https://docs.ide-on-aws.com/idea/first-time-users/access-your-idea-cluster/apis)
{% endhint %}

To get started, either deploy a Linux virtual desktop [Virtual Desktop Interface (VDI)](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/QthiamUzKn8KJLl0hYBf/ "mention") or connect to the Bastion host via [https://docs.ide-on-aws.com/idea/first-time-users/access-your-idea-cluster/ssh.](https://docs.ide-on-aws.com/idea/first-time-users/access-your-idea-cluster/ssh)

Once connected to your Linux box, create a simple text file and name it "job\_submit.que". See below for a simple template (you will be required to edit whatever is between \*\*)

```bash
#!/bin/bash
## BEGIN PBS SETTINGS: Note PBS lines MUST start with #
#PBS -N **your_job_name**
#PBS -V -j oe -o **your_job_name**.qlog
#PBS -P **your_project**
#PBS -q **your_queue**
#PBS -l nodes=**number_of_nodes_for_this_job**
## END PBS SETTINGS
## BEGIN ACTUAL CODE
** your code goes here **
## END ACTUAL CODE
```

### Run your job <a href="#run-your-job" id="run-your-job"></a>

Once connected to your Linux machine, run `qsub job_submit.que` to submit your job to the queue.

```bash
user@host:~$ qsub job_submit.que
3323.ip-10-10-10-28
```

If your qsub command succeed, you will receive an id for your job (3323 in this example). To get more information about this job, run `qstat -f 3323` (or `qstat -f 3323 -x` is the job is already terminated).\
Your job will start as soon as resources are available (usually within 5 minutes after job submission)

### List your job <a href="#delete-a-job-from-the-queue" id="delete-a-job-from-the-queue"></a>

To see the job in the queue, you can run `qstat` utility from your Linux machine. Alternatively, you can use the web-interface to see your active jobs via [#active-jobs](control-my-jobs.md#active-jobs "mention")

### Dry Run <a href="#delete-a-job-from-the-queue" id="delete-a-job-from-the-queue"></a>

IDEA automatically performs DryRun action to validate whether your job can run or not. DryRun checks for service quotas, EC2 typo etc&#x20;

<pre class="language-bash"><code class="lang-bash"><strong># Example1: Trying to submit a job with an invalid instance type
</strong><strong>$ qsub -l instance_type=fake_instance -- /bin/sleep 60
</strong>qsub: Job submission failed and cannot be queued: 

* Below parameters failed to be validated: 

[INVALID_EC2_INSTANCE_TYPE] ec2 instance_type is invalid: fake_instance

# Example2: Trying to submit a job with a very large number of nodes
[clusteradmin@ip-10-0-142-68 ~]$ qsub -l instance_type=m5.large  -l nodes=6000 -- /bin/sleep 60
qsub: Job submission failed and cannot be queued: 

* Job will not be provisioned due to below errors: 

[ServiceQuota] Following AWS Service Quota needs to be requested from AWS: 
+------------------------------------------------------------------+-----------------+---------------+----------------+
|                            QuotaName                             | Available vCPUs | Desired vCPUs | Consumed vCPUs |
+------------------------------------------------------------------+-----------------+---------------+----------------+
| Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances |        32       |     12000     |       24       |
+------------------------------------------------------------------+-----------------+---------------+----------------+
 Please contact administrator to request these AWS Service Quotas.
[EC2DryRunFailed] EC2 dry run failed for instance_type: m5.large, Err: An error occurred (ResourceCountExceeded) when calling the RunInstances operation: You have exceeded the number of resources allowed in a single call of this type


</code></pre>

### Delete a job from the queue <a href="#delete-a-job-from-the-queue" id="delete-a-job-from-the-queue"></a>

Run `qdel <job_id>` to remove a job from the queue or use the web interface ( [control-my-jobs.md](control-my-jobs.md "mention")). If the job was running, associated ephemeral capacity (EC2/Storage) resource will be terminated within a couple of minutes.

### Custom AWS scheduler resources (optional) <a href="#custom-aws-scheduler-resources-optional" id="custom-aws-scheduler-resources-optional"></a>

Here is a list of scheduler resources specially designed for workloads running on AWS [supported-ec2-parameters.md](supported-ec2-parameters.md "mention"). The line starting with -l (lowercase L) is meant to define scheduler resources which will be used by this job. Syntax is as follow:

* In a script: `#PBS -l parameter_name=parameter_value,parameter_name_2=parameter_value_2`
* Using qsub: `qsub -l parameter_name=parameter_value -l parameter_name_2=parameter_value_2 myscript.sh`

{% hint style="info" %}
If you do not specify any parameters, your job will use the default configure for your queue ( [queue-profiles.md](../admin-documentation/queue-profiles.md "mention"))
{% endhint %}

### Specify an EC2 Instance Type (optional) <a href="#specify-an-ec2-instance-type-optional" id="specify-an-ec2-instance-type-optional"></a>

IDEA supports all type of EC2 instance. If you don't specify it, job will use a default type which may not be optimal (eg: simulation is memory intensive but default EC2 is compute optimized) If you are not familiar with EC2 instances, take some time to review [https://aws.amazon.com/ec2/instance-types/](https://aws.amazon.com/ec2/instance-types/)

If you want to force utilization of a specific instance type (and not use the default one), simply change the line and modify instance\_type value\
`#PBS -l [existing_parameters...],instance_type=**instance_type_value**`

### Specify a license restriction (optional) <a href="#specify-a-license-restriction-optional" id="specify-a-license-restriction-optional"></a>

Refer to [configure-floating-license-resources.md](../admin-documentation/configure-floating-license-resources.md "mention") to learn more about licenses configuration

### Manage your application logs <a href="#manage-your-application-logs" id="manage-your-application-logs"></a>

PBS will automatically generate a .qlog file once the job is complete as shown below.\
`#PBS -V -j oe -o **your_job_name**.qlog`\
If you need more verbose log, we recommend you using STDERR/STDOUT redirection on your code

### How to submit and run multiple jobs on the same EC2 instance <a href="#how-to-submit-and-run-multiple-jobs-on-the-same-ec2-instance" id="how-to-submit-and-run-multiple-jobs-on-the-same-ec2-instance"></a>

IDEA includes a new queue named `job-shared` which allows multiple jobs to run on the same EC2 instance. To allow multiple jobs to run on the same instance, the jobs need to have the same values for the following four parameters:

* instance\_ami
* instance\_type
* ht\_support
* spot\_price

If the jobs have the same values, then the jobs can run on the same EC2 instance. If some of the jobs have different values for any of these parameters, then one or more instances will be provisioned for these jobs.

EC2 instance capacity for `job-shared` queue is dynamically provisioned and de-provisioned automatically similar to the `normal` queue. The provisioning is based on the total number of vCPUs (when `ht_support=true)` or the total number of cores (when `ht_support=false`) for all queued jobs. Instances are de-provisioned after all jobs running on the instance complete and the instance(s) become idle for `terminate_when_idle` minutes.

### Examples <a href="#examples" id="examples"></a>

**Run a simple script on 1 node using default settings on 'normal' queue**

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=1
## END PBS SETTINGS
cd $HOME
./script.sh >> my_output.log 2>&1
```

**Run a simple script on 1 node using default settings on 'normal' queue**

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=1
## END PBS SETTINGS
cd $HOME
./script.sh >> my_output.log 2>&1
```

**Run a simple MPI script on 3 nodes using custom EC2 instance type**

This job will use a 3 c5.18xlarge instances

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=3,instance_type=c5.18xlarge
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/bin/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# c5.18xlarge is 36 cores so -np is 36 * 3 hosts
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 108 script.sh > my_output.log
```

**Run a simple script on 3 nodes using custom License Restriction**

This job will only start if we have at least 4 Comsol Acoustic licenses available

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=3,instance_type=c5.18xlarge,comsol_lic_acoustic=4
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# c5.18xlarge is 36 cores so -np is 36 * 3 hosts
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 108 script.sh > my_output.log
```

**Run a simple script on 5 nodes using custom AMI**

This job will use a user-specified AMI ID

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=5,instance_type=c5.18xlarge,instance_ami=ami-123abcde
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# c5.18xlarge is 36 cores so -np is 36 * 5 hosts
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 180 script.sh > my_output.log
```

**Run a simple script on 5 nodes using custom AMI using a different OS**

This job will use a user-specified AMI ID which use a operating system different than the scheduler

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=5,instance_type=c5.18xlarge,instance_ami=ami-123abcde,base_os=centos7
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# c5.18xlarge is 36 cores so -np is 36 * 5 hosts
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 180 script.sh > my_output.log
```

**Run a simple script on 5 m5.24xlarge SPOT instances as long as instance price is lower than $2.5 per hour**

This job will use SPOT instances. Instances will be automatically terminated if BID price is higher than $2.5 / per hour per instance

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=5,instance_type=m5.24xlarge,spot_price=2.5
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# m5.24xlarge is 48 cores so -np is 48 * 5 hosts
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 240 script.sh > my_output.log
```

**Run a simple script on 5 m5.24xlarge SPOT instances as long as instance price is lower than OD price**

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=5,instance_type=m5.24xlarge,spot_price=auto
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# m5.24xlarge is 48 cores so -np is 48 * 5 hosts
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 240 script.sh > my_output.log
```

**Submit a job with EFA**

Make sure to use an instance type supported by EFA [https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html#efa-instance-types](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html#efa-instance-types)

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=5,instance_type=c5n.18xlarge,efa_support=true
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# c5n.18xlarge is 36 cores so -np is 36 * 5
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 180 script.sh > my_output.log
```

**Use 50 c5.xlarge for your job and fallback to m5.xlarge and r5.xlarge if capacity is not available**

AWS honors the instance order, so it will try to provision 50 c5.large first and fallback to m5.xlarge/r5.xlarge if needed (in case your account has instance limitation or AWS can't allocate more than X instance type on a given AZ/region). Ultimately, you may end up with the following configuration (but not limited to):

* 50 c5.xlarge
* 30 c5.xlarge, 20 m5.xlarge
* 20 c5.xlarge, 20 m5.xlarge, 10 r5.xlarge
* Or any other combination. The only certain know is that you will get 50 instances

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=50,instance_type=c5.xlarge+m5.xlarge+r5.xlarge
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# c5n.18xlarge is 36 cores so -np is 36 * 5
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 180 script.sh > my_output.log
```

**Use multiple SPOT instance type**

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=5,instance_type=c5.xlarge+m5.xlarge+r5.xlarge, spot_price=auto
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# c5n.18xlarge is 36 cores so -np is 36 * 5
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 180 script.sh > my_output.log
```

**Provision 50 instances (10 On-Demand and 40 SPOT)**

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
#PBS -l nodes=50,instance_type=c5.large,spot_allocation_count=40
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# c5n.18xlarge is 36 cores so -np is 36 * 5
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 180 script.sh > my_output.log
```

**Multi-lines parameters**

Custom AMI running on a different distribution than the scheduler, with EFA enable, without placement group and within a specific subnet\_id

```bash
#!/bin/bash
#PBS -N my_job_name
#PBS -V -j oe -o my_job_name.qlog
#PBS -P project_a
#PBS -q normal
## Resources can be specified on multiple lines
#PBS -l nodes=5,instance_type=c5n.18xlarge,efa_support=yes
#PBS -l placement_group=false,base_os=rhel7,ami_id=ami-12345,subnet_id=sub-abcde
## END PBS SETTINGS
cd $PBS_O_WORKDIR
cat $PBS_NODEFILE | sort | uniq > mpi_nodes
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/apps/openmpi/4.0.1/lib/
export PATH=$PATH:/apps/openmpi/4.0.1/bin/
# c5n.18xlarge is 36 cores so -np is 36 * 5
/apps/openmpi/4.0.1/bin/mpirun --hostfile mpi_nodes -np 180 script.sh > my_output.log
```

### Examples for `job-shared` queue <a href="#examples-for-job-shared-queue" id="examples-for-job-shared-queue"></a>

**Run a simple script on 96 vCPUs using on-demand c5.4xlarge instances on 'job-shared' queue**

```
#!/bin/bash
for i in {1..96}; do
    qsub -q job-shared -l instance_type=c5.4xlarge -l ht_support=true -- /path/to/script.sh
done
```

Since we specified `instance_type=c5.4xlarge` and `ht_support=true`, the number of instances required would be calculated as 96 vCPUs (total number of queued jobs is 96 and each job requires 1 vCPU) divided by 16 vCPUs provided by each c5.4xlarge instance. So, these queued jobs would lead to provisioning of 6 on-demand c5.4xlarge instances

***

**Run a simple script on 96 cores using on-demand c5.4xlarge instances on 'job-shared' queue**

```
#!/bin/bash
for i in {1..96}; do
    qsub -q job-shared -l instance_type=c5.4xlarge -l ht_support=false -- /path/to/script.sh
done
```

Since we specified `instance_type=c5.4xlarge` and `ht_support=false`, the number of instances required would be calculated as 96 cores (total number of queued jobs is 96 and each job requires 1 core) divided by 8 cores provided by each c5.4xlarge instance. So, these queued jobs would lead to provisioning of 12 on-demand c5.4xlarge instances

***

**Run a simple script on 96 vCPUs using Spot Fleet with c5.4xlarge or c5.9xlarge on 'job-shared' queue**

```
#!/bin/bash
for i in {1..96}; do
    qsub -q job-shared -l instance_type=c5.4xlarge+c5.9xlarge -l ht_support=true -l spot_price=auto -- /path/to/script.sh
done
```

Since we specified `instance_type=c5.4xlarge+c5.9xlarge`, and `spot_price=auto`, this will create a spot fleet request with two instance types c5.4xlarge and c5.9xlarge and the total required capacity would be 96. Weighted Capacity for each instance type would be automatically calculated for c5.4xlarge and c5.9xlarge based on the value of `ht_support`. In this case the weighted capacity for c5.4xlarge would be 16 and the weighted capacity for c5.9xlarge would be 36. The Spot fleet would then create a corresponding number of instances depending on instance availability.

***

**Run a script that requires 4 cores 24 times using Spot Fleet with c5.4xlarge or c5.9xlarge on 'job-shared' queue**

```
#!/bin/bash
for i in {1..24}; do
    qsub -q job-shared -l instance_type=c5.4xlarge+c5.9xlarge -l ht_support=false -l select=1:ncpus=4 -l spot_price=auto -- /path/to/four_core_script.sh
done
```

Since we specified `instance_type=c5.4xlarge+c5.9xlarge`, and `spot_price=auto`, this will create a spot fleet request with two instance types c5.4xlarge and c5.9xlarge and the total required capacity would be 96 (24 jobs each requires 4 cores as ht\_support is false). Weighted Capacity for each instance type would be automatically calculated for c5.4xlarge and c5.9xlarge based on the value of `ht_support`. In this case the weighted capacity for c5.4xlarge would be 8 and the weighted capacity for c5.9xlarge would be 18. The Spot fleet would then create a corresponding number of instances depending on instance availability.
