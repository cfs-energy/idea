# What is IDEA?

**ReadMe First:** This project is **NOT** AWS RES ([https://github.com/aws/res](https://github.com/aws/res)). RES is a fork of IDEA and not all features listed on this doc made it to RES. RES Documentation is available on [https://docs.aws.amazon.com/res/latest/ug/overview.html](https://docs.aws.amazon.com/res/latest/ug/overview.html)&#x20;

**I**ntegrated **D**igital **E**ngineering on **A**WS (**IDEA**) empowers teams of engineers, scientists and researchers with a cloud environment to host engineering tools required for end-to-end product development workloads (Computer Aided Design – CAD, Computer Aided Engineering - CAE, Model Based Systems Engineering - MBSE, Electronic Design Automation – EDA). Using this solution, research and development (R\&D) leaders enable engineers and designers to break-through development silos and accelerate the product development process.

With Integrated Digital Engineering on AWS, in a matter of hours engineering application teams can now deploy scalable engineering collaboration chambers customized to meet organizations security requirements for joint development with trusted partners and suppliers. Engineers interact with a catalog of familiar tools seamlessly integrated into an intuitive web portal. Applications tuned engineering workstations and compute clusters are automatically created and then scaled down once work is completed eliminating wasted time and resources.&#x20;

The solution features a large selection of compute resources; fast network backbone; unlimited storage;  budget and cost management directly integrated within AWS. The solution also deploys a user interface (UI) and automation tools that allows you to create your own queues, scheduler resources, Amazon Machine Images (AMIs), software and libraries.&#x20;

This solution is designed to provide a production ready reference implementation to be a starting point for digital engineering workloads, allowing engineering applications administrators and the engineers, analysts and designers they support to spend more time focusing on innovation.

## How to get started

Refer to this guide if you do not have any prior experience with IDEA. We will show you how to install your first cluster:

{% content-ref url="first-time-users/install-idea/" %}
[install-idea](first-time-users/install-idea/)
{% endcontent-ref %}

IDEA is deployed? Let's see how to interact with it:

{% content-ref url="first-time-users/lets-get-started.md" %}
[lets-get-started.md](first-time-users/lets-get-started.md)
{% endcontent-ref %}

IDEA is a framework on which you can deploy multiple independent modules based on your own requirements. Select the module (Scale-Out Workloads, Engineering Virtual Desktops ..) below to learn more.

{% content-ref url="broken-reference" %}
[Broken link](broken-reference)
{% endcontent-ref %}

## Key Features <a href="#easy-installation" id="easy-installation"></a>

### Easy installation <a href="#easy-installation" id="easy-installation"></a>

Installation of IDEA is fully automated via a visual installation wizard ([install-idea](first-time-users/install-idea/ "mention"))

{% hint style="info" %}
Did you know?

* You can have multiple IDEA clusters on the same AWS account
* IDEA comes with a list of unique tags, making resource tracking easy for AWS Administrators
{% endhint %}

### Access your cluster in 1 click <a href="#access-your-cluster-in-1-click" id="access-your-cluster-in-1-click"></a>

You can access your IDEA cluster using multiple channels such as APIs, Virtual Desktops, Web Interface or SSH ([access-your-idea-cluster](first-time-users/access-your-idea-cluster/ "mention"))

### Linux & Windows Virtual Desktops <a href="#simple-job-submission" id="simple-job-submission"></a>

Control your fleet of virtual desktops easily via the [Virtual Desktop Interface (VDI)](http://127.0.0.1:5000/o/ewXgnQpSEObr0Vh0WSOj/s/QthiamUzKn8KJLl0hYBf/ "mention")modules. Control what compute/AMIs can be used. Update your fleet in real-time and enable session-sharing to simplify collaboration cross teams.

### Simple Job Submission <a href="#simple-job-submission" id="simple-job-submission"></a>

IDEA [supports a list of parameters designed to simplify your job submission on AWS](https://docs.ide-on-aws.com/hpc-simulations/user-documentation/supported-ec2-parameters). Advanced users can either manually choose compute/storage/network configuration for their job or simply ignore these parameters and let IDEA picks the most optimal hardware (defined by the cluster administrator)

```bash
# Advanced Configuration
user@host$ qsub -l instance_type=c5n.18xlarge \
    -l instance_ami=ami-123abcde
    -l nodes=2 
    -l scratch_size=300 
    -l efa_support=true
    -l spot_price=1.55 myscript.sh

# Basic Configuration
user@host$ qsub myscript.sh
```

### Web-Based Jobs Workflow <a href="#os-agnostic-and-support-for-custom-ami" id="os-agnostic-and-support-for-custom-ami"></a>

Submit your Scale-Out Workloads simulation easily via a simple [NoCode web-based wizard interface.](https://docs.ide-on-aws.com/hpc-simulations/admin-documentation/create-web-based-job-submission-worfklows)

### OS agnostic and support for custom AMI <a href="#os-agnostic-and-support-for-custom-ami" id="os-agnostic-and-support-for-custom-ami"></a>

Customers can integrate their Centos7/Rhel7/AmazonLinux2 AMI automatically by simply using `-l instance_ami=<ami_id>` at job submission. There is no limitation in term of AMI numbers (you can have 10 jobs running simultaneously using 10 different AMIs). IDEA supports heterogeneous environment, so you can have concurrent jobs running different operating system on the same cluster.

{% hint style="warning" %}
**AMI using OS different than the scheduler**

In case your AMI is different than your scheduler host, you can specify the OS manually to ensure packages will be installed based on the node distribution.

In this example, we assume your IDEA deployment was done using AmazonLinux2, but you want to submit a job on your personal RHEL7 AMI

```bash
user@host$ qsub -l instance_ami=<ami_id> \
                -l base_os=rhel7 myscript.sh
```
{% endhint %}

### Web User Interface <a href="#web-user-interface" id="web-user-interface"></a>

IDEA includes a simple web interface designed to simplify user interactions ([web-interface.md](first-time-users/access-your-idea-cluster/web-interface.md "mention"))

### HTTP Rest API <a href="#http-rest-api" id="http-rest-api"></a>

IDEA is 100% API based. IDEA provide a Swagger template for all methods (.yml url is available via the `Settings` section of each module)

### Budgets and Cost Management <a href="#budgets-and-cost-management" id="budgets-and-cost-management"></a>

You can review your HPC costs ([review-your-aws-spend.md](best-practices/budget/review-your-aws-spend.md "mention"))  filtered by user/team/project/queue very easily using AWS Cost Explorer.

IDEA also supports AWS Budget and let you create budgets assigned to user/team/project or queue. To prevent over-spend, IDEA includes hooks to restrict job submission when customer-defined budget has expired ([set-up-budget-per-project.md](best-practices/budget/set-up-budget-per-project.md "mention"))

Lastly, Scale-Out Computing on AWS let you create queue ACLs or instance restriction at a queue level. Refer to  [budget](best-practices/budget/ "mention") for all best practices in order to control your HPC cost on AWS and prevent overspend.

### Detailed Cluster Analytics <a href="#detailed-cluster-analytics" id="detailed-cluster-analytics"></a>

IDEA includes OpenSearch (formerly ElasticSearch) and automatically ingest job and hosts data in real-time for accurate visualization of your cluster activity.

Don't know where to start? Check out [create-your-own-analytics-visualizations.md](best-practices/analytics/opensearch/create-your-own-analytics-visualizations.md "mention") for some examples

### 100% Customizable <a href="#100-customizable" id="100-customizable"></a>

IDEA is built entirely on top of AWS and can be customized by users as needed. The entire  codebase is open-source and available on Github ([https://github.com/awslabs/integrated-digital-engineering-on-aws)](https://github.com/awslabs/integrated-digital-engineering-on-aws)

### Persistent and Unlimited Storage <a href="#persistent-and-unlimited-storage" id="persistent-and-unlimited-storage"></a>

Scale-Out Computing on AWS includes two unlimited EFS storage (/apps and /data). Customers also have the ability to deploy high-speed SSD EBS disks or FSx for Lustre as scratch location on their compute nodes. [Refer to this page to learn more about the various storage options](https://awslabs.github.io/scale-out-computing-on-aws/storage/backend-storage-options/) offered by Scale-Out Computing on AWS

### Centralized user-management <a href="#centralized-user-management" id="centralized-user-management"></a>

Customers can create unlimited LDAP users and groups via OpenLDAP or Microsoft Active Directory.

### Automatic backup <a href="#automatic-backup" id="automatic-backup"></a>

IDEA  automatically backup your data ( [backup-idea-environment.md](best-practices/security/backup-idea-environment.md "mention")) with no additional effort required on your side.

### Support for network licenses <a href="#support-for-network-licenses" id="support-for-network-licenses"></a>

IDEA includes a FlexLM-enabled script which calculate the number of licenses for a given features and only start the job/provision the capacity when enough licenses are available.

### Automatic Errors Handling <a href="#automatic-errors-handling" id="automatic-errors-handling"></a>

IDEA performs various dry run checks before provisioning the capacity.&#x20;

### And more ... <a href="#and-more" id="and-more"></a>

Refer to the various sections (tutorial/security/analytics ...) to learn more about this solution
