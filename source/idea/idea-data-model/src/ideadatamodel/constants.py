#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

CONFIG_LEVEL_INFO = 0
CONFIG_LEVEL_WARNING = 1
CONFIG_LEVEL_CRITICAL = 2

DIRECTORYSERVICE_OPENLDAP = 'openldap'
DIRECTORYSERVICE_ACTIVE_DIRECTORY = 'activedirectory'
DIRECTORYSERVICE_AWS_MANAGED_ACTIVE_DIRECTORY = 'aws_managed_activedirectory'
DEFAULT_AUTH_PROVIDER = DIRECTORYSERVICE_OPENLDAP

IDENTITY_PROVIDER_COGNITO_IDP = 'cognito-idp'
IDENTITY_PROVIDER_KEYCLOAK = 'keycloak'

STORAGE_PROVIDER_EFS = 'efs'
STORAGE_PROVIDER_FSX_CACHE = 'fsx_cache'
STORAGE_PROVIDER_FSX_LUSTRE = 'fsx_lustre'
STORAGE_PROVIDER_FSX_NETAPP_ONTAP = 'fsx_netapp_ontap'
STORAGE_PROVIDER_FSX_OPENZFS = 'fsx_openzfs'
STORAGE_PROVIDER_FSX_WINDOWS_FILE_SERVER = 'fsx_windows_file_server'
DEFAULT_STORAGE_PROVIDER = STORAGE_PROVIDER_EFS
SUPPORTED_STORAGE_PROVIDERS = [
    STORAGE_PROVIDER_EFS,
    STORAGE_PROVIDER_FSX_CACHE,
    STORAGE_PROVIDER_FSX_LUSTRE,
    STORAGE_PROVIDER_FSX_NETAPP_ONTAP,
    STORAGE_PROVIDER_FSX_OPENZFS,
    STORAGE_PROVIDER_FSX_WINDOWS_FILE_SERVER
]

SCHEDULER_OPENPBS = 'openpbs'

QUEUE_MODE_FIFO = 'fifo'
QUEUE_MODE_FAIRSHARE = 'fairshare'
QUEUE_MODES = [QUEUE_MODE_FIFO, QUEUE_MODE_FAIRSHARE]
DEFAULT_QUEUE_MODE = QUEUE_MODE_FIFO

SCALING_MODE_SINGLE_JOB = 'single-job'
SCALING_MODE_MULTIPLE_JOBS = 'batch'
DEFAULT_SCALING_MODE = SCALING_MODE_SINGLE_JOB

ALLOWED_BASEOS = ['rhel7', 'centos7', 'amazonlinux2']

TOPIC_BROADCAST = 'idea.app.broadcast'
MESSAGE_RELOAD = 'app.reload'

TOPIC_EC2_INSTANCE_MONITOR_EVENTS = 'idea.aws.ec2.instance-monitor'
EC2_INSTANCE_MONITOR_EVENT_CACHE_REFRESH = 'instance-cache-refreshed'
EC2_INSTANCE_MONITOR_EVENT_INSTANCE_STATE_RUNNING = 'instance-state.running'

TOPIC_NODE_MONITOR_EVENTS = 'idea.scheduler.node-monitor'
TOPIC_JOB_MONITOR_EVENTS = 'idea.scheduler.job-monitor'

LOGGER_TEMPLATE_APP = 'app'
LOGGER_TEMPLATE_ROOT = 'root'
DEFAULT_LOGGER_NAME = 'idea'

AWS_TAG_CFN_LOGICAL_ID = 'aws:cloudformation:logical-id'
AWS_TAG_CFN_STACK_NAME = 'aws:cloudformation:stack-name'
AWS_TAG_CFN_STACK_ID = 'aws:cloudformation:stack-id'
AWS_TAG_EC2SPOT_FLEET_REQUEST_ID = 'aws:ec2spot:fleet-request-id'
AWS_TAG_AUTOSCALING_GROUP_NAME = 'aws:autoscaling:groupName'

