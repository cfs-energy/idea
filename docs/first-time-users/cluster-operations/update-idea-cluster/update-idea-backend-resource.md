# Update IDEA backend resource (idea-admin.sh deploy)

## Update IDEA backend resource

{% hint style="info" %}
This page covers different methods for updating your IDEA cluster infrastructure.
{% endhint %}

## Individual Module Updates (idea-admin.sh deploy)

{% hint style="info" %}
Use the **Deploy** command if you have made an architecture update outside of core IDEA codebase (e.g: update Lambda function codebase, change ALB listeners, update filesystem mount ..). Refer to [update-idea-configuration.md](update-idea-configuration.md "mention") for other types of updates.
{% endhint %}

### Standard Upgrade Procedure

After you have made your infrastructure change, run `./idea-admin.sh cdk diff <STACK>` command to preview what infrastructure changes will be performed during the upcoming upgrade deployment.

```sh
./idea-admin.sh cdk diff cluster \
  --cluster-name <CLUSTER_NAME> \
  --aws-region us-east-2
re-use code assets for lambda: idea_custom_resource_self_signed_certificate ...
re-use code assets for lambda: idea_custom_resource_update_cluster_settings ...
re-use code assets for lambda: idea_custom_resource_update_cluster_prefix_list ...
re-use code assets for lambda: idea_ec2_state_event_transformation_lambda ...
re-use code assets for lambda: idea_custom_resource_cluster_endpoints ...
source updated for idea_solution_metrics...
building code assets for lambda: idea_solution_metrics ...

Stack <CLUSTER_NAME>-cluster
Resources
[~] AWS::Lambda::Function solution-metrics solutionmetricsAE489078
 ├─ [~] Code
 │   └─ [~] .S3Key:
 │       ├─ [-] cdk/cbd8b4dd2b809b410fd75ab3d6c95d8746c69decbab8c117f04dfebb9120a006.zip
 │       └─ [+] cdk/b25e3a038f6877199484a5531e47bfd984d7850dd69ff0d0e72cfc5c4f90c2ab.zip
 └─ [~] Metadata
     └─ [~] .aws:asset:path:
         ├─ [-] asset.cbd8b4dd2b809b410fd75ab3d6c95d8746c69decbab8c117f04dfebb9120a006
         └─ [+] asset.b25e3a038f6877199484a5531e47bfd984d7850dd69ff0d0e72cfc5c4f90c2ab
[~] Custom::ClusterSettings <CLUSTER_NAME>-cluster-settings ideapatchuclustersettings
```

Once you have reviewed your changes and everything looks correct, run `./idea-admin.sh deploy cluster --upgrade` to update your AWS environment by triggering a CloudFormation ChangeSet to the stack you are upgrading (`cluster` in this example).

