# Update IDEA configuration (idea-admin.sh config)

{% hint style="info" %}
Use the **Config** command if you want to make a configuration change (e.g: enable SES, point DCV driver to a new version, change default security group ...). Refer to [update-idea-configuration.md](update-idea-configuration.md "mention") for other types of updates.
{% endhint %}

`idea-admin.sh config` utility is a powerful tool that allow you to control the vast majority of your cluster configuration/parameters without having to manually access the configuration files. In this example, we will demonstrate how you can easily update the integration of IDEA with AWS Backup.

## Workflow

<figure><img src="../../../.gitbook/assets/image (3).png" alt=""><figcaption></figcaption></figure>

* Admins run the `idea-admin.sh config` utility to retrieve/update the current configuration
* Configuration change is updated on the Amazon DynamoDB table associated to the IDEA cluster
* DynamoDB Stream updates the relevant IDEA module automatically after a configuration parameter has been changed

{% hint style="info" %}
Configuration keys are prefixed with the module information.&#x20;

See some examples below:

* scheduler.security\_group\_id is linked to the [Scale-Out Workloads](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/LGamNPuOYtjAP3GFfRJO/ "mention") module&#x20;
* vdc.dcv\_host\_security\_group\_id is linked to the [Virtual Desktop Interface (VDI)](https://app.gitbook.com/o/ewXgnQpSEObr0Vh0WSOj/s/QthiamUzKn8KJLl0hYBf/ "mention") module
* directoryservice.root\_username\_secret\_arn is linked to the DirectoryService module
{% endhint %}

## ./idea-admin.sh config show

You can retrieve the current configuration of your IDEA cluster by running `./idea-admin.sh config show` utility.&#x20;

This utility also supports regular expressions as part of the `--query/-q` argument.&#x20;

For example, run the command below to list all configuration related to the integration of AWS Backup:

```
./idea-admin.sh config show \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION> \
  --query "(.*)backup(.*)"
+-------------------------------------------------------------------------------+----------------------------------------------------------------------------------------+---------+
| Key                                                                           | Value                                                                                  | Version |
+-------------------------------------------------------------------------------+----------------------------------------------------------------------------------------+---------+
| cluster.backups.backup_plan.arn                                               | arn:aws:backup:us-east-2:REDACTED:backup-plan:c80992c6-8d7c-4708-9182-0bf81f20b3d2     | 2       |
| cluster.backups.backup_plan.rules.default.completion_window_minutes           | 480                                                                                    | 1       |
| cluster.backups.backup_plan.rules.default.delete_after_days                   | 7                                                                                      | 1       |
| cluster.backups.backup_plan.rules.default.move_to_cold_storage_after_days     | -                                                                                      | 1       |
| cluster.backups.backup_plan.rules.default.schedule_expression                 | cron(0 5 * * ? *)                                                                      | 1       |
| cluster.backups.backup_plan.rules.default.start_window_minutes                | 60                                                                                     | 1       |
| cluster.backups.backup_plan.selection.tags                                    | - Key=idea:ClusterName,Value=<CLUSTER_NAME>                                            | 1       |
|                                                                               | - Key=idea:BackupPlan,Value=cluster                                                    |         |
|                                                                               |                                                                                        |         |
| cluster.backups.backup_vault.arn                                              | arn:aws:backup:us-east-2:REDACTED:backup-vault:<CLUSTER_NAME>-cluster-backup-vault        | 2       |
| cluster.backups.backup_vault.kms_key_id                                       | -                                                                                      | 1       |
| cluster.backups.backup_vault.removal_policy                                   | DESTROY                                                                                | 1       |
| cluster.backups.enable_restore                                                | True                                                                                   | 1       |
| cluster.backups.enabled                                                       | True                                                                                   | 1       |
| cluster.backups.role_arn                                                      | arn:aws:iam::REDACTED:role/<CLUSTER_NAME>-cluster-backup-role-us-east-2                   | 2       |
| cluster.logging.handlers.file.backupCount                                     | 15                                                                                     | 1       |
| vdc.vdi_host_backup.backup_plan.arn                                           | arn:aws:backup:us-east-2:REDACTED:backup-plan:d65c14d0-a970-4d3c-933e-4b658e1a6f3d     | 1       |
| vdc.vdi_host_backup.backup_plan.rules.default.completion_window_minutes       | 480                                                                                    | 1       |
| vdc.vdi_host_backup.backup_plan.rules.default.delete_after_days               | 7                                                                                      | 1       |
| vdc.vdi_host_backup.backup_plan.rules.default.move_to_cold_storage_after_days | -                                                                                      | 1       |
| vdc.vdi_host_backup.backup_plan.rules.default.schedule_expression             | cron(0 5 * * ? *)                                                                      | 1       |
| vdc.vdi_host_backup.backup_plan.rules.default.start_window_minutes            | 60                                                                                     | 1       |
| vdc.vdi_host_backup.backup_plan.selection.tags                                | - Key=idea:ClusterName,Value=<CLUSTER_NAME>                                            | 1       |
|                                                                               | - Key=idea:BackupPlan,Value=vdc                                                        |         |
|                                                                               |                                                                                        |         |
| vdc.vdi_host_backup.enabled                                                   | True                                                                                   | 1       |
+-------------------------------------------------------------------------------+----------------------------------------------------------------------------------------+---------+

```

{% hint style="info" %}
You can display the output in multiple formats (yaml/table/raw)
{% endhint %}

To continue our example, let's pretend we want to disable the AWS Backup integration.&#x20;

First, query your IDEA configuration to verify if the integration is active by checking the "cluster.backups.enabled" parameter.

```
./idea-admin.sh config show \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
  --query "cluster.backups.enabled"
+-----------------------------+-------+---------+
| Key                         | Value | Version |
+-----------------------------+-------+---------+
| cluster.backups.enabled.    | True  | 1       |
+-----------------------------+-------+---------+
```

Alternatively, you can validate this setting via the web interface under "**Cluster Settings**":

![](<../../../.gitbook/assets/Screen Shot 2022-12-04 at 5.01.18 PM.png>)

## ./idea-admin.sh config set

To update this configuration parameter, run the `./idea-admin.sh config set` command and pass the `Key` argument via Key=\<param\_name>,Type=\<param\_type>,Value=\<param\_value>

```
./idea-admin.sh config set \
  Key=cluster.backups.enabled,Type=bool,Value=False \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
+-----------------------------+-------+
| Key                         | Value |
+-----------------------------+-------+
| cluster.backups.enabled.    | False |
+-----------------------------+-------+
? Are you sure you want to update above config entries? Yes
updating config: cluster.backups.enabled = False

```

{% hint style="info" %}
Entry must be of below format: Key=KEY\_NAME,Type=\[str|int|float|bool|list|list|list|list],Value=\[VALUE|\[VALUE1,VALUE2,...]]&#x20;

Config key names cannot contain: comma(,), colon(:)

Examples:

1. To set a **string** config type: ./idea-admin.sh config set Key=global-settings.string\_val,Type=string,Value=stringcontent --cluster-name \<CLUSTER\_NAME> --aws-region \<REGION>
2. To set an **integer** config type: ./idea-admin.sh config set Key=global-settings.int\_val,Type=int,Value=12 --cluster-name \<CLUSTER\_NAME> --aws-region \<REGION>
3. To set a config **with list of strings**: ./idea-admin.sh config set "Key=my\_config.string\_list,Type=list\<str>,Value=value1,value2" --cluster-name \<CLUSTER\_NAME> --aws-region \<REGION>
4. To set a config **with list of integers**: ./idea-admin.sh config set "Key=my\_config.string\_list,Type=list\<int>,Value=value1,value2" --cluster-name \<CLUSTER\_NAME> --aws-region \<REGION>
5. To set a config **with list of decimal/float**: ./idea-admin.sh config set "Key=my\_config.string\_list,Type=list\<float>,Value=value1,value2" --cluster-name \<CLUSTER\_NAME> --aws-region \<REGION>
6. To set a config **with list of bool**: ./idea-admin.sh config set "Key=my\_config.string\_list,Type=list\<bool>,Value=value1,value2" --cluster-name \<CLUSTER\_NAME> --aws-region \<REGION>
7. Update multiple config entries: ./idea-admin.sh config set Key=global-settings.string\_val,Type=string,Value=stringcontent\
   "Key=global-settings.integer\_list,Type=list,Value=1,2"\
   "Key=global-settings.string\_list,Type=list,Value=str1,str2"\
   \--cluster-name \<CLUSTER\_NAME>\
   \--aws-region \<REGION>
{% endhint %}

You can now re-run the `./idea-admin.sh config show` command to validate the configuration in the IDEA database has been updated correctly:

```
./idea-admin.sh config show \
  --cluster-name <CLUSTER_NAME> \
  --aws-region <REGION>
  --query "cluster.backups.enabled"
+-----------------------------+-------+---------+
| Key                         | Value | Version |
+-----------------------------+-------+---------+
| cluster.backups.enabled     | False | 2       |
+-----------------------------+-------+---------+

```

Alternatively, you can validate this setting via the web interface under "**Cluster Settings**" and config the integration with AWS Backup is now disabled.

![](<../../../.gitbook/assets/Screen Shot 2022-12-05 at 11.14.21 AM.png>)