IDEA_TAG_NODE_TYPE = 'idea:NodeType'

IDEA_TAG_CLUSTER_NAME = 'idea:ClusterName'
IDEA_TAG_MODULE_ID = 'idea:ModuleId'
IDEA_TAG_MODULE_NAME = 'idea:ModuleName'
IDEA_TAG_MODULE_VERSION = 'idea:ModuleVersion'
IDEA_TAG_PROJECT = 'idea:Project'
IDEA_TAG_AMI_BUILDER = 'idea:AmiBuilder'

IDEA_TAG_NAME = 'Name'

IDEA_TAG_JOB_ID = 'idea:JobId'
IDEA_TAG_JOB_GROUP = 'idea:JobGroup'
IDEA_TAG_JOB_NAME = 'idea:JobName'
IDEA_TAG_JOB_OWNER = 'idea:JobOwner'
IDEA_TAG_JOB_QUEUE = 'idea:JobQueue'
IDEA_TAG_KEEP_FOREVER = 'idea:KeepForever'
IDEA_TAG_TERMINATE_WHEN_IDLE = 'idea:TerminateWhenIdle'
IDEA_TAG_QUEUE_TYPE = 'idea:QueueType'
IDEA_TAG_SCALING_MODE = 'idea:ScalingMode'
IDEA_TAG_CAPACITY_TYPE = 'idea:CapacityType'
IDEA_TAG_FSX = 'idea:FSx'
IDEA_TAG_COMPUTE_STACK = 'idea:StackId'
IDEA_TAG_CREATED_FROM = 'idea:CreatedFrom'
IDEA_TAG_CREATED_ON = 'idea:CreatedOn'
IDEA_TAG_BACKUP_PLAN = 'idea:BackupPlan'
IDEA_TAG_STACK_TYPE = 'idea:StackType'
IDEA_TAG_IDEA_SESSION_ID = 'idea:IDEASessionUUID'
IDEA_TAG_DCV_SESSION_ID = 'idea:DCVSessionUUID'

NODE_TYPE_COMPUTE = 'compute-node'
NODE_TYPE_DCV_HOST = 'virtual-desktop-dcv-host'
NODE_TYPE_APP = 'app'
NODE_TYPE_INFRA = 'infra'
NODE_TYPE_AMI_BUILDER = 'ami-builder'
NODE_TYPE_UNKNOWN = 'unknown'

STACK_TYPE_BOOTSTRAP = 'bootstrap'
STACK_TYPE_CLUSTER = 'cluster'
STACK_TYPE_ANALYTICS = 'analytics'
STACK_TYPE_APP = 'app'
STACK_TYPE_ALB = 'alb'
STACK_TYPE_JOB = 'job'
STACK_TYPE_DEBUG = 'debug'

SPOT_PRICE_AUTO = 'auto'

EC2_SERVICE_QUOTA_ONDEMAND = 1
EC2_SERVICE_QUOTA_SPOT = 2
EC2_SERVICE_QUOTA_DEDICATED = 3
EC2_SERVICE_CPU_OPTIONS_UNSUPPORTED_FAMILY = ('t2', 'hpc6a', 'a1')

