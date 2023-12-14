# Control your AWS spend

IDEA offers multiple ways to make sure you will stay within budget while running your Engineer & Design  workloads on AWS

### General Best Practices <a href="#best-practices" id="best-practices"></a>

Assuming you are on-boarding a new team, here are our recommend best practices:

1 - [Create LDAP accounts for all users](https://docs.ide-on-aws.com/cluster-manager/menu/users-management).

2 - [Create LDAP group for the team. Add all users to the group](https://docs.ide-on-aws.com/cluster-manager/menu/groups-management).

3 - [Create a Project and map the LDAP group](https://docs.ide-on-aws.com/cluster-manager/menu/projects-management).

4 - [Create a new queue profile](https://docs.ide-on-aws.com/hpc-simulations/admin-documentation/queue-profiles) limit the queue ACLs to the project created step 3.

5 - [Limit the type of EC2 instances your users can provision](control-your-aws-spend.md#limit-what-type-of-ec2-instance-can-be-provisioned).

6 - [If needed, configure restricted parameters](https://docs.ide-on-aws.com/hpc-simulations/admin-documentation/queue-profiles#restricted-parameters).

7 - Create a Budget to make sure the new team won't spend more than what's authorized.

8 - [Limit your job to only run on your Reserved Instances](control-your-aws-spend.md#force-jobs-to-run-only-on-reserved-instances) or [limit the number of provisioned instances](control-your-aws-spend.md#limit-the-number-of-concurrent-jobs-or-provisioned-instances) for your queue.

### Limit who can submit jobs <a href="#limit-who-can-submit-jobs" id="limit-who-can-submit-jobs"></a>

Only allow specific individual users or/and LDAP groups to submit jobs or provision virtual desktops. [Refer to this page for examples and documentation](https://docs.ide-on-aws.com/cluster-manager/menu/projects-management).

### Limit what type of EC2 instance can be provisioned <a href="#limit-what-type-of-ec2-instance-can-be-provisioned" id="limit-what-type-of-ec2-instance-can-be-provisioned"></a>

Control what type of EC2 instances can be provisioned for any given queue. [Refer to this page for examples and documentation](https://docs.ide-on-aws.com/hpc-simulations/admin-documentation/queue-profiles#allowed-instance-types)

{% hint style="info" %}
Accelerated Computing Instances

Unless required for your workloads, it's recommended to exclude "p2", "p3", "g2", "g3", "p3dn" or other GPU instances type.
{% endhint %}

### Force jobs to run only on Reserved Instances <a href="#force-jobs-to-run-only-on-reserved-instances" id="force-jobs-to-run-only-on-reserved-instances"></a>

You can limit a your simulation jobs to only un on Reserved Instances if you specify `force_ri=True` ([Documentation](https://docs.ide-on-aws.com/hpc-simulations/user-documentation/supported-ec2-parameters#force\_ri)) flag at job submission or [for the entire queue](https://docs.ide-on-aws.com/hpc-simulations/admin-documentation/queue-profiles#force-reserved-instances). Your job will stay in the queue if you do not have any Reserved Instance available.

### Limit the number of concurrent jobs or provisioned instances <a href="#limit-the-number-of-concurrent-jobs-or-provisioned-instances" id="limit-the-number-of-concurrent-jobs-or-provisioned-instances"></a>

You can limit the number of [concurrent running jobs](https://docs.ide-on-aws.com/hpc-simulations/admin-documentation/queue-profiles#max-running-jobs) or [provisioned instances ](https://docs.ide-on-aws.com/hpc-simulations/admin-documentation/queue-profiles#ma-provisioned-instances)at the queue level.

{% hint style="info" %}
These settings are independent so you can choose to either limit by # jobs, # instances, both or nones
{% endhint %}

### Create a budget <a href="#create-a-budget" id="create-a-budget"></a>

Creating an AWS Budget will ensure jobs can't be submitted if the budget allocated to the team/queue/project has exceeded the authorized amount. [Refer to this page for examples and documentation](https://awslabs.github.io/scale-out-computing-on-aws/budget/set-up-budget-project/)

### Review your HPC cost in a central dashboard <a href="#review-your-hpc-cost-in-a-central-dashboard" id="review-your-hpc-cost-in-a-central-dashboard"></a>

Stay on top of your AWS costs in real time. Quickly visualize your overall usage and find answers to your most common questions:

* Who are my top users?
* How much money did we spend for Project A?
* How much storage did we use for Queue B?
* Where my money is going (storage, compute ...)
* Etc ...

[Refer to this page for examples and documentation](https://awslabs.github.io/scale-out-computing-on-aws/budget/review-hpc-costs/)
