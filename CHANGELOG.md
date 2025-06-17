# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Calendar Versioning](https://calver.org/).

## [25.06.2] - 2025-06-17

### **🔧 Critical Hotfixes**

This maintenance release addresses critical stability and compatibility issues in the Virtual Desktop Controller (VDC) and Cluster Manager components.

**Upgrade Instructions:**
* From `25.05.1`: Update only `vdc` and `cluster-manager` (non-service impacting)
```bash
./idea-admin.sh upgrade-cluster cluster-manager vdc --aws-region $IDEA_AWS_REGION --cluster-name $IDEA_CLUSTER_NAME
```
* From other versions: Run full `upgrade-cluster` ([documentation](https://docs.idea-hpc.com/first-time-users/cluster-operations/update-idea-cluster/upgrade-cluster))

### **🔄 Updates**
* Software Stack and Base AMIs updated
* Fixed changelog link typos

### **🐛 Bug Fixes**

* **TailFile API Optimization**: Refactored to reduce memory consumption and prevent OOM crashes in cluster-manager supervisord process
* **Windows Server 2025 GPU Support**: Fixed NVIDIA GPU compatibility by using Windows Server 2022 drivers (Windows Server 2025 drivers not yet available from AWS)

## [25.06.1] - 2025-06-10

> **📋 TL;DR - KEY HIGHLIGHTS**
> - 🚨 **Breaking:** Amazon Linux 2 no longer supported for Infrastructure Base OS
> - 📅 **Versioning:** Switched to CalVer from Semantic Versioning
> - 🛠️ **New Tool:** Unified `upgrade-cluster` command for easier upgrades
> - 💻 **OS Support:** Added AL2023, Windows Server 2022/2025, Ubuntu 22.04/24.04
> - ⚡ **Major Feature:** Script Workbench for PBS job creation in Web UI

* This release marks the change to [CalVer](https://calver.org/) from Semantic Versioning

### **⚠️ BREAKING CHANGES** - Read Carefully!

**Amazon Linux 2 Support for Infrastructure Base OS Removed**
* Amazon Linux 2 is no longer supported as an Infrastructure Base OS
* **Action Required:** The new `upgrade-cluster` tool will assist with updating the Base OS and AMI for Infrastructure Hosts as part of the upgrade process
* **New Default:** Amazon Linux 2023 is now the default for new installs

### **🚀 Upgrade Paths**

There are two recommended upgrade paths:

1. **⭐ Recommended: Use the new unified upgrade-cluster command** (simplest approach)
   ```bash
   ./idea-admin.sh upgrade-cluster \
     --aws-region us-east-2 \
     --cluster-name idea-test1
   ```
   This command handles all the necessary steps including base OS updates, global settings backup and regeneration, and module upgrades in a single operation.

   Detailed information about the `upgrade-cluster` command can be found in the [IDEA Upgrade Documentation](https://docs.idea-hpc.com/first-time-users/cluster-operations/update-idea-cluster/upgrade-cluster).

2. **Alternative: Manual update process**

   If you prefer more control over the upgrade process, follow the detailed instructions in the [IDEA Upgrade Documentation](https://docs.idea-hpc.com/first-time-users/cluster-operations/update-idea-cluster/update-idea-backend-resource).

### **📋 Post Upgrade**

**Merge Software Stacks (Optional)**

   After completing the upgrade, you can run `ideactl merge-software-stacks` from the VDC Controller to merge new default software stacks from the release into your cluster.

## **🆕 Major New Features**

### **Script Workbench**
Added [Script Workbench](https://docs.idea-hpc.com/modules/hpc-workloads/user-documentation/script-workbench) - a game-changing feature that allows users to create, validate (dry run), and submit PBS job scripts directly from the Web UI with real-time directive validation and cost estimation.

### **Expanded Operating System Support**
* **Amazon Linux 2023** support for eVDI, Infrastructure, and Compute hosts
* **Windows Server 2022 & 2025** support for eVDI
  * All Windows eVDI nodes now bootstrap GPU drivers and DCV packages allowing for more modern instance types and increased flexibility
* **Ubuntu 22.04 and 24.04** HPC Job support
* **RHEL 8/9 & Rocky 8/9** Infrastructure host support
* **FSx Lustre client** for Ubuntu 24.04

### **New Unified Upgrade Command**
* `upgrade-cluster` command that combines multiple steps into a single operation for easier upgrades
* Handles base OS updates, global settings backup and regeneration, and module upgrades automatically

## **✨ Features**

### **Administration and Tools**
* New `idea-admin.sh` tools
  * `backup-update-global-settings` command to update the global cluster settings in DDB
* New `vdc-controller` cli tools in `ideactl`
  * `merge-software-stacks` tool to merge in new software stacks after a cluster upgrade
  * `cleanup-orphaned-schedules` tool to cleanup the schedules table in DDB if sessions were improperly deleted
  * `terminate-sessions` tool to delete sessions by ID, username, or created date. Useful for deleting old sessions en mass
  * `update-base-stacks` tool to automatically update base software stacks with latest AMI versions
* Added optional `--reset` parameter to `ideactl reindex-software-stacks` and `ideactl reindex-user-sessions` on VDC Controller to first clear OpenSearch data before reindexing from DDB
* Updated `ami_update.py` and `ami_update_stacks.py` to use more maintainable dictionary definitions
* Admins can now force stop sessions in any state
* Added Session Health tab for DCV Session Info to Admin eVDI Session detail page
* Implement Github Actions for CI for build, push, and unit tests

### **Software Stack Management**
* Added the ability to clone existing software stacks
* Software Stack AMI, RAM, and Storage are now modifiable from the Web UI
* Added the ability for admin users to enable / disable / delete software stacks from the Cluster Manager Web UI
  * Added DeleteSoftwareStacks Admin API
  * Software Stack table is now multi-select and Admins can delete/disable multiple stacks at once
* Added the ability to set allowed instance types on a per software stack basis
* Admins can now assign projects to software stacks without needing to be a project member
* Admins can see all available projects in both the Create Software Stack and Capture from Session modals

### **User Experience and Interface**
* Added the ability to sort most tabular objects alphanumerically
* Added the ability for admins to launch sessions on behalf of users using instance types not defined in the allowed lists
* Refactored the Create Session form in Cluster Manager Web UI to only show users what they have permissions to launch
* Added Budget tab to the Submit Job Web UI page for users to review existing budget consumption if the submitting project has a budget attached
* Enhanced session validation for eVDI instances. Sessions in an ERROR state can now be recovered by Reboot or Admin Stop/Resume if the problem causing the ERROR has been resolved. Example: Bad `.bashrc` causing DCV session creation to fail
* Added the ability to export page tables in CSV format
* Updated default Job Notification email to include queue name and exit code
* Dark Mode is now default
* Various front end cosmetic improvements

### **Infrastructure and Security**
* VDC and Compute Hosts will now be deleted from AD upon termination
* Windows eVDI hosts now support extended metrics collection via Cloudwatch Agent
* Linux Compute nodes enable file logging via Cloudwatch Agent
* Added additional Cloudwatch log file consumption to Infrastructure & Application hosts
* Add `JobOwnerEmail` tag to eVDI and Compute nodes
  * Update your cost allocation tags to consume this new tag into CUR
* Reworked `filesystem_helper` for better file deletion management
* Implemented Github Actions CI for
  * Linting & Pre-Commit Checks
  * Unit Tests
  * Build Tests
  * Production Build & Push
* Implemented `pre-commit` checks & linting
  * Updated codebase to satisfy `ruff`, `j2lint`, `typos` and more

## **🔄 Changes**

### **Core Dependencies and Services**
| Component | Previous Version | New Version | Notes |
|-----------|------------------|-------------|-------|
| AWS CDK | `2.164.1` | `2.1016.1` | CDK stacks updated for deprecating actions |
| Python | `3.9.19` | `3.13.3` | Lambda runtimes now use Python 3.13 |
| Node | `18.20.4` | `22.14.0` | - |
| NVM | `0.40.1` | `0.40.2` | - |
| NPM | `10.5.2` | `10.9.2` | - |
| AWS EFA | `1.35.0` | `1.41.0` | - |
| OpenSearch | `2.15` | `2.19` | - |

### **DCV Updates**
| Component | Previous Version | New Version |
|-----------|------------------|-------------|
| Amazon DCV Server | `2024.0-17979` | `2024.0-19030` |
| Amazon DCV Agent | `2024.0.781-1` | `2024.0-817-1` |
| Amazon DCV Connection Gateway | `2023.0.710-1` | `2024.0-777-1` |
| Amazon DCV Broker | `2024.0.457-1` | `2024.0-504-1` |

### **System Configurations**
* Update NVIDIA Production GPU Drivers from `550.127.05` -> `570.124.06`
* New installs use Amazon Linux 2023 by default for Infrastructure hosts
* New installs use AWS Managed Active Directory by default
* OpenLDAP is no longer supported for new deployments
* Included `ec2:DescribeInstanceTypes` and S3 access to GPU Drivers on eVDI instance IAM role
* Cognito IDP Advanced security is now a paid feature, default to OFF to avoid Plus plan requirement

### **User Experience Improvements**
* Set `pro config set apt_news=false` on Ubuntu to prevent advertising
* Install Firefox on Ubuntu with apt instead of snap to prevent launch issues
* Made `terminate instance` button more obvious that it is a destructive action
* Documentation is now residing in the `main` branch

## **🐛 Bug Fixes**

### **User Interface**
* Fixed Chrome background handling closing the Service Worker causing premature auto-logouts
* Fixed projects page not loading when AWS Budget had been deleted or expired
* When capturing a Software Stack from a session, Project selection is now respected in the resulting stack
* Fixed GiB/GB conversion issue resulting in captured software stacks having incorrect Min RAM
* Fixed session filtering by calendar range
* Fixed CodeEditor theming

### **Infrastructure and Services**
* Fixed automated generation of data model. Updated to support Pydantic v2
* Fixed an issue with JSON output from qstat with multiple jobids, added `-E` flag to ensure successful output
* Fixed and refactored integration tests
* Fixed multi-node non-spot jobs not being launched in placement groups
* Fixed infrastructure deployments failing with non-default AWS profile using `idea-admin` utility
* Fixed GPU drivers installation failing on GPU instances in AWS Gov Cloud
* Added file size validation to open operations from Web UI File Browser
* Fixed dependency with HPC Jobs using placement groups, CloudFormation stack no longer fails to delete
* Fixed instance type EFA validation when queue has EFA enabled by default and submit script does not contain `efa_support` directive
* Updated to Tail File mechanism for resilience and robustness
* Linux and Windows eVDI nodes now provide Cloudwatch Agent metrics if Metrics & Logging is enabled
  * Extended eVDI metrics use the `CWAgent` namespace so they appear in the AWS Console on the Monitoring Tab alongside default metrics

## **⚠️ Known Issues**

### **Platform Limitations**
* Backend APIs return paginated results so table sorting is only per page
* CSV Exports are limited to 10000 results
* AMD GPUs (`g4ad`) only work on Windows and Rocky8/9
  * Incompatible OS's are filtered client side in the eVDI Create Session form

### **Browser-Specific Issues**
* Logins fail with multiple sso-enabled clusters on Safari
* Software Stacks captured from running sessions where AMI creation exceeds 8 minutes will fail to auto-enable and must be enabled manually

## [3.1.10] - 2024-10-29

### Notes
* This upgrade does require an update to the global settings. Please review [Global Settings Upgrade](https://docs.idea-hpc.com/first-time-users/cluster-operations/update-idea-cluster/update-idea-backend-resource#global-settings-backup-and-upgrade) before upgrading.
* You should update the IDEA CDK Bootstrap to fix [CDK Issue #31885](https://github.com/aws/aws-cdk/issues/31885) - This is a security fix and should be addressed on all CDK stacks regardless of IDEA
  * To update the IDEA CDK Bootstrap for existing deployments, use idea-admin:
    ```
    idea-admin.sh bootstrap --cluster-name <CLUSTER_NAME> --aws-region <CLUSTER_REGION> --aws-profile <AWS_PROFILE>
    ```

### Features
* Nice DCV Updated from `2023.1` to `2024.0` and renamed to Amazon DCV
* Ubuntu 24.04 Support for eVDI

### Changes
* Update AWS CDK from `2.154.1` to `2.164.1`
* Update Python Requirements
* Update NVIDIA Production GPU Drivers from `550.90.07` to `550.127.05`
* Update Installer and Software Stack AMIs - Raised minimum storage to 20 GB for Linux Stacks on new IDEA installs
* Update EFA Installer from `1.31.0` to `1.35.0`
* Default CDK Nag Scan to `false` in `idea-admin.sh`
* Added owner filtering, GovCloud support, and Ubuntu support to `ami_update_stacks.py` and `ami_update.py`
* Updated `browserslist` in Cluster Manager Web App
* Update IDEA `CdkBootstrapVersion` from `18` to `23`
* Update OpenSearch from `2.7` to `2.15`
* New installs use VPC Endpoints by default

### Bug Fixes
* Fixed Ubuntu eVDI nodes attempting Auto-Update - [Issue #177](https://github.com/cfs-energy/idea/issues/177)
* Fixed CDK version mismatch causing upgrade issues without Dev Mode
* Fixed Firefox menu entry on Ubuntu
* Fixed OpenSearch Private IP and Target Group logic
* Fixed initial eVDI session creation failing when instances requiring reboot take longer than expected
* Removed `OS_CENTOS7` reference from IDEA Admin Utility

### Known Caveats
* There is no FSx Lustre client for `Ubuntu 24.04` thus any FSx Lustre mounts will not function with `Ubuntu 24.04` eVDI until AWS releases a client
* OpenPBS is compiled from latest code in Github repository when using `Ubuntu 24.04`
* Amazon Windows DCV AMIs are using DCV server `2023.1.17701`

## [3.1.9] - 2024-08-29

### Notes
* DCV session files with no `transport` set have a 1 second timeout where `QUIC` is tried first and then a fallback to `TCP/WS` - In testing, this has found to leverage `QUIC` automatically intermittently when `QUIC` is enabled in IDEA. This will likely be resolved in the future with a customizable timeout to ensure `QUIC` connections complete in time.
* Docs need updating to reflect Ubuntu usage and removal of EOL OS options.

### Features
* Added GPU Driver support for `g6e` instance types

### Changes
* Added default to disable QUIC datagrams in DCV on eVDI nodes
* Update AWS CDK from `2.147.3` to `2.154.1`
* Update Python Requirements
* Update Node from `18.20.2` to `18.20.4`
* Update NVM from `0.39.7` to `0.40.1`

### Bug Fixes
* Fixed taskbar not appearing on Ubuntu eVDI nodes
* Removed non-existent `cfn_template` from devtool `release.update-version`
* Fixed snap on Ubuntu to use `/data/home` user directory
* Update firefox.desktop `NoDisplay=false` to show in Ubuntu application launcher
* Change input method for Ubuntu to `xim` to fix issues with `ibus` not allowing keyboard input in certain apps
* Only OpenSearch In-Use IPs should be added to the `dashboard` target group
* Adjusted pagination to `1000` records in `ListUsersInGroup` calls from Cluster Manager Web UI. This should be adjusted in the future to use proper pagination.

### Known Caveats


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
* When accessing multiple IDEA deployments at once in Safari, SSO logins hang for environments that were loaded after the first. Also exists in prior releases.
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
* In `idea-admin.sh` sso configuration, multiple scopes can be submitted separated by `,` this will be replaced with a space in Cognito

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
