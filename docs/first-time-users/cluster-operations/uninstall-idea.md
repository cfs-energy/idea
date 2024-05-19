# Uninstall IDEA

{% hint style="warning" %}
Make sure to have removed all backups from AWS Backup (if applicable).  Read more at [backup-idea-environment.md](../../best-practices/security/backup-idea-environment.md "mention")
{% endhint %}

To uninstall IDEA, run `idea-admin.sh delete-cluster` command with the following arguments:

<table><thead><tr><th>Argument</th><th>Description</th><th data-type="checkbox">Required ?</th></tr></thead><tbody><tr><td>--cluster-name</td><td>Name of your IDEA cluster  (e.g: idea-test)</td><td>true</td></tr><tr><td>--aws-region</td><td>Region where your IDEA cluster is installed</td><td>true</td></tr><tr><td>--delete-bootstrap</td><td>Delete the S3 bucket created by CDK</td><td>false</td></tr><tr><td>--delete-databases</td><td>Delete DynamoDB tables associated to your cluster</td><td>false</td></tr><tr><td>--delete-backups</td><td>Delete backups from AWS Backups</td><td>false</td></tr><tr><td>--force</td><td>Skip confirmation prompts</td><td>false</td></tr></tbody></table>

The uninstall command will also remove running instances such as virtual desktops if applicable.

#### Example: Clean-Uninstall

{% hint style="info" %}
Deleting Analytics stack (if using OpenSearch) will take at least 20 minutes
{% endhint %}

{% hint style="warning" %}
\--delete-backups will also delete any backups stored on AWS Backups.&#x20;
{% endhint %}

