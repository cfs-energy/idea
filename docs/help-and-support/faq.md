# FAQ

New clusters use the container control plane only. Move an existing host-based control plane with `upgrade-cluster --drain`; follow the [container upgrade guide](../first-time-users/cluster-operations/update-idea-cluster/move-to-containers.md).

## Cluster Management

<details>

<summary>How do I update Boto3</summary>

Boto3 ([https://boto3.amazonaws.com/v1/documentation/api/latest/index.html](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)) is the official AWS Python SDK. We recommend to update boto3 on a regular basis in order to stay up-to-date with the latest AWS releases (new instance types ...)

On a cluster whose control plane runs as containers, the SDK version is part of the control plane image: build an image with the newer Boto3 and roll it out as described in [patch-idea-module.md](../first-time-users/cluster-operations/update-idea-cluster/patch-idea-module.md "mention"). On a host, run the update over Systems Manager and restart the services:

```
aws ssm send-command --instance-ids <SCHEDULER_INSTANCE_ID> --document-name AWS-RunShellScript \
  --parameters 'commands=["sudo idea_pip install boto3 --upgrade && sudo supervisorctl restart all"]'
```

</details>

<details>

<summary>How do I safe-list a new IP to access my IDEA environment</summary>

To safelist a new IP, navigate to VPC > Managed Prefix List and add your new entry into the Prefix List created by IDEA.

Alternatively, you can run the following `idea-admin.sh` command:

```
./idea-admin.sh utils cluster-prefix-list add-entry \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION> \
  --cidr x.x.x.x/x \
  --description '<DESCRIPTION>'
```

</details>

<details>

<summary>I never received the welcome email after installing IDEA, how can I create an admin user?</summary>

For a container deployment, connect to a running cluster-manager application task using the ECS
Exec procedure under **How to debug a module not starting correctly** below. The task must be
healthy enough to serve its local application socket; if it is not, inspect its logs and restore
service first. In that container's root shell, run the application CLI:

```bash
python3.13 -m ideaclustermanager.cli.cli_main accounts create-user \
  --email '<ADMIN_EMAIL>' --password '<NEW_PASSWORD>' --username '<ADMIN_USERNAME>' \
  --sudo --email-verified
```

This creates a confirmed administrator without sending an invitation email. Use the same
application task's logs to investigate a failed request. On a host deployment, connect to the
cluster-manager EC2 instance through Systems Manager and run
`sudo ideactl accounts create-user --email '<ADMIN_EMAIL>' --password '<NEW_PASSWORD>' --username '<ADMIN_USERNAME>' --sudo --email-verified`.

</details>

<details>

<summary>How do I patch/update/change the configuration an IDEA module</summary>

See [update-idea-cluster](../first-time-users/cluster-operations/update-idea-cluster/ "mention")

</details>

<details>

<summary>How do I uninstall IDEA?</summary>

See [uninstall-idea.md](../first-time-users/cluster-operations/uninstall-idea.md "mention")

</details>

<details>

<summary>How do I resume a failed IDEA installation</summary>

See [#how-do-i-resume-a-failed-installation](faq.md#how-do-i-resume-a-failed-installation "mention")

</details>

<details>

<summary>How to customize the logo/title or subtitle of my IDEA environment</summary>

The logo, title and subtitle of the Web Portal can be customized using configurations.

<img src="https://confluence.amazon.com/download/attachments/108564578/Screen%20Shot%202022-07-11%20at%207.49.14%20AM.png?version=2&#x26;modificationDate=1657551271000&#x26;api=v2" alt="" data-size="original">

#### Defaults <a href="#customizelogo-titleandsubtitle-defaults" id="customizelogo-titleandsubtitle-defaults"></a>

* title - Integrated Digital Engineering on AWS (IDEA)
* logo - IDEA Default Logo
* subtitle - \<cluster-name> (\<aws-region>)

#### Customization <a href="#customizelogo-titleandsubtitle-customization" id="customizelogo-titleandsubtitle-customization"></a>

**Logo**

Logo can be customized by uploading appropriate logo file to the cluster's S3 Bucket. Copy the S3 object key and run the below command:

```bash
./idea-admin.sh config \
set Key=cluster-manager.web_portal.logo,Type=string,Value=assets/logo.png \
--cluster-name <CLUSTER_NAME> \
--aws-region <REGION>
```

**Title**

Title can be customized by running the below command:

```bash
./idea-admin.sh config \
  set "Key=cluster-manager.web_portal.title,Type=string,Value=My Company" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
```

**Subtitle**

Subtitle can be customized by running the below command:

```bash
./idea-admin.sh config \
  set "Key=cluster-manager.web_portal.subtitle,Type=string,Value=R&D Cluster" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>


```

</details>

<details>

<summary>How to embed an external dashboard in the Web Portal</summary>

The Web Portal can render an external dashboard URL in a sandboxed iframe as an extra entry under **Home**. It is disabled by default.

```bash
./idea-admin.sh config \
  set "Key=cluster-manager.web_portal.custom_dashboard.enabled,Type=bool,Value=true" \
      "Key=cluster-manager.web_portal.custom_dashboard.title,Type=string,Value=Cluster Dashboard" \
      "Key=cluster-manager.web_portal.custom_dashboard.url,Type=string,Value=https://dashboard.example.com/view" \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
```

The nav entry and page stay hidden unless `enabled` is `true` and `url` is an `http`/`https` URL. The iframe is sandboxed and sends no referrer, so the dashboard must permit framing by the Web Portal origin (`Content-Security-Policy: frame-ancestors`) and must not depend on the referrer header. A dashboard that refuses framing renders as an empty box with no error, so the page header always carries an **Open in a new tab** link.

A dashboard served from the Web Portal origin itself is framed without `allow-same-origin`, so it cannot use cookies or browser storage; host it on its own origin if it needs them.

</details>

<details>

<summary>How do I configure automatic mount for additional File-system (FSx Lustre/OnTAP/OpenZFS/Windows, EFS)</summary>

See [storage](../modules/storage/ "mention") module

</details>

<details>

<summary>How do I automatically add new tags during the installation?</summary>

Update the last section of source/idea/ideactl/resources/config/templates/global-settings/settings.yml

```
# provide custom tags for all resources created by IDEA
# for eg. to add custom tags, tags as below:
# custom_tags:
#   - Key=custom:MyTagName,Value=MyTagValue
#   - Key=AnotherExampleName,Value=Another Example Value
custom_tags: []
```

</details>

<details>

<summary>How to automatically add IAM Managed Policies to existing IDEA IAM roles</summary>

Add the managed policy ARN in cluster settings: source/idea/ideactl/resources/config/templates/cluster/settings.yml

All roles will contain the policy(ies) you have added to the list.

</details>

<details>

<summary><strong>I am using an existing VPC and scheduler module is not working (not able to query the internal DNS)</strong></summary>

IDEA create a route53 private hosted zone.

If you try to curl any DNS from this Route53 Zone,and get no result, even though the Private Zone is assigned to the VPC

```
# nslookup
internal-alb.idea-demo.us
-east-2.local
Server: 10.110.0.2
Address: 10.110.0.2#53
** server can't find
internal-alb.idea-demo.us
-east-2.local: NXDOMAIN
```

To fix this, enable DNS hostname/resolution on your VPC

</details>

## IDEA Logs

<details>

<summary>Where are the application logs stored?</summary>

For containers, CloudWatch log groups persist across task replacement. With the generated module
IDs, the groups are `/<CLUSTER_NAME>/cluster-manager`, `/<CLUSTER_NAME>/scheduler`,
`/<CLUSTER_NAME>/scheduler/openpbs`, `/<CLUSTER_NAME>/vdc/controller`,
`/<CLUSTER_NAME>/vdc/dcv-broker` and `/<CLUSTER_NAME>/vdc/dcv-connection-gateway`.
The Datadog daemon writes to `/<CLUSTER_NAME>/ecs/datadog`. Check both the main container's stdout
stream and the file-tail sidecar streams: application files and PBS/broker/gateway logs do not all
appear on stdout. Streams include the container and task ID, so use the failed task's ID when
investigating a replacement.

IDEA modules such as cluster-manager, virtual-desktop-controller and scheduler run a python based application server.

The application server logs are available under: **/opt/idea/app/logs**

All logs will be available in **application.log**. In rare occasions, few logs may be available under **stdout.log**.

Logging can configured per application server using IDEA Cluster Configuration. Below is the logging configuration for cluster-manager:

```
./idea-admin.sh config show \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION> \
  --query "cluster-manager.logging.*"
+-----------------------------------------------+--------------------+---------+
| Key                                           | Value              | Version |
+-----------------------------------------------+--------------------+---------+
| cluster-manager.logging.default_log_file_name | application.log    |    1    |
| cluster-manager.logging.logs_directory        | /opt/idea/app/logs |    1    |
| cluster-manager.logging.profile               | production         |    1    |
+-----------------------------------------------+--------------------+---------+
```

</details>

<details>

<summary>How to debug a module not starting correctly</summary>

For a container deployment, inspect ECS service events, desired/running task counts, and the failed
task's stopped reason and container exit codes. Open its CloudWatch streams from the task's Logs
tab, including application-file sidecars; image pulls, secret access, mounts and health checks can
fail before the application starts.

Use a profile for the target account, AWS CLI with the Session Manager plugin, and permission for
ECS Exec. Read the ECS cluster name and discover the service and task:

```bash
./idea-admin.sh config show --cluster-name <CLUSTER_NAME> --aws-region <REGION> \
  --query ecs.cluster_name --format raw
aws --profile <PROFILE> --region <REGION> ecs list-services --cluster <ECS_CLUSTER>
aws --profile <PROFILE> --region <REGION> ecs describe-services --cluster <ECS_CLUSTER> --services <SERVICE>
aws --profile <PROFILE> --region <REGION> ecs list-tasks --cluster <ECS_CLUSTER> --service-name <SERVICE>
aws --profile <PROFILE> --region <REGION> ecs describe-tasks --cluster <ECS_CLUSTER> --tasks <TASK_ARN>
aws --profile <PROFILE> --region <REGION> ecs execute-command --cluster <ECS_CLUSTER> \
  --task <TASK_ARN> --container <APPLICATION_CONTAINER_NAME> --interactive --command /bin/bash
```

Select the application container from `describe-tasks`, not its log sidecar; use the cluster-manager
service for account recovery. Inspect `/opt/idea/app/logs` there. After correcting configuration or
deploying a corrected image, use `aws --profile <PROFILE> --region <REGION> ecs update-service --cluster <ECS_CLUSTER> --service <SERVICE> --force-new-deployment`
and wait for the service to stabilize. Scheduler replacement briefly interrupts submissions/API
access. Containers run their role directly and are restarted by ECS, so supervisor commands do not
apply to them.

For a host deployment, connect through Systems Manager, inspect `/root/bootstrap/logs`, then
`/opt/idea/app/logs`. Check `/opt/idea/python/latest/bin/supervisorctl status` and
`/var/log/supervisord.log`; after fixing the cause, run `sudo supervisorctl restart all` on that host.

</details>

## Scale-Out Workloads Jobs

<details>

<summary>My job is not starting, how can I check the bootstrap/setup logs</summary>

If your job is not starting, you can verify if the provisioned capacity is configured correctly by checking the bootstrap logs under `/apps/<CLUSTER>/scheduler/jobs`

Logs structure:

* jobs/
  * \<job\_id>/
    * \<job\_type> (bootstrap or compute\_node setup)
      * \<EC2 Host>

Example: `/apps/idea-demo/scheduler/jobs/98/logs/ip-10-110-4-189`

</details>

<details>

<summary>How do I add additional logic to be executed on my compute nodes?</summary>

Edit `/apps/<CLUSTER>/scheduler/compute_node/userdata_customizations.sh` if you want to add your own code to the compute node(s). Script is executed at the very end of the bootstrap sequence.

</details>