JOB_PARAM_NODES = 'nodes'
JOB_PARAM_CPUS = 'cpus'
JOB_PARAM_MEMORY = 'memory'
JOB_PARAM_GPUS = 'gpus'
JOB_PARAM_MPIPROCS = 'mpiprocs'
JOB_PARAM_BASE_OS = 'base_os'
JOB_PARAM_INSTANCE_AMI = 'instance_ami'
JOB_PARAM_INSTANCE_TYPES = 'instance_types'
JOB_PARAM_FORCE_RESERVED_INSTANCES = 'force_reserved_instances'
JOB_PARAM_SPOT = 'spot'
JOB_PARAM_SPOT_PRICE = 'spot_price'
JOB_PARAM_SPOT_ALLOCATION_COUNT = 'spot_allocation_count'
JOB_PARAM_SPOT_ALLOCATION_STRATEGY = 'spot_allocation_strategy'
JOB_PARAM_SUBNET_IDS = 'subnet_ids'
JOB_PARAM_SECURITY_GROUPS = 'security_groups'
JOB_PARAM_INSTANCE_PROFILE = 'instance_profile'
JOB_PARAM_KEEP_EBS_VOLUMES = 'keep_ebs_volumes'
JOB_PARAM_ENABLE_SCRATCH = 'enable_scratch'
JOB_PARAM_ROOT_STORAGE_SIZE = 'root_storage_size'
JOB_PARAM_SCRATCH_STORAGE_SIZE = 'scratch_storage_size'
JOB_PARAM_SCRATCH_IOPS = 'scratch_storage_iops'
JOB_PARAM_FSX_LUSTRE = 'fsx_lustre'
JOB_PARAM_FSX_LUSTRE_S3_BACKEND = 'fsx_lustre_s3_backend'
JOB_PARAM_FSX_LUSTRE_EXISTING_FSX = 'fsx_lustre_existing_fsx'
JOB_PARAM_FSX_LUSTRE_IMPORT_PATH = 'fsx_lustre_import_path'
JOB_PARAM_FSX_LUSTRE_EXPORT_PATH = 'fsx_lustre_export_path'
JOB_PARAM_FSX_LUSTRE_DEPLOYMENT_TYPE = 'fsx_lustre_deployment_type'
JOB_PARAM_FSX_LUSTRE_PER_UNIT_THROUGHPUT = 'fsx_lustre_per_unit_throughput'
JOB_PARAM_FSX_LUSTRE_SIZE = 'fsx_lustre_size'
JOB_PARAM_ENABLE_INSTANCE_STORE = 'enable_instance_store'
JOB_PARAM_ENABLE_EFA_SUPPORT = 'enable_efa_support'
JOB_PARAM_ENABLE_HT_SUPPORT = 'enable_ht_support'
JOB_PARAM_ENABLE_PLACEMENT_GROUP = 'enable_placement_group'
JOB_PARAM_ENALE_SYSTEM_METRICS = 'enable_system_metrics'
JOB_PARAM_ENABLE_ANONYMOUS_METRICS = 'enable_anonymous_metrics'
JOB_PARAM_LICENSES = 'licenses'
JOB_PARAM_WALLTIME = 'walltime'
JOB_PARAM_COMPUTE_STACK = 'compute_stack'
JOB_PARAM_STACK_ID = 'stack_id'
JOB_PARAM_JOB_GROUP = 'job_group'
JOB_PARAM_JOB_STARTED_EMAIL_TEMPLATE = 'job_started_email_template'
JOB_PARAM_JOB_COMPLETED_EMAIL_TEMPLATE = 'job_completed_email_template'
JOB_PARAM_CUSTOM_PARAMS = 'custom_params'
JOB_OPTION_TERMINATE_WHEN_IDLE = 'terminate_when_idle'
JOB_OPTION_KEEP_FOREVER = 'keep_forever'
JOB_OPTION_TAGS = 'tags'

MAX_SECURITY_GROUPS = 4

DEFAULT_NODES = 1
DEFAULT_CPUS = 1
DEFAULT_FORCE_RESERVED_INSTANCES = False
DEFAULT_KEEP_EBS_VOLUMES = False
DEFAULT_ROOT_STORAGE_SIZE = 10
DEFAULT_SCRATCH_STORAGE_SIZE = 0
DEFAULT_SCRATCH_IOPS = 0

DEFAULT_FSX_LUSTRE_PER_UNIT_THROUGHPUT = 200
DEFAULT_FSX_LUSTRE_SIZE_GB = 1200