```bash
# add --delete-backups if you also want to delete backups
./idea-admin.sh delete-cluster \ 
  --delete-databases \
  --delete-bootstrap \
  --force \
  --aws-region us-east-2 \
  --cluster-name idea-beta
  
+------------------------------------+---------------------+--------------+---------------+---------+
| Name                               | Instance Id         | Private IP   | Instance Type | Status  |
+------------------------------------+---------------------+--------------+---------------+---------+
| idea-beta-MyDesktop1-clusteradmin | i-030287972c8dec008 | 10.0.142.68  | t3.xlarge     | running |
| idea-beta-MyDesktop4-clusteradmin | i-04f7e691249459fed | 10.0.206.251 | t3.xlarge     | running |
+------------------------------------+---------------------+--------------+---------------+---------+
2 ec2 instances will be terminated.
searching for cloud formation stacks to be terminated ...
+------------------------------+-----------------+------------------------+
| Stack Name                   | Status          | Termination Protection |
+------------------------------+-----------------+------------------------+
| idea-beta-metrics           | CREATE_COMPLETE | True                   |
| idea-beta-vdc               | CREATE_COMPLETE | True                   |
| idea-beta-identity-provider | CREATE_COMPLETE | True                   |
| idea-beta-bastion-host      | CREATE_COMPLETE | True                   |
| idea-beta-cluster-manager   | CREATE_COMPLETE | True                   |
| idea-beta-directoryservice  | CREATE_COMPLETE | True                   |
| idea-beta-scheduler         | CREATE_COMPLETE | True                   |
| idea-beta-analytics         | CREATE_COMPLETE | True                   |
| idea-beta-shared-storage    | CREATE_COMPLETE | True                   |
| idea-beta-cluster           | CREATE_COMPLETE | True                   |
+------------------------------+-----------------+------------------------+
10 stacks will be terminated.
executing app-module-clean-up commands for app: cluster-manager
executing app-module-clean-up commands for app: vdc
executing app-module-clean-up commands for app: scheduler
+-----------------------------+---------------------+--------------+---------------+---------+
| Name                        | Instance Id         | Private IP   | Instance Type | Status  |
+-----------------------------+---------------------+--------------+---------------+---------+
| idea-beta-directoryservice | i-072478e020c011b7b | 10.0.112.123 | m5.large      | running |
| idea-beta-scheduler        | i-0443a832c4696c5c5 | 10.0.81.11   | m5.large      | running |
| idea-beta-bastion-host     | i-0049d21d8ccfd36c1 | 10.0.0.15    | m5.large      | running |
+-----------------------------+---------------------+--------------+---------------+---------+
found 3 ec2 instances with termination protection enabled.
disabling termination protection for ec2 instance: i-072478e020c011b7b ...
termination protection disabled for ec2 instance: i-072478e020c011b7b
disabling termination protection for ec2 instance: i-0443a832c4696c5c5 ...
termination protection disabled for ec2 instance: i-0443a832c4696c5c5
disabling termination protection for ec2 instance: i-0049d21d8ccfd36c1 ...
termination protection disabled for ec2 instance: i-0049d21d8ccfd36c1
terminating ec2 instance: i-030287972c8dec008
terminated ec2 instance: i-030287972c8dec008
terminating ec2 instance: i-04f7e691249459fed
terminated ec2 instance: i-04f7e691249459fed
disabling termination protection for stack: idea-beta-metrics
terminating cloud formation stack: idea-beta-metrics
disabling termination protection for stack: idea-beta-vdc
terminating cloud formation stack: idea-beta-vdc
disabling termination protection for stack: idea-beta-identity-provider
terminating cloud formation stack: idea-beta-identity-provider
disabling termination protection for stack: idea-beta-bastion-host
terminating cloud formation stack: idea-beta-bastion-host
disabling termination protection for stack: idea-beta-cluster-manager
terminating cloud formation stack: idea-beta-cluster-manager
disabling termination protection for stack: idea-beta-directoryservice
terminating cloud formation stack: idea-beta-directoryservice
disabling termination protection for stack: idea-beta-scheduler
terminating cloud formation stack: idea-beta-scheduler
disabling termination protection for stack: idea-beta-analytics
terminating cloud formation stack: idea-beta-analytics
disabling termination protection for stack: idea-beta-shared-storage
terminating cloud formation stack: idea-beta-shared-storage
stack: idea-beta-metrics, status: DELETE_COMPLETE
stack: idea-beta-vdc, status: DELETE_IN_PROGRESS
stack: idea-beta-identity-provider, status: DELETE_IN_PROGRESS
stack: idea-beta-bastion-host, status: DELETE_IN_PROGRESS
stack: idea-beta-cluster-manager, status: DELETE_IN_PROGRESS
stack: idea-beta-directoryservice, status: DELETE_IN_PROGRESS
stack: idea-beta-scheduler, status: DELETE_IN_PROGRESS
stack: idea-beta-analytics, status: DELETE_IN_PROGRESS
stack: idea-beta-shared-storage, status: DELETE_IN_PROGRESS
waiting for 8 stacks to be deleted: ['idea-beta-vdc', 'idea-beta-identity-provider', 'idea-beta-bastion-host', 'idea-beta-cluster-manager', 'idea-beta-directoryservice', 'idea-beta-scheduler', 'idea-beta-analytics', 'idea-beta-shared-storage'] ...
stack: idea-beta-vdc, status: DELETE_IN_PROGRESS
stack: idea-beta-identity-provider, status: DELETE_COMPLETE
stack: idea-beta-bastion-host, status: DELETE_IN_PROGRESS
stack: idea-beta-cluster-manager, status: DELETE_IN_PROGRESS
.........
.........
.........
stack: idea-beta-cluster, status: DELETE_COMPLETE
disabling termination protection for stack: idea-beta-bootstrap
terminating cloud formation stack: idea-beta-bootstrap
found cluster s3 bucket: idea-beta-cluster-us-east-2-549172027899
deleting s3 bucket: idea-beta-cluster-us-east-2-549172027899 for cluster ...
bucket idea-beta-cluster-us-east-2-549172027899 deleted successfully
+--------------------------------------------------------------------------+
| Table Name                                                               |
+--------------------------------------------------------------------------+
| idea-beta.accounts.group-members                                        |
| idea-beta.accounts.groups                                               |
| idea-beta.accounts.sequence-config                                      |
| idea-beta.accounts.sso-state                                            |
| idea-beta.accounts.users                                                |
| idea-beta.cluster-manager.distributed-lock                              |
| idea-beta.cluster-settings                                              |
| idea-beta.email-templates                                               |
| idea-beta.modules                                                       |
| idea-beta.projects                                                      |
| idea-beta.projects.project-groups                                       |
| idea-beta.projects.user-projects                                        |
| idea-beta.scheduler.applications                                        |
| idea-beta.scheduler.license-resources                                   |
| idea-beta.scheduler.queue-profiles                                      |
| idea-beta.vdc.controller.permission-profiles                            |
| idea-beta.vdc.controller.schedules                                      |
| idea-beta.vdc.controller.servers                                        |
| idea-beta.vdc.controller.session-permissions                            |
| idea-beta.vdc.controller.software-stacks                                |
| idea-beta.vdc.controller.ssm-commands                                   |
| idea-beta.vdc.controller.user-sessions                                  |
| idea-beta.vdc.controller.user-sessions-counter                          |
| idea-beta.vdc.dcv-broker.AgentKeyPair                                   |
| idea-beta.vdc.dcv-broker.AgentOAuth2Clients                             |
| idea-beta.vdc.dcv-broker.AuthServerPubKeys                              |
| idea-beta.vdc.dcv-broker.BrokerAuthServerPrivateKey                     |
| idea-beta.vdc.dcv-broker.ConnectSessionKeyPair                          |
| idea-beta.vdc.dcv-broker.DescribeNextTokenKeyPair                       |
| idea-beta.vdc.dcv-broker.HealthTest                                     |
| idea-beta.vdc.dcv-broker.JwksUrls                                       |
| idea-beta.vdc.dcv-broker.PortalOAuth2Clients                            |
| idea-beta.vdc.dcv-broker.ServerDnsMapping                               |
| idea-beta.vdc.dcv-broker.SoftwareStatement                              |
| idea-beta.vdc.dcv-broker.UpdateSessionPermissionsSessionCustomerRequest |
| idea-beta.vdc.dcv-broker.createSessionCustomerRequest                   |
| idea-beta.vdc.dcv-broker.dcvServer                                      |
| idea-beta.vdc.dcv-broker.deleteSessionCustomerRequest                   |
| idea-beta.vdc.distributed-lock                                          |
+--------------------------------------------------------------------------+
39 tables will be deleted.
deleting table: idea-beta.accounts.group-members ...
deleted dynamodb table: idea-beta.accounts.group-members
deleting table: idea-beta.accounts.groups ...
deleted dynamodb table: idea-beta.accounts.groups
deleting table: idea-beta.accounts.sequence-config ...
deleted dynamodb table: idea-beta.accounts.sequence-config
deleting table: idea-beta.accounts.sso-state ...
.........
.........
.........
```