```sh
./idea-admin.sh deploy  cluster --upgrade \
  --cluster-name <CLUSTER_NAME>\
  --aws-region us-east-2
deploying module: cluster, module id: cluster
re-use code assets for lambda: idea_custom_resource_self_signed_certificate ...
re-use code assets for lambda: idea_custom_resource_update_cluster_settings ...
re-use code assets for lambda: idea_custom_resource_update_cluster_prefix_list ...
re-use code assets for lambda: idea_ec2_state_event_transformation_lambda ...
re-use code assets for lambda: idea_custom_resource_cluster_endpoints ...
re-use code assets for lambda: idea_solution_metrics ...

✨  Synthesis time: 17.68s

<CLUSTER_NAME>-cluster: building assets...

[0%] start: Building 0a9c7e320d724b92457f9df9325d6f0014ba434d71d23d0c20c6afb1ca20dc79:549172027899-us-east-2
[0%] start: Building e8af6af9ddccbfe39cb70b54a772900ec28e77a49433a8f480db9779ec2a71f1:549172027899-us-east-2
[0%] start: Building 402db69e73fdd83283c5df7754892d7a47e9a026ad3d019a8e42e1dffd79946b:549172027899-us-east-2
[0%] start: Building fcdcab8ae7b888ac267e0822f4bfc89d5b97c54892fe6e3359036b79ddefe031:549172027899-us-east-2
[0%] start: Building 821317b1ea7eb21bcb9aafec5de6c305c1a076d6bea89231bef1e43f9ddc8b93:549172027899-us-east-2
[0%] start: Building b25e3a038f6877199484a5531e47bfd984d7850dd69ff0d0e72cfc5c4f90c2ab:549172027899-us-east-2
[0%] start: Building 7169cf61baf93fd8b0b1ab7a7b21bfb3097e58e1516fd70e1e4efb52873b58e9:549172027899-us-east-2
[14%] success: Built 0a9c7e320d724b92457f9df9325d6f0014ba434d71d23d0c20c6afb1ca20dc79:549172027899-us-east-2
[28%] success: Built e8af6af9ddccbfe39cb70b54a772900ec28e77a49433a8f480db9779ec2a71f1:549172027899-us-east-2
[42%] success: Built 402db69e73fdd83283c5df7754892d7a47e9a026ad3d019a8e42e1dffd79946b:549172027899-us-east-2
[57%] success: Built fcdcab8ae7b888ac267e0822f4bfc89d5b97c54892fe6e3359036b79ddefe031:549172027899-us-east-2
[71%] success: Built 821317b1ea7eb21bcb9aafec5de6c305c1a076d6bea89231bef1e43f9ddc8b93:549172027899-us-east-2
[85%] success: Built b25e3a038f6877199484a5531e47bfd984d7850dd69ff0d0e72cfc5c4f90c2ab:549172027899-us-east-2
[100%] success: Built 7169cf61baf93fd8b0b1ab7a7b21bfb3097e58e1516fd70e1e4efb52873b58e9:549172027899-us-east-2

<CLUSTER_NAME>-cluster: assets built

<CLUSTER_NAME>-cluster: deploying...
[0%] start: Publishing 0a9c7e320d724b92457f9df9325d6f0014ba434d71d23d0c20c6afb1ca20dc79:549172027899-us-east-2
[0%] start: Publishing e8af6af9ddccbfe39cb70b54a772900ec28e77a49433a8f480db9779ec2a71f1:549172027899-us-east-2
[0%] start: Publishing 402db69e73fdd83283c5df7754892d7a47e9a026ad3d019a8e42e1dffd79946b:549172027899-us-east-2
[0%] start: Publishing fcdcab8ae7b888ac267e0822f4bfc89d5b97c54892fe6e3359036b79ddefe031:549172027899-us-east-2
[0%] start: Publishing 821317b1ea7eb21bcb9aafec5de6c305c1a076d6bea89231bef1e43f9ddc8b93:549172027899-us-east-2
[0%] start: Publishing b25e3a038f6877199484a5531e47bfd984d7850dd69ff0d0e72cfc5c4f90c2ab:549172027899-us-east-2
[0%] start: Publishing 7169cf61baf93fd8b0b1ab7a7b21bfb3097e58e1516fd70e1e4efb52873b58e9:549172027899-us-east-2
[14%] success: Published 402db69e73fdd83283c5df7754892d7a47e9a026ad3d019a8e42e1dffd79946b:549172027899-us-east-2
[28%] success: Published fcdcab8ae7b888ac267e0822f4bfc89d5b97c54892fe6e3359036b79ddefe031:549172027899-us-east-2
[42%] success: Published 0a9c7e320d724b92457f9df9325d6f0014ba434d71d23d0c20c6afb1ca20dc79:549172027899-us-east-2
[57%] success: Published 821317b1ea7eb21bcb9aafec5de6c305c1a076d6bea89231bef1e43f9ddc8b93:549172027899-us-east-2
[71%] success: Published e8af6af9ddccbfe39cb70b54a772900ec28e77a49433a8f480db9779ec2a71f1:549172027899-us-east-2
[85%] success: Published b25e3a038f6877199484a5531e47bfd984d7850dd69ff0d0e72cfc5c4f90c2ab:549172027899-us-east-2
[100%] success: Published 7169cf61baf93fd8b0b1ab7a7b21bfb3097e58e1516fd70e1e4efb52873b58e9:549172027899-us-east-2
<CLUSTER_NAME>-cluster: creating CloudFormation changeset...
<CLUSTER_NAME>-cluster | 0/3 | 3:16:48 PM | UPDATE_IN_PROGRESS   | AWS::CloudFormation::Stack                | <CLUSTER_NAME>-cluster User Initiated
<CLUSTER_NAME>-cluster | 0/3 | 3:16:55 PM | UPDATE_IN_PROGRESS   | AWS::Lambda::Function                     | solution-metrics (solutionmetricsAE489078)
<CLUSTER_NAME>-cluster | 1/3 | 3:17:04 PM | UPDATE_COMPLETE      | AWS::Lambda::Function                     | solution-metrics (solutionmetricsAE489078)
<CLUSTER_NAME>-cluster | 2/3 | 3:17:40 PM | UPDATE_COMPLETE_CLEA | AWS::CloudFormation::Stack                | <CLUSTER_NAME>-cluster
<CLUSTER_NAME>-cluster | 3/3 | 3:17:40 PM | UPDATE_COMPLETE      | AWS::CloudFormation::Stack                | <CLUSTER_NAME>-cluster

 ✅  <CLUSTER_NAME>-cluster

✨  Deployment time: 93.11s

Stack ARN:
arn:aws:cloudformation:us-east-2:<REDACTED>:stack/<CLUSTER_NAME>-cluster/06a115e0-73d2-11ed-8707-026cb7c72132

✨  Total time: 110.78s
```

