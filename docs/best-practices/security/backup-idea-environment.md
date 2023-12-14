# Backup IDEA environment

By default, IDEA **automatically backup your EC2 scheduler, EFS filesystems, DynamoDB and Virtual Desktops every day and keep the backups for 7 days** using AWS Backup.&#x20;

During the installation, IDEA creates a new [#backup-vault](backup-idea-environment.md#backup-vault "mention") and one [#backup-plan](backup-idea-environment.md#backup-plan "mention") per module&#x20;

{% hint style="success" %}
You can change the default AWS Backup parameters created by IDEA to match your own company requirements (what resources to backup, backup lifecycle policies, when to trigger a backup etc ...)
{% endhint %}

#### What is AWS Backups? <a href="#what-is-aws-backups" id="what-is-aws-backups"></a>

[AWS Backup](https://aws.amazon.com/backup/) is a fully managed backup service that makes it easy to centralize and automate the back up of data across AWS services in the cloud. Using AWS Backup, you can centrally configure backup policies and monitor backup activity for AWS resources, such as Amazon EBS volumes, Amazon RDS databases, Amazon DynamoDB tables, Amazon EFS file systems.

#### Backup Vault <a href="#backup-vault" id="backup-vault"></a>

A "Backup Vault" is where all your backups are stored. Your vault is automatically encrypted using your [Key Management Service (KMS)](https://aws.amazon.com/kms/) key and reference to your IDEA cluster ID&#x20;

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-19 at 10.26.08 AM.png" alt=""><figcaption><p>List of AWS Backup vault as well as count of the number of protected resources (recovery points)</p></figcaption></figure>

#### Backup Plan <a href="#backup-plan" id="backup-plan"></a>

A "Backup Plan" is where you define all your backup strategy such as backup frequency, data retention or resource assignments. IDEA creates separate backup plan for each modules (e.g: one for cluster, one for eVDI etc ..). By default, both backup plans use the same backup vaults created by IDEA.

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-19 at 10.27.37 AM.png" alt=""><figcaption><p>List of AWS Backup plans created by IDEA</p></figcaption></figure>

Click on any Backup plan to get more details about it.

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-19 at 10.31.40 AM.png" alt=""><figcaption><p>Details of a AWS Backup plan</p></figcaption></figure>

**Backup rules (red section)**

By default, IDEA creates one backup rule with the following parameters:

* Backup will start every day at 5AM UTC (blue section)
* Backup will expire after 1 week (orange section)
* Backup is stored on the encrypted vault created by IDEA (purple section)

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-19 at 10.33.58 AM.png" alt=""><figcaption><p>Example of AWS Backup rule</p></figcaption></figure>

{% hint style="info" %}
If needed, you can edit this rule (or create a new one) to match your company backup strategy.
{% endhint %}

#### **Resource Assignments (green section)**

By default, IDEA backup all data using the combination of Tags listed under the "Tags" section of the "Resource Assignment" section. In the example below, AWS Backup will backup all [#supported-resources](backup-idea-environment.md#supported-resources "mention") that match the combination of both `idea:BackupPlan = cluster` and `idea:ClusterName = idea-beta` tags.

<figure><img src="../../.gitbook/assets/Screen Shot 2022-11-19 at 10.37.26 AM.png" alt=""><figcaption><p>Resource Assignment will let you configure what resource(s) you want to backup</p></figcaption></figure>

#### Supported Resources

AWS Backup integration on IDEA supports the following resources:

* EC2 instances
* EBS disks
* EFS filesystems
* DynamoDB

#### How to add/remove resources to the backup plan <a href="#how-to-addremove-resources-to-the-backup-plan" id="how-to-addremove-resources-to-the-backup-plan"></a>

Remove the tags listed on the [#resource-assignments-green-section](backup-idea-environment.md#resource-assignments-green-section "mention") If you want to exclude specific resources from the backup plan without deleting the entire plan.

#### How to restore a backup? <a href="#how-to-restore-a-backup" id="how-to-restore-a-backup"></a>

On the left sidebar, click "Protected Resources" then choose the resource you want to restore

![Access the Backup Restore menu from the left sidebar](https://awslabs.github.io/scale-out-computing-on-aws/imgs/backup-plan-8.png)

This will open a new window with additional information about this resource (either EFS or EC2). Select the latest entry you want to restore from the "Backups" section then click "Restore"

![Click restore button to restore an EC2 instance from a AMI created by AWS Backup](https://awslabs.github.io/scale-out-computing-on-aws/imgs/backup-plan-9.png)

This will open a regular EC2 launch instance or EFS wizard. Specify the parameters (VPC, Subnet, Security Group, IAM role ...) you want to use and click "Restore Backup"

#### How to delete a backup ? <a href="#how-to-delete-a-backup" id="how-to-delete-a-backup"></a>

Select your vault, choose which recovery point you want to remove under the "Backups" section then click "Delete".

![Delete a AWS backup](https://awslabs.github.io/scale-out-computing-on-aws/imgs/backup-plan-10.png)

#### Check the status of the backup jobs <a href="#check-the-status-of-the-backup-jobs" id="check-the-status-of-the-backup-jobs"></a>

On the left sidebar, check "Jobs" to verify if your backup jobs are running correctly

![Check Backup restoration job](https://awslabs.github.io/scale-out-computing-on-aws/imgs/backup-plan-5.png)

#### What happen if you delete your CloudFormation stack? <a href="#what-happen-if-you-delete-your-cloudformation-stack" id="what-happen-if-you-delete-your-cloudformation-stack"></a>

Your backup vault won't be deleted if you have active backups in it. In case of accidental termination of your primary CloudFormation template, you will still be able to recover your data by restoring the EFS and/or EC2. To delete your AWS Backup entry, you first need to manually remove all backups present in your vault.
