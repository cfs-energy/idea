# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [3.1.3] - 2023-06-16

### Features

* Added support for `Red Hat Enterprise Linux 8.7` and `Rocky Linux 8.7` as supported operating systems for VDI and compute nodes.
  * **NOTE:** Subscription to `Rocky Linux 8` in AWS Marketplace is required to access Rocky Linux AMIs.

* `ideactl`  now supports adding multiple users to multiple named groups in a single command. For example: `ideactl groups add-user-to-group --username user1 --username user2 --groupname group1 --groupname group2`  will add both `user1`  and `user2`  to groups `group1`  and `group2`
* Added support for new instance families: `i4g, inf2, trn1n, c6in, m6in, m6idn, r6in,  rdidn`
* eVDI session startup now validates that there is remaining budget for the associated project. If there is no remaining budget a session cannot be started for the project. This is controlled via the new configuration setting `vdc.controller.enforce_project_budgets`  (defaults to `True` ).

### Changes

* Update boto3 from 1.26.61 to 1.26.138
* Update requests module from 2.27.1 to 2.31.0
* ALB deployments will now set the option to Drop Invalid Headers
* Default Web/API page size increased from 20 to 50
* Update AMI IDs for all supported operating systems
* Update AWS EFA Installer from 1.22.1  to 1.23.1
* Update DCV Server from 2023.0-14852 to 2023.0-15065, DCV Session Manager from 2023.0-642 to 2023.0-675, and DCV viewer from 2023.0.5388 to 2023.0.5483

* Reduce the default DCV idle disconnect from 24-hours to 4-hours
* Update Nvidia drivers from 510.47.03 to 525.105.17

### Bug Fixes
* `ideactl ldap search-nnn `on `cluster-manager` did not properly return all results when the results spanned multiple pages of results. This has been fixed.
* Newly created software stacks were not set to enabled  by default.
* Deleting a cluster with a large AWS Backup Vault could have resulted in error and the need to delete the Vault manually.
* eVDI owner permissions that had de-selected the `builtin`  and customized permissions would render the eVDI session unusable even to the session owner. This has been corrected and custom owner permissions work as expected.

### Known Caveats

* `Red Hat Enterprise Linux  8.7` and `Rocky Linux 8.7` do not launch VDI sessions on `G4ad` instances due to AMD GPU driver kernel version dependencies.
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
  * Allow for eVDI subnets to differ from HPC/compute subnets in the configuration. By default the same subnets are configured. This can be changed on a running cluster without a restart.
  * Allow for `ordered`  or `random`  subnet selection during eVDI launching. Default subnet selection is `ordered` .
  * Allow for automatic retry of eVDI subnets during creating eVDI resources. Default is to `auto-retry` the next subnet. This may be disabled in situations to avoid cross-AZ charges with eVDI resources accessing resources in other AZs.
* Allow the IDEA Administrator to define NICE DCV USB remotization devices that will apply to the eVDI fleet. USB filter strings can be added to `vdc.server.usb_remotization`  (list) for USB client-side devices to be enabled for USB remotization.
* Added terminate when idle support to `AlwaysOn` capacity

### Changes

* AWS EFA Installer updated from `1.22.0`  to `1.22.1`
* Upgrade to NICE DCV `2023.0` where applicable.
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
* Log files are now encoded in `UTF-8` encoding. This allows for logging of eVDI session names with UTF-8/multi-byte characters. Previously this would cause a traceback.
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
* Support for new instances:   `C6in`, `M6in`, `M6idn`, `R6in` and `R6idn`
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
* Changes to integration-tests to cleanup any AWS Backups that are deployed and log more environment variables during the run
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