You can validate your upgrade by looking at the AWS CloudFormation console. The stack you are being updating should be listed as "UPDATE\_IN\_PROGRESS".

<figure><img src="../../../.gitbook/assets/ftu_ops_updatebk_inprogress.webp" alt=""><figcaption></figcaption></figure>

Once the stack is updated, you can verify the resources have been upgraded correctly. In my example, I have changed the codebase of one Lambda function. I can confirm the function has been updated successfully by looking at the new code and confirming the timestamp under "Last Modified Time" match my `./idea-admin.sh deploy` command.

<figure><img src="../../../.gitbook/assets/ftu_ops_updatebk_lastmod.webp" alt=""><figcaption></figcaption></figure>

## Global Settings Backup and Upgrade

### Overview

The `backup-update-global-settings` command performs only the global settings portion of the upgrade process:

1. Exporting current configuration to a directory
2. Creating a backup of the configuration with date stamp
3. Regenerating configuration files from values.yml
4. Updating global settings in DynamoDB

This is especially useful during development, or when you want to update global settings without performing a full upgrade of the cluster.

### Usage

The basic syntax for the command is:

```bash
./idea-admin.sh backup-update-global-settings [OPTIONS]
```

#### Required Parameters

* `--cluster-name`: Name of your IDEA cluster
* `--aws-region`: AWS region where the cluster is deployed

#### Optional Parameters

* `--aws-profile`: AWS profile to use for the operation
* `--force`: Skip all confirmation prompts
* `--module-set`: Name of the module set to use (default: default)

### Examples

#### Basic Usage

```bash
./idea-admin.sh backup-update-global-settings \
  --cluster-name idea-test1 \
  --aws-region us-east-2
```

#### With AWS Profile and Force Options

```bash
./idea-admin.sh backup-update-global-settings \
  --cluster-name idea-test1 \
  --aws-region us-east-2 \
  --aws-profile default \
  --force
```

### When to Use This Command

This command is particularly useful in the following scenarios:

1. **During development**: When you're making changes to global settings and want to test them without going through a full upgrade cycle.
2. **Configuration updates**: When you need to update global settings configuration but don't want to change the base OS or update AMIs.
3. **Troubleshooting**: When you need to regenerate the configuration files to resolve configuration issues.
4. **Before upgrades**: To create a backup of your current configuration before performing a full upgrade.

### Notes

* This command will create a backup of your current configuration in a directory with a date stamp.
* The backup will be stored in the path: `~/.idea/clusters/<cluster-name>/<aws-region>/config.golden.<date>`
* If you need to rollback, you can manually copy the backup directory to restore your previous configuration.
