What is IDEA?
=============

**ReadMe First:** This project is **NOT** AWS RES (
https://github.com/aws/res). RES is a fork of IDEA and not all features
listed on this doc made it to RES. RES Documentation is available on
https://docs.aws.amazon.com/res/latest/ug/overview.html

**I** ntegrated **D**igital **E**ngineering on **A**WS ( **IDEA**)
empowers teams of engineers, scientists and researchers with a cloud
environment to host engineering tools required for end-to-end product
development workloads (Computer Aided Design – CAD, Computer Aided
Engineering - CAE, Model Based Systems Engineering - MBSE, Electronic
Design Automation – EDA). Using this solution, research and development
(R&D) leaders enable engineers and designers to break-through
development silos and accelerate the product development process.

With Integrated Digital Engineering on AWS, in a matter of hours
engineering application teams can now deploy scalable engineering
collaboration chambers customized to meet organizations security
requirements for joint development with trusted partners and suppliers.
Engineers interact with a catalog of familiar tools seamlessly
integrated into an intuitive web portal. Applications tuned engineering
workstations and compute clusters are automatically created and then
scaled down once work is completed eliminating wasted time and
resources.

The solution features a large selection of compute resources; fast
network backbone; unlimited storage; budget and cost management directly
integrated within AWS. The solution also deploys a user interface (UI)
and automation tools that allows you to create your own queues,
scheduler resources, Amazon Machine Images (AMIs), software and
libraries.

This solution is designed to provide a production ready reference
implementation to be a starting point for digital engineering workloads,
allowing engineering applications administrators and the engineers,
analysts and designers they support to spend more time focusing on
innovation.

How to get started `<#how-to-get-started>`__
-------------------------------------------------------------------

Refer to this guide if you do not have any prior experience with IDEA.
We will show you how to install your first cluster:

`Install IDEA </idea/first-time-users/install-idea>`__

IDEA is deployed? Let’s see how to interact with it:

`Let’s get started </idea/first-time-users/lets-get-started>`__

IDEA is a framework on which you can deploy multiple independent modules
based on your own requirements. Select the module (Scale-Out Workloads,
Engineering Virtual Desktops ..) below to learn more.

`🤖
Modules </idea/modules>`__

Key Features `<#easy-installation>`__
------------------------------------------------------------

Easy installation `<#easy-installation-1>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Installation of IDEA is fully automated via a visual installation wizard
(`Install IDEA </idea/first-time-users/install-idea>`__)

Did you know?

You can have multiple IDEA clusters on the same AWS account

IDEA comes with a list of unique tags, making resource tracking easy for
AWS Administrators

Access your cluster in 1 click `<#access-your-cluster-in-1-click>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You can access your IDEA cluster using multiple channels such as APIs,
Virtual Desktops, Web Interface or SSH (`Access your IDEA
cluster </idea/first-time-users/access-your-idea-cluster>`__)

Linux & Windows Virtual Desktops `<#simple-job-submission>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Control your fleet of virtual desktops easily via the `Virtual Desktop
Interface
(VDI) <https://docs.ide-on-aws.com/virtual-desktop-interface/>`__\ modules.
Control what compute/AMIs can be used. Update your fleet in real-time
and enable session-sharing to simplify collaboration cross teams.

Simple Job Submission `<#simple-job-submission-1>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

IDEA `supports a list of parameters designed to simplify your job
submission on
AWS <https://docs.ide-on-aws.com/hpc-simulations/user-documentation/supported-ec2-parameters>`__.
Advanced users can either manually choose compute/storage/network
configuration for their job or simply ignore these parameters and let
IDEA picks the most optimal hardware (defined by the cluster
administrator)

# Advanced Configuration

| user@host$ qsub -linstance_type=c5n.18xlarge

-linstance_ami=ami-123abcde

-lnodes=2

-lscratch_size=300

-lefa_support=true

-lspot_price=1.55 myscript.sh

​

# Basic Configuration

user@host$ qsub myscript.sh

Web-Based Jobs Workflow `<#os-agnostic-and-support-for-custom-ami>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Submit your Scale-Out Workloads simulation easily via a simple `NoCode
web-based wizard
interface. <https://docs.ide-on-aws.com/hpc-simulations/admin-documentation/create-web-based-job-submission-worfklows>`__
​

OS agnostic and support for custom AMI `<#os-agnostic-and-support-for-custom-ami-1>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Customers can integrate their Centos7/Rhel7/AmazonLinux2 AMI
automatically by simply using ``-l instance_ami=<ami_id>`` at job
submission. There is no limitation in term of AMI numbers (you can have
10 jobs running simultaneously using 10 different AMIs). IDEA supports
heterogeneous environment, so you can have concurrent jobs running
different operating system on the same cluster.

**AMI using OS different than the scheduler**

In case your AMI is different than your scheduler host, you can specify
the OS manually to ensure packages will be installed based on the node
distribution.

In this example, we assume your IDEA deployment was done using
AmazonLinux2, but you want to submit a job on your personal RHEL7 AMI

| user@host$ qsub -linstance_ami=<ami_id>

-lbase_os=rhel7 myscript.sh

Web User Interface `<#web-user-interface>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

IDEA includes a simple web interface designed to simplify user
interactions (`Web
Interface </idea/first-time-users/access-your-idea-cluster/web-interface>`__)

HTTP Rest API `<#http-rest-api>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

IDEA is 100% API based. IDEA provide a Swagger template for all methods
(.yml url is available via the ``Settings`` section of each module)

Budgets and Cost Management `<#budgets-and-cost-management>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You can review your HPC costs (`Review your AWS
spend </idea/best-practices/budget/review-your-aws-spend>`__) filtered
by user/team/project/queue very easily using AWS Cost Explorer.

IDEA also supports AWS Budget and let you create budgets assigned to
user/team/project or queue. To prevent over-spend, IDEA includes hooks
to restrict job submission when customer-defined budget has expired
(`Set up budget per
project </idea/best-practices/budget/set-up-budget-per-project>`__)

Lastly, Scale-Out Computing on AWS let you create queue ACLs or instance
restriction at a queue level. Refer to
`Budget </idea/best-practices/budget>`__ for all best practices in order
to control your HPC cost on AWS and prevent overspend.

Detailed Cluster Analytics `<#detailed-cluster-analytics>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

IDEA includes OpenSearch (formerly ElasticSearch) and automatically
ingest job and hosts data in real-time for accurate visualization of
your cluster activity.

Don’t know where to start? Check out `Create your own analytics
visualizations </idea/best-practices/analytics/opensearch/create-your-own-analytics-visualizations>`__
for some examples

100% Customizable `<#100-customizable>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

IDEA is built entirely on top of AWS and can be customized by users as
needed. The entire codebase is open-source and available on Github (
`https://github.com/awslabs/integrated-digital-engineering-on-aws) <https://github.com/awslabs/integrated-digital-engineering-on-aws>`__
​

Persistent and Unlimited Storage `<#persistent-and-unlimited-storage>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Scale-Out Computing on AWS includes two unlimited EFS storage (/apps and
/data). Customers also have the ability to deploy high-speed SSD EBS
disks or FSx for Lustre as scratch location on their compute nodes.
`Refer to this page to learn more about the various storage
options <https://awslabs.github.io/scale-out-computing-on-aws/storage/backend-storage-options/>`__
offered by Scale-Out Computing on AWS

Centralized user-management `<#centralized-user-management>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Customers can create unlimited LDAP users and groups via OpenLDAP or
Microsoft Active Directory.

Automatic backup `<#automatic-backup>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

IDEA automatically backup your data ( `Backup IDEA
environment </idea/best-practices/security/backup-idea-environment>`__)
with no additional effort required on your side.

Support for network licenses `<#support-for-network-licenses>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

IDEA includes a FlexLM-enabled script which calculate the number of
licenses for a given features and only start the job/provision the
capacity when enough licenses are available.

Automatic Errors Handling `<#automatic-errors-handling>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

IDEA performs various dry run checks before provisioning the capacity.

And more … `<#and-more>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Refer to the various sections (tutorial/security/analytics …) to learn
more about this solution

`Next - First Time Users
Install IDEA </idea/first-time-users/install-idea>`__