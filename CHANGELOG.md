# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.1.8] - 2024-07-17

### Notes
* This upgrade does require an update to the global settings. Please review [Global Settings Upgrade](https://docs.idea-hpc.com/first-time-users/cluster-operations/update-idea-cluster/update-idea-backend-resource#global-settings-backup-and-upgrade) before upgrading.
* Removed `RHEL 7` and `CentOS 7` due to EOL on 6/30/2024
  * Remove software stacks and existing eVDI deployments that are running `CentOS 7` or `RHEL 7` BEFORE upgrading to IDEA 3.1.8

### Features
* Support for Ubuntu 22.04.04 in eVDI
  * Full support for Kernels up to `6.2.0-1018-aws`
* Added GPU Driver support for `g6` instance types

### Changes
* Update AWS CDK from `2.137.0` to `2.147.3`
* Update NVIDIA GPU drivers used during installation
  * Production `550.54.15` to `550.90.07`
* Update Python Requirements
  * Pin `requests` to `2.31` for ideactl
* Move from `apt-get` to `apt` in Dockerfile

### Bug Fixes
* Fix `global-settings` indent error for Ubuntu DCV keys
* NICE DCV GL Package removed from Bootstrap
* Fix job submissions failing when requesting existing FSx Lustre file systems
* Fix job designer variable checks for required variables used in Jinja2 conditional statements
* Fix Windows eVDI instances getting stuck in an initializing state when resuming

### Known Caveats
* Internal DNS zone uses `.local` which should be reserved for mDNS per RFC6762
* DCV USB Forwarding not available on 
  * `RHEL 9`
  * `Rocky 9`
  * `Ubuntu 22.04` with kernel newer than `6.2`
* No Lustre client for Ubuntu with kernel newer than `6.2`
* When accessing multiple IDEA deployments at once in Safari, SSO logins hang for environemnts that were loaded after the first. Also exists in prior releases.
* When using SSO and Chrome, inactive tabs time out more quickly than desired. Also exists in prior releases.
* Docs need some updates to reflect addition of Ubuntu and removal of RHEL 7 and CentOS 7

## [3.1.7] - 2024-06-01

### Notes
* This upgrade does require an update to the global settings. Please review [Global Settings Upgrade](https://docs.idea-hpc.com/first-time-users/cluster-operations/update-idea-cluster/update-idea-backend-resource#global-settings-backup-and-upgrade) before upgrading.
* Pre-upgrade script `scripts/pre-upgrade-317.sh` will assist by outputting the cluster settings command to update Base OS AMI as well as rename occurrences in settings for schedule `STOP_ALL_DAY` to `STOP_ON_IDLE`.
* Schedule Update script included in scripts to be able to update existing sessions to `STOP_ON_IDLE` via API

### Features
* First truly Open Source IDEA Release!
* Documentation combined into IDEA monorepo under `docs` folder
* Multi Architecture Administrator Docker Image
  * Added build-push-multi task to use buildx to create the IDEA Admin container and auto-push to ECR
* Added DCV Checks in the eVDI Auto Power off event. This will check CPU, DCV, and SSH connections based on the idle time setup before stopping eVDI hosts
  * Renamed schedule `STOP_ALL_DAY` to `STOP_ON_IDLE` to better represent stop schedule behavior
  * Set default schedule for all days to `STOP_ON_IDLE`
* Login page will show a Login With SSO button when SSO is enabled.

### Changes
* Update AWS CDK from `2.95.1` to `2.317.0`
* Update Node from `16.20.2` to `18.20.2`
* Update Python from `3.9.18` to `3.9.19`
* Update NVM from `0.39.5` to `0.39.7`
* Update NPM from `9.8.1` to `10.5.2`
* Update OpenMPI from `4.1.5` to `5.0.3`
* Update EFA Driver from `1.25.1` to `1.31.0`
* Update DCV and components from `2023.0` to `2023.1`
* Update NVIDIA GPU drivers used during installation
  * LTSB from  `470.199.02` to `470.239.06`
  * Production `535.104.05` to `550.54.15`
* Update all AMIs for Base OS and Software Stacks. Update Amazon Linux 2 AMI to Kernel 5.10
  * Pre-Upgrade script can assist in updating AMI in DDB Settings. Software Stacks will remain untouched during upgrade
* Implement Renovate for dependency tracking
  * Update all applicable Python packages per Renovate best-practices config
    * `sanic` left at `23.6.0` for socket authorization issues in `ideactl`
  * Dependency pinning and some dependency updates for Cluster Manager Web App node packages
* Added gcc and python3-dev to Docker image for building Python requirements
* Removed AWS Corporate ECR dependency in `idea-admin.sh`
* New Active Directory users will get their account name applied to the givenName and sn attributes to satisfy directory import syncing with Okta requirements
* Updated Help menu in Cluster Web App to direct issues to GitHub
* Update references to old Github Repo
* Update references to old Docs URL
* Added AMI update scripts in `scripts/dev` to update AMI versions for base and software stacks. These will be worked into devtool at a later time
  
### Bug Fixes
* Fixed FSx for Lustre allowed size mismatch between AWS and IDEA
  * This likely needs a re-work for different FSx Lustre types.
* Fixed NVIDIA GPU Driver install for Amazon Linux 2 with Kernel 5.10
* Remove trailing whitespace from ENI IDS for tagging in instance bootstrap
* Fixed HPC Job Name pydantic validation to allow strings, integers, and floats instead of just strings for HPC job names submitted from CLI
* Set PBS config on eVDI Nodes to use the Rt53 scheduler record instead of the ec2 hostname. This will enable existing eVDI nodes to access PBS with an upgraded scheduler with a new ip.
* Fix root storage size calculation for new eVDI nodes with hibernation enabled
* Fix resuming Hibernated eVDI nodes
* Fixed ID Token claims for SSO enabled clusters. Added Cognito pre token generation Lambda to stuff custom attributes to ID token
* Fix License Server Check script permissions
  * Cluster home directory `/apps/idea-<cluster name>` from `700` to `701` to allow traverse to script directory
* Fix Home Dir permissions for New Users from `700` to `710` to allow file sharing when using `Add User to My Group` feature
* Fix scratch storage mount permissions for HPC jobs on Linux
* In `idea-admin.sh` sso configuration, multiple scopes can be submitted seperated by `,` this will be replaced with a space in Cognito

### Known Caveats
* Data model was updated in 3.1.6, devtool web-portal.typings needs to be re-worked to support changes in Pydantic v2 and IDEA data model
* Developer documentation needs some re-work / updates
* When using OpenLDAP and doing an upgrade, replacement of the directoryservice instance removes existing directory entries. This can be avoided by backing up and restoring post upgrade or using ideactl on cluster manager to sync groups and users from DynamoDB into OpenLDAP. Generally there is not an explicit need to upgrade the directoryservice module.
* After upgrading the scheduler module, job id's reset to 0. This is purely cosmetic and does not affect functionality. However, users tracking analytics by job id may want to be aware of this upon upgrade.
* Tests need some re-work
* Node dependencies need updating in more places. Cluster Manager Web App
* Python needs updating. 3.9 is EOL October 2025


## [3.1.6] - 2023-10-20

### Features

* Create all EFA interfaces for instances that support multiple-EFA interfaces (when an HPC job sets `enable_efa` to `True`)
* Added support for `Red Hat Enterprise Linux 9.2` and `Rocky Linux 9.2` as supported operating systems for VDI and compute nodes.
  * **NOTE:** Subscription to `Rocky Linux 9` in AWS Marketplace (https://aws.amazon.com/marketplace/pp/prodview-ygp66mwgbl2ii) is required to access Rocky Linux AMIs.
  * `Red Hat Enterprise Linux 9.2` and `Rocky Linux 9.2` are not currently supported as installation / infrastructure nodes.


### Changes
* Improvements to Active Directory join process for Windows eVDI sessions.
  * Improved debug logging for troubleshooting
  * Add minimum floor of `3` seconds to wait before attempting AD join
  * Auto-retry eVDI Active Directory process to reduce the chance of a join failure in busy environments
  * Active Directory join timers are now controlled with new configuration options
    * `directoryservice.ad_automation.ad_join_min_sleep` - The minimum sleep interval before attempting join (per loop)
    * `directoryservice.ad_automation.ad_join_max_sleep` - The maximum sleep interval before attempting join (per loop)
    * `directoryservice.ad_automation.ad_join_retry_count` - The retry count (loop count) to attempt Active Directory joins
  * Allow the `cluster-manager` to supply an `OU` and `hostname` back to the joining client.
    * This can be used to move `hostname` generation logic and `OU` sorting to `cluster-manager` (customizations required)
    * Auto-rename the host on AD join when `cluster-manager` supplied hostname differs from the hostname
* Dashes (`-`) are now allowed in IDEA usernames
  * Dashes are not permitted at the start or end of a username, or appearing consecutively
* `cluster-manager` will now mask the content of the API request `FileBrowser.SaveFile` (used when editing/saving a file in the browser)
* NICE DCV historic session information from the internal API is available for up to `1-hour` after the DCV server has been removed
* NICE DCV servers can be purged from API responses after being unreachable for `900` seconds
* Update NVIDIA GPU drivers used during installation
  * LTSB from `470.141.03` to `470.199.02`
  * Production `525.105.17` to `535.104.05`
* Update OpenPBS Scheduler from `v22.05.11` to `v23.06.06`
* Update Python from `3.9.16` to `3.9.18`
* Update AWS CDK from `2.93.0` to `2.95.1`
* Updated boto3 from `1.28.12` to `1.28.44`
* Update all Classes to be compatible with `Pydantic v2`
* Update OpenAPI spec to `3.1.0`
* Update swagger links to newer OpenAPI (`3.1.0`) compatible versions
* Allow `VDC Controller` IAM permissions for Auto Scaling Groups
* Misc 3rd party package updates

### Bug Fixes
* In the File browser right-click / context menu - the `Open Selection` action didn't open the file.
* Updated installer policies for missing IAM permissions
* AWS tag `JobOwner` was not being set for eVDI sessions. This has been fixed.
* Misc PEP cleanups


### Known Caveats
* FSxL is not supported on Rocky9 and RHEL9
* EFA is not supported on RHEL9
* RHEL9 / Rocky9 is not able to be used as infrastructure nodes (IDEA/NICE DCV application nodes)


## [3.1.5] - 2023-09-06

### Features
* Install XDummy driver for non-gpu Linux console eVDI sessions
* Add command-line options (`--custom-permissions-boundary`, `--cloudformation-execution-policies`, `--public-access-block-configuration`) for `idea-admin.sh bootstrap` sub-command. These are passed to the underlying `cdk bootstrap` command as needed to support IAM permissions boundary and S3 bucket restrictions during the `cdk bootstrap` phase.
  * Users may also need to customize `cdk.json` for the IDEA-created IAM roles to attach IAM permissions boundaries, depending on their specific AWS Account policy. See [this blog post](https://aws.amazon.com/blogs/devops/secure-cdk-deployments-with-iam-permission-boundaries/) for additional information regarding AWS CDK and IAM permission boundaries.
* In Active Directory environments - added a cache for discovered Active Directory information (domain controller IP addresses)
* Support added for [HPC7a](https://aws.amazon.com/ec2/instance-types/hpc7a/) and [P5](https://aws.amazon.com/ec2/instance-types/p5/) instance families
* Added `delete-backups` sub-command for `idea-admin.sh`


### Changes
* Update AWS EFA Installer from `1.23.1` to `1.25.1`
* Improve ability for IDEA SDK consumers (such as `scheduler` module) to support newly launched AWS EC2 instances on the same day of launch. This changes the authoritative source of instance type validity from the installed boto3 to the AWS API call `describe_instance_types()` and provides a caching method.
* Adjust Project budget display (and other displays of currency) to be formatted with commas
* Change default instance types to be `m6i` family from `m5` family in keeping with current generations of available instance families. Older family instances can still be updated in the generated YAML configuration files before `config update`.
* Support egress IPv6 traffic in security groups where IPv4-traffic was permitted
* Split apart VDC Broker Security group generation
* Update OpenSearch default engine from `2.3` to `2.7`
* AWS CDK `2.63.0` -> `2.93.0`
* AWS CDK template updates from `v14` to `v18`
* Set the FSx/Lustre filesystem version to `2.15` for newly created filesystems
* Updated default scratch size to `60GiB` in the WebUI (when editing Queue profiles -> EBS Scratch Provider)
* Misc 3rd party package updates

### Bug Fixes
* Fixed missing IAM permission for VDC Controller scheduled event transformer lambda when using a customer-managed KMS key for SQS. This would have impacted the ability for eVDI schedules to apply to sessions.
* Fixed an issue that prevented `Red Hat Enterprise Linux 8.7` from launching VDI sessions on `G4ad` instances
* Windows eVDI instances were not correctly tagging their underlying EBS volumes and Elastic Network Interfaces. This has been fixed (Linux eVDI was not impacted)
* HPC Job DryRun functionality was not properly sending EBS Encryption settings. This could cause jobs to fail a DryRun in environments with Service control policies (SCPs) that required Encrypted EBS volumes. The actual job submission would work properly but the DryRun requirement was a gate that was required to be passed first.
* Restore ability to have spaces in the Project `title`
* During `GovCloud` installation - display a list of AWS profiles for the commercial profile versus requiring the user to type it in.
* Correct a defect that was not allowing the selection of Tenancy Choices for a Software stack.
* Improve eVDI subnet retry logic for both Capacity exceptions and Unsupported Instance exceptions
* OpenSearch domains that were not completely deployed were being listed during `idea-admin.sh config generate --existing-resources`
* When generating a stack from a session - make sure to copy the minimum storage and projects from the session. This should restore the ability to create stacks from existing sessions.
* Fixed WebUI modal for Session Sharing permissions appearing with a dark blue header no matter what the selected theme is
* The example configuration displayed in the SSH Access screen was missing the `Hostname` (IP address) when the bastion was deployed in Private subnet scenarios
* Misc PEP cleanups


### Known Caveats

## [3.1.4] - 2023-07-25

:heavy_exclamation_mark: - *Please note the IDEA ECR Repository location has changed as of `3.1.4`*

Users of older `idea-admin.sh` and `idea-admin-windows.ps1` may need to manually update these files for the new repo location (`public.ecr.aws/h5i3y8y1/idea-administrator`).


### Features
* Added support for `Launch Tenancy` for eVDI software stacks. This allows the IDEA administrator to configure [EC2 Launch Tenancy](https://docs.aws.amazon.com/autoscaling/ec2/userguide/auto-scaling-dedicated-instances.html).


### Changes
* Reduced IAM actions for SQS and SNS in scheduler, VDC broker, and VDC Host to least required privileges
* eVDI session tiles now display the Project of the associated eVDI session (along with putting this information in boxes for clarity)
* Updates to the ECR Repo alias/location
* Add additional logging during (`debug` logging profile) for some latency sensitive operations such as Active Directory lookups.


### Bug Fixes
* In Active Directory (AD) environments - the DynamoDB AD automation table would continue to grow and not properly expire old entries.
* In Active Directory environments - an incoming Windows eVDI session was not using the same domain-controller that `cluster-manager` pre-created the object. This could lead to a race condition where the incoming client would fail to join the domain properly as the AD object had not replicated within the domain.
* In Active Directory environments - users were not displayed properly as members of a group on the Active Directory `Member Of` tab within Active Directory Users and Computers.
* Restore AMI IDs for `us-gov-west-1` region (missing since `3.1.3`)
* Fixed a bug that prevented `userdata_customizations.sh` from executing for jobs that require EFA driver to be installed
* Fixed a syntax error in the `robots.txt` on `cluster-manager` that allowed bots to index IDEA.
* Expand skipping service quotas to batch queues when `scheduler.job_provisioning.service_quotas` is set to `False`
* Added missing IAM actions to installer policies
* Project titles were allowed to exceed character limits in some cases. Project titles are now restricted to `3-32` characters.
* Under certain conditions - the `cluster-manager` task manager could have difficulty keeping up with task execution requirements. Additional configuration parameters have been added to address this.


## [3.1.3] - 2023-06-16

### Features

* Added support for `Red Hat Enterprise Linux 8.7` and `Rocky Linux 8.7` as supported operating systems for VDI and compute nodes.
  * **NOTE:** Subscription to `Rocky Linux 8` in AWS Marketplace is required to access Rocky Linux AMIs.

* `ideactl` now supports adding multiple users to multiple named groups in a single command. For example: `ideactl groups add-user-to-group --username user1 --username user2 --groupname group1 --groupname group2`  will add both `user1`  and `user2`  to groups `group1` and `group2`
* Added support for new instance families: `i4g`, `inf2`, and `trn1n`
* eVDI session startup now validates that there is remaining budget for the associated project. If there is no remaining budget a session cannot be started for the project. This is controlled via the new configuration setting `vdc.controller.enforce_project_budgets`  (defaults to `True` ).

### Changes

* Update boto3 from `1.26.61` to `1.26.138`
* Update requests module from `2.27.1` to `2.31.0`
* ALB deployments will now set the option to Drop Invalid Headers
* Default Web/API page size increased from `20` to `50`
* Update AMI IDs for all supported operating systems
* Update AWS EFA Installer from `1.22.1` to `1.23.1`
* Update DCV Server from `2023.0-14852` to `2023.0-15065`, DCV Session Manager Agent from `2023.0.642` to `2023.0.675`, and DCV viewer from `2023.0.5388` to `2023.0.5483`
* Reduce the default DCV idle disconnect from 24-hours to 4-hours
* Update Nvidia drivers from `510.47.03` to `525.105.17`

### Bug Fixes
* `ideactl ldap search-nnn `on `cluster-manager` did not properly return all results when the results spanned multiple pages of results. This has been fixed.
* Newly created software stacks were not set to enabled  by default.
* Deleting a cluster with a large AWS Backup Vault could have resulted in error and the need to delete the Vault manually.
* eVDI owner permissions that had de-selected the `builtin`  and customized permissions would render the eVDI session unusable even to the session owner. This has been corrected and custom owner permissions work as expected.

### Known Caveats

* `Red Hat Enterprise Linux 8.7` and `Rocky Linux 8.7` do not launch VDI sessions on `G4ad` instances due to AMD GPU driver kernel version dependencies.
* AWS EFA installer doesn't install successfully on `Rocky Linux 8.7`


## [3.1.2] - 2023-05-09

### Features

* Expand proxy support for VPCs without VPC endpoints
* Validate that the user password conforms to Cognito user pool password policy requirements before creating the user in the user pool
* Added `ideactl`  support for `add-user-to-group`  and `remove-user-from-group `
* Expand support for customer-managed KMS keys to `DynamoDB`, `OpenSearch`, `Kinesis`, and `EBS`
* Support use case of having a unique customer-managed KMS key for each of the following AWS service: `Secrets Manager`, `SNS`, `SQS`, `DynamoDB`, `EBS`, `Backup`, `OpenSearch`, `Kinesis`, `EFS`, and `FSx for Lustre`
  * `EBS` customer-managed key needs the following service-roles to be added as key users: AWSServiceRoleForAutoScaling, AWSServiceRoleForEC2Fleet, AWSServiceRoleforEC2SpotFleet. Post IDEA cluster deployment, IDEA VDC Controller IAM role also needs to be added as a key user.
  * `SQS` customer-managed key needs customization to grant SNS service-principal access per https://docs.aws.amazon.com/sns/latest/dg/sns-enable-encryption-for-topic-sqs-queue-subscriptions.html
* New options to control eVDI subnet use/selection can be found under `vdc.dcv_sessions.network`:
  * Allow for eVDI subnets to differ from HPC/compute subnets in the configuration. By default, the same subnets are configured. This can be changed on a running cluster without a restart.
  * Allow for `ordered`  or `random`  subnet selection during eVDI launching. Default subnet selection is `ordered` .
  * Allow for automatic retry of eVDI subnets during creating eVDI resources. Default is to `auto-retry` the next subnet. This may be disabled in situations to avoid cross-AZ charges with eVDI resources accessing resources in other AZs.
* Allow the IDEA Administrator to define NICE DCV USB remotization devices that will apply to the eVDI fleet. USB filter strings can be added to `vdc.server.usb_remotization`  (list) for USB client-side devices to be enabled for USB remotization.
* Added terminate when idle support to `AlwaysOn` capacity

### Changes

* AWS EFA Installer updated from `1.22.0`  to `1.22.1`
* Update DCV Server from `2022.1-13300` to `2023.0-14852`, DCV Session Manager Agent from `2022.1-592` to `2023.0-642`, DCV Connection Gateway from `2022.1.377` to `2023.0.531`, DCV Session Manager Broker from `2022.1.355` to `2023.0.392`, and DCV viewer from `2022.1.4251` to `2023.0.5388`
* Changes to WebUI / notification icon - Password expiration warning will only appear at `<10days`.  Remove the default pip on the icon indicating a waiting notification.
* eVDI hosts will now populate `/etc/environment`  with two additional environment variables that can be used by bootstrap scripting / post-boot customization. `IDEA_SESSION_OWNER`  and `IDEA_SESSION_ID` .
* When submitting a job from the WebUI - the job name  will now default to the filename with the `.`  character replaced with `_`  as `.`  is not allowed in job names.
* Changed front-end WebUI API request timeout from `10-seconds` to `30-seconds` to accommodate longer lookups/lists on back-end directory services.
* Changed user drop-down lists to search instead of select for clusters with a large number of users.

### Bug Fixes

* Fixed an issue that prevented user VDI sessions to successfully transition from `Stopping` to `Stopped` state
* Set `PBS_LEAF_NAME` to the compute node hostname to address an issue if compute node AMI has more than one network interface
* The incorrect version number for IDEA was displayed in the Web console
* eVDI sessions were launched with EBS volume encryption tied to the Hibernation setting
* The download link for NICE DCV Session manager agent for Windows was incorrect
* Log files are now encoded in `UTF-8` encoding. This allows for logging of eVDI session names with UTF-8/multibyte characters. Previously this would cause a traceback.
* A bug was preventing the cluster timezone from properly being detecting in some modules. The timezone would default to `America/Los_Angeles`  for some situations even when the `cluster.timezone`  was properly set. This would cause eVDI schedules to operate in `America/Los_Angeles`  instead of the cluster timezone.
* Fixed a bug that prevented updates to DCV connection gateway certificate ARNs when private certificates are used
* Fixed a bug that prevented SSH access to IDEA infrastructure instances when `CentOS7` is used
* Addressed user-scale issue of querying Cognito API for user-status in bulk list users.



## [3.1.1] - 2023-03-10

### Features

* Enable IDEA to deploy in `isolated subnets` (no NAT Gateway) utilizing combination of VPC Endpoints and a customer-managed proxy. The proxy is used to access public repos and AWS service endpoints that don't support VPC Endpoints (Pricing, ServiceQuotas, DynamoDB Streams, Cognito, Directory Service).


### Bug Fixes

* When adding a user that does not conform to the Cognito pool password policy a user record is still created in the User Pool without the non-conforming password. This has been updated to remove the non-conforming user account during a password policy failure.
* Running `delete-cluster`  with a previously configured cluster may cause problems in removing the Cognito User Pools.

### Changes

* Remove AWS CLI v1 (for AL2) and install AWS CLI v2 to remain consistent with AWS CLI v2 as a requirement.
* Update AWS EFA Installer from `1.21.0`  to `1.22.0 `
* Update OpenMPI from `4.1.4`  to `4.1.5`
* `G5  Instance Family` are now permitted in the default VDI configuration

### Security

* Reduced IAM permissions required for `analytics-sink-lambda` function from `ec2:*` to the required permissions
* When configured for SSO - only allow `clusteradmin` to use Cognito-local authentication. Other users will not be permitted to use Cognito local authentication. A new logging message has been created to log the attempt.



## [3.1.0] - 2023-02-20

### Features

* Enable IDEA installation to use an existing Active Directory for directory services. Several behavior changes take place and the dedicated document should be consulted to understand these changes.

* Cognito User Pool now contains the standard AWS Tags for the IDEA cluster name.
* Amazon OpenSearch Service - Default engine version updated to OpenSearch 2.3  for new installations.
* Enhanced `delete-cluster` to delete CloudWatch Log Groups.
* Added option `--delete-all` for `delete-cluster`. This will delete bootstrap, backups, dynamodb tables, and cloudwatch log groups.
* Support for new instance families:   `c6in`, `m6in`, `m6idn`, `r6in` and `r6idn`
* (`cluster-manager`) - Added `groups`  subcommands for group add/delete/enable/disable/listing


### Changes

* AWS CDK updated to `2.63.0` (requires development environment update)
* boto3 / botocore updates (`1.26.61 / 1.29.61`)
* Various Python library updates (ujson, troposphere, jsii)
* **NOTE** - Development environment updates are required due to CDK and package changes.
* Shell script automations now have a min/max sleep time. Previously a random sleep interval of `0`  could be encountered.
* **Revert** max username length from 32-characters to 20-characters to maintain Active Directory compatibility.
* Joins to Active Directory now make use of a generated IDEA Hostname  with a configured prefix (`directoryservice/settings.yml`).
* misc typos / text cleanups


### Security

* `IMDSv2 support - Phase-2` - Added ability for IMDSv2 version enforcement for infrastructure and user VDI instances.
* Run SSH key conversion via `puttygen`  as the requesting username vs. root to decrease potential for permissions problems.
* AD preset/joins now take place with 120-character computer object passwords.
* The Cognito User Pool is now created with Deletion Protection activated. This prevents an accidental deletion from the console or CloudFormation. A delete-cluster  operation from idea-admin.sh  will automatically remove this attribute when deleting a cluster.
* The Cognito User Pool now deploys with Advanced Security Mode - Audit enabled by default (configurable via `identity-provider/settings.yml` ).  See documentation for additional details and the metrics that are published to CloudWatch: https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pool-settings-advanced-security.html


### Bug Fixes

* CloudWatch alarms related to DynamoDB scaling are now automatically deleted when the dynamodb tables are deleted.
* AD Joins were failing when the hostname length was being truncated between the pre-creation of the AD object by `cluster-manager`  and the actual join by the node. AD joins will now use a generated IDEA hostname to properly join AD. This hostname is limited to 15characters in length for AD compatibility reasons.
* Under certain circumstances a non-admin user would be unable to see a listing of projects with a DirectoryService back-end of Active Directory.
* A race condition during IAM role creation and policy application could sometimes cause a failure and rollback during installation of the cluster  stack, preventing IDEA installation from proceeding.
* The IDEA installer only allowed specifying an IPv4 /32 during installation. This has been corrected to allow valid IPv4 CIDR notations.
* Active Directory configuration was not creating Computer objects in the configured OU. Computer Objects were being created in the default OU/location in the domain which may not be permitted by the service user.
* The IDEA installer would fail when selecting a new VPC and to use VPC Endpoints.
* Bug fix for `idea-admin.sh shared-storage attach-file-system`  to properly query and configure CIFS share names for FSx for NetApp ONTAP
* Bug fix for platform identification used during SSH key generation
* Bug fix for creating PPK SSH key with correct user ownership
* When performing an IDEA installation - under rare circumstances the analytics  stack may fail with an error: The role defined for the function cannot be assumed by Lambda. Retrying the deployment will generally recover the installation. This has been corrected.
* Cluster Status displayed an unhealthy status for modules that were not deployed by the IDEA administrator. These now show as 'Not Applicable'.
* The `DeleteSession`  eVDI API was not functioning correctly.
* During installation of the analytics  stack - there could be a race condition that prevented installation. This has been corrected.
* Enforce matching job_owner  to JWT during API requests.
* Under certain conditions the `config generate`  phase could fail in regions due to Amazon File Cache not being deployed. This has been corrected.
* The eVDI / DCV Connection Gateway process may not properly restart after an instance restart. This has been corrected.


### Known Caveats

* When using a custom DNS domain name - the Administrator must update the Cognito invite template from the AWS console or the URL sent to users will not be the custom DNS name.
* Performing a `delete-cluster`  while AWS Backup Vault has running jobs will result in an error and the cluster delete will fail.
  * **Workaround**: Retry the `delete-cluster`  outside of the Backup Vault window when the recovery point can be deleted properly.
* After the removal/deletion of a cluster from an AWS account - there may still be artifacts related to the old cluster:
  * OpenSearch statistic entries / Job history if using `--existing-resources`

* Using a default configuration of NFSv4 for EFS filesystems will still attempt to validate/install the Amazon EFS Mount helper
* Nested `shared-storage`  modules are not currently supported and may not mount in the correct order
* Using the job-level parameter for `fsx_lustre`  is only supported for creating ephemeral/new FSx/Lustre deployments. Attaching to existing FSx/Lustre deployments at a per-job-level is not supported in this release. Attaching via the shared-storage  module for existing FSx/Lustre filesystems is supported.
* Some WebUI elements are still visible but without functionality when using Active Directory and SSO. (e.g. Forgot Password). These elements should be avoided in Active Directory / SSO configurations and will be automatically disabled in future releases based on the configuration settings.
* By default - the Cognito User Pool uses Cognito for sending email invitations for new users. There is a limit of 50  emails (invites) per day in this configuration.
  * **Workaround**: Configured Amazon Simple Email Service (SES) and configure the Cognito pool to use SES and a verified sender-id.



## [3.0.0] - 2022-12-30

### Features

* Add support for Amazon File Cache filesystems to shared-storage module.
* Support for GovCloud region `us-gov-west-1` . GovCloud region `us-gov-east-1`  is not supported at this time.
* Perform Lustre client performance tuning on high-performance instances (64core+, 64GiB+)

### Changes

* AWS EFA Installer updated to `1.21.0 `
* IDEA Python updated to `3.9.16 `
* Lustre client updated to `2.12`
* Changes to integration-test infrastructure to leverage `IMDSv2`
* Changes to integration-tests to clean up any AWS Backups that are deployed and log more environment variables during the run
* Connect anonymous metrics for telemetry information about the AWS Solution
* Revised eVDI `allowlist/denylist` functionality to allow fine-grained control of instances. Supports both instance family and specific instance conventions.
* Build python in the bootstrap directory vs. `/tmp`  - this is more compatible with AMIs that have `noexec`  mount policy for `/tmp` .
* Refactor launch configurations into Launch Templates for `cluster-manager` and eVDI ASGs
* Reduce memory consumption of `qstat`  data collection by not including the environment variables. Also corrects PBS that emits poorly formatted JSON.
* Disable computer renaming to prevent AD problems
* Change idea-admin.sh  "detect my IP address" functionality to use `checkip.amazonaws.com`
* Usernames can now be up to 32-characters and including the `_`  character
* Instance families `m6a`  and `g4ad`  added to default VDI allowlist
* Internal improvements to the testing framework and unit tests
* Add `availability_zone_id` to PBS `resouredef` for jobs (unused)
* Improve the behavior of installation in dense EFS environments
* Prevent shared-storage from listing unhealthy filesystems


### Security

* Security: Support mounting EFS filesystems via the Amazon EFS Helper  , providing TLS protection of NFS traffic / Data in transit encryption. (non-default)
* Security: `IMDSv2 support - Phase-1` - Ephemeral nodes now make use of IMDSv2 when connecting to Instance Metadata Service
* Security: prevent user enumeration via timing attacks/analysis
* Security: Prevent rendering in iframe
* Security: reduce refresh token to 12hrs
* Security: Prevent LFI in limited scenarios
* Security: Invalidate refresh token on all devices during password change
* Security: prevent log injection
* Security: OpenLDAP now uses `SSHA/SHA512` for password storage
* Security: Allow ELBs to have a configured SSL/TLS policy in the configuration
* Security: Removed unused API endpoints
* Security: `SHA384 rollout- Phase1` - Start to use `sha384`  for external objects/ downloads

### Bug Fixes

* SSO / SAML behaviors fixed.
  * SSO now requires provider-type.
  * While SSO/SAML should work for any complaint vendor - primary testing is with external providers AzureAD  and Okta

* The `patch`  command would finish and exit before the patch was completed on the deployed nodes.
* VDC API failure for users listing software packages without a defined filter
* Corrections to VDC software stack and user stack indexing
* Improved error handling for bad options/arguments to `qsub`  / scheduler
* Fixed inability for Windows session sharing in VDI when using AWS MAD
* Windows VDI sessions with AWS MAD displayed a black screen/thumbnail
* Cleaned up delete-cluster  operations to prevent race condition on bucket objects delete
* Properly check CPU utilization before enforcing scheduled actions on eVDI sessions
* Fix EVDI windows sessions and being properly added to Local Admin user context
* Correct PBS job comment when a user deletes a job from the WebUI
* Fix for `sssd` error related to config params in wrong stanza/section
* Misc other fixes/corrections

### Known Caveats

* After the removal/deletion of a cluster from an AWS account - there may still be artifacts related to the old cluster:
  * Cloudwatch autoscaling alarms for the DynamoDB tables
  * OpenSearch statistic entries / Job history if using `--existing-resources`
* Using a default configuration of NFSv4 for EFS filesystems will still attempt to validate/install the Amazon EFS Mount helper
* Nested shared-storage  modules are not currently supported and may not mount in the correct order
* Using the job-level parameter for `fsx_lustre`  is only supported for creating ephemeral/new FSx/Lustre deployments. Attaching to existing FSx/Lustre deployments at a per-job-level is not supported in this release. Attaching via the shared-storage  module for existing FSx/Lustre filesystems is supported.