FSX_LUSTRE_DEPLOYMENT_TYPE_PERSISTENT_1 = 'persistent_1'
FSX_LUSTRE_DEPLOYMENT_TYPE_PERSISTENT_2 = 'persistent_2'
FSX_LUSTRE_DEPLOYMENT_TYPE_SCRATCH_1 = 'scratch_1'
FSX_LUSTRE_DEPLOYMENT_TYPE_SCRATCH_2 = 'scratch_2'
DEFAULT_FSX_LUSTRE_DEPLOYMENT_TYPE = FSX_LUSTRE_DEPLOYMENT_TYPE_SCRATCH_2
FSX_LUSTRE_PER_UNIT_THROUGHPUT_TYPES = (FSX_LUSTRE_DEPLOYMENT_TYPE_PERSISTENT_1, FSX_LUSTRE_DEPLOYMENT_TYPE_PERSISTENT_2)

DEFAULT_ENABLE_EFA_SUPPORT = False
DEFAULT_ENABLE_HT_SUPPORT = False
DEFAULT_ENABLE_PLACEMENT_GROUP = False
DEFAULT_SPOT_ALLOCATION_STRATEGY = 'capacity-optimized'
DEFAULT_ENABLE_SYSTEM_METRICS = False
DEFAULT_ENABLE_SPOT = False
DEFAULT_ENABLE_SCRATCH = False
DEFAULT_TERMINATE_WHEN_IDLE = 0
DEFAULT_KEEP_FOREVER = False

EC2_PLACEMENT_GROUP_STRATEGY_CLUSTER = 'cluster'

SECONDS_IN_MINUTE = 60
SECONDS_IN_HOUR = 60 * SECONDS_IN_MINUTE

SELECT_CHOICE_OTHER = 'other'

# Supported OS
OS_AMAZONLINUX2 = 'amazonlinux2'
OS_RHEL7 = 'rhel7'
OS_CENTOS7 = 'centos7'
OS_WINDOWS = 'windows'
SUPPORTED_OS = (OS_AMAZONLINUX2, OS_RHEL7, OS_CENTOS7, OS_WINDOWS)
SUPPORTED_LINUX_OS = (OS_AMAZONLINUX2, OS_RHEL7, OS_CENTOS7)

# Platforms
PLATFORM_LINUX = 'linux'
PLATFORM_WINDOWS = 'windows'

CLICK_SETTINGS = dict(
    help_option_names=['-h', '--help'],
    max_content_width=1200)

AWS_SOLUTION_ID = 'SO0072'
DEFAULT_ENCODING = 'utf-8'
DEFAULT_LOCALE = 'en_US'
DEFAULT_TIMEZONE = 'America/Los_Angeles'

# modules
MODULE_BOOTSTRAP = 'bootstrap'
MODULE_GLOBAL_SETTINGS = 'global-settings'
MODULE_CLUSTER = 'cluster'
MODULE_IDENTITY_PROVIDER = 'identity-provider'
MODULE_DIRECTORYSERVICE = 'directoryservice'
MODULE_SHARED_STORAGE = 'shared-storage'
MODULE_ANALYTICS = 'analytics'
MODULE_SCHEDULER = 'scheduler'
MODULE_CLUSTER_MANAGER = 'cluster-manager'
MODULE_VIRTUAL_DESKTOP_CONTROLLER = 'virtual-desktop-controller'
MODULE_BASTION_HOST = 'bastion-host'
MODULE_METRICS = 'metrics'
ALL_MODULES = [
    MODULE_BOOTSTRAP,
    MODULE_GLOBAL_SETTINGS,
    MODULE_CLUSTER,
    MODULE_IDENTITY_PROVIDER,
    MODULE_DIRECTORYSERVICE,
    MODULE_SHARED_STORAGE,
    MODULE_ANALYTICS,
    MODULE_SCHEDULER,
    MODULE_CLUSTER_MANAGER,
    MODULE_VIRTUAL_DESKTOP_CONTROLLER,
    MODULE_BASTION_HOST,
    MODULE_METRICS
]

# module types
MODULE_TYPE_APP = 'app'
MODULE_TYPE_CONFIG = 'config'
MODULE_TYPE_STACK = 'stack'

# group types
GROUP_TYPE_USER = 'user'
GROUP_TYPE_PROJECT = 'project'
GROUP_TYPE_MODULE = 'module'
GROUP_TYPE_CLUSTER = 'cluster'
ALL_GROUP_TYPES = [
    GROUP_TYPE_USER,
    GROUP_TYPE_PROJECT,
    GROUP_TYPE_MODULE,
    GROUP_TYPE_CLUSTER
]

# project defaults
DEFAULT_PROJECT = 'default'

DEFAULT_COPYRIGHT_TEXT = 'Copyright {year} Amazon.com, Inc. or its affiliates. All Rights Reserved.'

# metrics
METRICS_PROVIDER_CLOUDWATCH = 'cloudwatch'
METRICS_PROVIDER_PROMETHEUS = 'prometheus'
METRICS_PROVIDER_AMAZON_MANAGED_PROMETHEUS = 'amazon_managed_prometheus'

# services
SERVICE_ID_LEADER_ELECTION = 'leader-election'
SERVICE_ID_DISTRIBUTED_LOCK = 'distributed-lock'
SERVICE_ID_METRICS = 'metrics-service'
SERVICE_ID_ANALYTICS = 'analytics-service'

# idea service account
IDEA_SERVICE_ACCOUNT = 'ideaserviceaccount'

# open api spec version
OPEN_API_SPEC_VERSION = '3.0.1'

#
# Caveat definitions
#
CAVEATS = dict()
#
# SSM service discovery namespace is not available
#
CAVEATS['SSM_DISCOVERY_RESTRICTED_REGION_LIST'] = [
    'af-south-1',
    'ap-northeast-3',
    'eu-south-1',
    'me-central-1',
    'me-south-1',
    'us-gov-east-1',
    'us-gov-west-1'
    ]
CAVEATS['SSM_DISCOVERY_FALLBACK_REGION'] = 'us-east-1'


#
# Kinesis streams does not support StreamData in CloudFormation
#
CAVEATS['KINESIS_STREAMS_CLOUDFORMATION_UNSUPPORTED_STREAMMODEDETAILS_REGION_LIST'] = [
    'us-gov-east-1',
    'us-gov-west-1'
]

#
# Route53 cross-zone Alias records are not permitted
# (creates CNAME records instead)
#
CAVEATS['ROUTE53_CROSS_ZONE_ALIAS_RESTRICTED_REGION_LIST'] = [
    'us-gov-east-1',
    'us-gov-west-1'
]

#
# FIPS endpoint is default
#
CAVEATS['COGNITO_REQUIRE_FIPS_ENDPOINT_REGION_LIST'] = [
    'us-gov-east-1',
    'us-gov-west-1'
]

#
# Cognito Advanced Security is not available
#
CAVEATS['COGNITO_ADVANCED_SECURITY_UNAVAIL_REGION_LIST'] = [
    'us-gov-east-1',
    'us-gov-west-1'
]

#
# No SQS FIFO queues
#
CAVEATS['SQS_NO_FIFO_SUPPORT_REGION_LIST'] = [
    'us-gov-east-1',
    'us-gov-west-1'
]

#
# No SNS FIFO queues
#
CAVEATS['SNS_NO_FIFO_SUPPORT_REGION_LIST'] = [
    'us-gov-east-1',
    'us-gov-west-1'
]

# module set
DEFAULT_MODULE_SET = 'default'

# api invocation source
API_INVOCATION_SOURCE_UNIX_SOCKET = 'unix-socket'
API_INVOCATION_SOURCE_HTTP = 'http'

# SSO
SSO_IDP_PROVIDER_OIDC = 'OIDC'
SSO_IDP_PROVIDER_SAML = 'SAML'
