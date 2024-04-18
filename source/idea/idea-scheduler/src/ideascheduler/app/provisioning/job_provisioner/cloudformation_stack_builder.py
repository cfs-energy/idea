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

import ideascheduler
from ideadatamodel import exceptions, errorcodes, constants, SocaJob, SocaAnonymousMetrics, GetProjectRequest
from ideasdk.utils import Utils
from ideasdk.context import BootstrapContext
from ideasdk.bootstrap import BootstrapUserDataBuilder, BootstrapPackageBuilder, BootstrapUtils

from troposphere import Base64, GetAtt, Sub
from troposphere import Ref, Template
from troposphere import Tags
from troposphere.autoscaling import AutoScalingGroup, \
    LaunchTemplateSpecification, \
    Tags as AsgTags, \
    LaunchTemplateOverrides, \
    MixedInstancesPolicy, \
    InstancesDistribution
from troposphere.autoscaling import LaunchTemplate as asg_LaunchTemplate
from troposphere.ec2 import PlacementGroup, \
    BlockDeviceMapping, \
    LaunchTemplate, \
    LaunchTemplateData, \
    EBSBlockDevice, \
    IamInstanceProfile, \
    InstanceMarketOptions, \
    NetworkInterfaces, \
    SpotOptions, \
    CpuOptions, \
    LaunchTemplateBlockDeviceMapping

from troposphere.fsx import FileSystem, LustreConfiguration
import troposphere.ec2 as ec2
import os
import logging
from typing import Optional


class CloudFormationStackBuilder:
    """
    Build and Provision the CloudFormation Stack for IDEA Compute Nodes
    """

    def __init__(self, context: ideascheduler.AppContext, job: SocaJob, target_capacity_override: Optional[int] = None):
        self.context = context
        self.logger = context.logger()
        self.job = job
        self.job.params.compute_stack = self.job.get_compute_stack()
        self.target_capacity_override = target_capacity_override

        template = Template()
        template.set_version('2010-09-09')
        template.set_description(f'IDEA Compute Node Stack (Version: {ideascheduler.__version__})')
        self.template = template

    def get_common_tags(self):

        terminate_when_idle = str(self.job.provisioning_options.terminate_when_idle)
        keep_forever = str(self.job.provisioning_options.keep_forever).lower()
        scaling_mode = str(self.job.scaling_mode)
        capacity_type = str(self.job.capacity_type())

        tags = {
            constants.IDEA_TAG_NAME: self.job.get_compute_stack(),
            constants.IDEA_TAG_JOB_ID: self.job.job_id,
            constants.IDEA_TAG_JOB_QUEUE: self.job.queue,
            constants.IDEA_TAG_QUEUE_TYPE: self.job.queue_type,
            constants.IDEA_TAG_JOB_OWNER: self.job.owner,
            constants.IDEA_TAG_TERMINATE_WHEN_IDLE: terminate_when_idle,
            constants.IDEA_TAG_KEEP_FOREVER: keep_forever,
            constants.IDEA_TAG_SCALING_MODE: scaling_mode,
            constants.IDEA_TAG_JOB_GROUP: self.job.get_job_group(),
            constants.IDEA_TAG_CAPACITY_TYPE: capacity_type,
            constants.IDEA_TAG_CLUSTER_NAME: self.job.cluster_name,
            constants.IDEA_TAG_STACK_TYPE: constants.STACK_TYPE_JOB,
            constants.IDEA_TAG_MODULE_ID: self.context.module_id(),
            constants.IDEA_TAG_COMPUTE_STACK: self.job.get_compute_stack()
        }

        # optional tags
        if self.job.name:
            tags[constants.IDEA_TAG_JOB_NAME] = self.job.name

        if self.job.project:
            tags[constants.IDEA_TAG_PROJECT] = self.job.project

            # assign custom project if available
            get_project_result = self.context.projects_client.get_project(GetProjectRequest(
                project_name=self.job.project
            ))
            project = get_project_result.project
            if project is not None and Utils.is_not_empty(project.tags):
                for tag in project.tags:
                    tags[tag.key] = tag.value

        custom_tags = self.context.config().get_list('global-settings.custom_tags', [])
        custom_tags_dict = Utils.convert_custom_tags_to_key_value_pairs(custom_tags)

        return {**custom_tags_dict, **tags}

    def build_user_data(self):
        base_os = self.job.params.base_os

        module_id = self.context.module_id()
        bootstrap_context = BootstrapContext(
            config=self.context.config(),
            module_name=self.context.module_name(),
            module_id=module_id,
            module_set=self.context.module_set(),
            base_os=base_os,
            instance_type=self.job.params.instance_types[0]
        )
        bootstrap_context.vars.job = self.job
        bootstrap_context.vars.project = self.job.project
        bootstrap_context.vars.queue_profile = self.job.queue_type

        job_directory = self.context.get_job_dir(self.job)

        bootstrap_context.vars.job_directory = job_directory

        cluster_s3_bucket = self.context.config().get_string('cluster.cluster_s3_bucket', required=True)

        bootstrap_tmp_dir = os.path.join(job_directory, 'bootstrap')
        if self.job.is_shared_capacity():
            bootstrap_package_suffix = f'{self.job.get_job_group()}-{self.job.job_uid}'
            metrics_namespace = f'{self.context.cluster_name()}/{self.context.module_id()}/job-{self.job.get_job_group()}'
        else:
            bootstrap_package_suffix = f'{self.job.job_id}-{self.job.job_uid}'
            metrics_namespace = f'{self.context.cluster_name()}/{self.context.module_id()}/job-{self.job.job_id}'

        BootstrapUtils.check_and_attach_cloudwatch_logging_and_metrics(
            bootstrap_context=bootstrap_context,
            metrics_namespace=metrics_namespace,
            node_type=constants.NODE_TYPE_COMPUTE,
            enable_logging=False,
            log_files=[],
            enable_metrics=Utils.is_true(self.job.params.enable_system_metrics, default=False)
        )

        bootstrap_source_dir = self.context.get_bootstrap_dir()
        bootstrap_package_archive_file = BootstrapPackageBuilder(
            bootstrap_context=bootstrap_context,
            source_directory=bootstrap_source_dir,
            target_package_basename=f'compute-node-{bootstrap_package_suffix}',
            components=[
                'compute-node'
            ],
            tmp_dir=bootstrap_tmp_dir,
            force_build=True,
            logger=self.logger
        ).build()
        self.logger.info(f'{self.job.log_tag} built bootstrap package: {bootstrap_package_archive_file}')

        bootstrap_package_key = f'idea/{self.context.module_id()}/bootstrap/{os.path.basename(bootstrap_package_archive_file)}'
        bootstrap_package_uri = f's3://{cluster_s3_bucket}/{bootstrap_package_key}'
        self.logger.info(f'{self.job.log_tag} uploading bootstrap package: {bootstrap_package_uri}')
        self.context.aws().s3().upload_file(
            Bucket=cluster_s3_bucket,
            Filename=bootstrap_package_archive_file,
            Key=bootstrap_package_key
        )

        https_proxy = self.context.config().get_string('cluster.network.https_proxy', required=False, default='')
        no_proxy = self.context.config().get_string('cluster.network.no_proxy', required=False, default='')
        proxy_config = {}
        if Utils.is_not_empty(https_proxy):
            proxy_config = {
                    'http_proxy': https_proxy,
                    'https_proxy': https_proxy,
                    'no_proxy': no_proxy
                    }

        return BootstrapUserDataBuilder(
            aws_region=self.context.aws().aws_region(),
            bootstrap_package_uri=bootstrap_package_uri,
            install_commands=[
                '/bin/bash compute-node/setup.sh'
            ],
            proxy_config=proxy_config,
            base_os=base_os
        ).build()

    def build_spot_fleet(self, launch_template: LaunchTemplate) -> ec2.SpotFleet:

        spot_fleet_request = ec2.SpotFleetRequestConfigData()
        spot_fleet_request.AllocationStrategy = self.job.params.spot_allocation_strategy.spot_value()
        spot_fleet_request.ExcessCapacityTerminationPolicy = self.context.config().get_string('scheduler.job_provisioning.spot_fleet_request.excess_capacity_termination_policy', default='noTermination')
        spot_fleet_iam_role_arn = self.context.config().get_string('scheduler.spot_fleet_request_iam_role_arn', required=True)
        spot_fleet_request.IamFleetRole = spot_fleet_iam_role_arn
        spot_fleet_request.InstanceInterruptionBehavior = self.context.config().get_string('scheduler.job_provisioning.spot_fleet_request.instance_interruption_behavior', default='terminate')
        if self.job.params.spot_price and float(self.job.params.spot_price.amount) > 0:
            spot_fleet_request.SpotPrice = str(self.job.params.spot_price.amount)
        else:
            spot_fleet_request.SpotPrice = Ref('AWS::NoValue')

        spot_maintenance_strategies = self.context.config().get_string('scheduler.job_provisioning.spot_fleet_request.spot_maintenance_strategies')
        if Utils.is_not_empty(spot_maintenance_strategies):
            spot_fleet_request.SpotMaintenanceStrategies = ec2.SpotMaintenanceStrategies(
               CapacityRebalance=ec2.SpotCapacityRebalance(ReplacementStrategy='launch')
            )

        if self.target_capacity_override is None:
            target_capacity = self.job.spot_capacity()
        else:
            target_capacity = self.target_capacity_override

        spot_fleet_request.TargetCapacity = target_capacity
        spot_fleet_request.Type = self.context.config().get_string('scheduler.job_provisioning.spot_fleet_request.request_type', default='maintain')
        spot_fleet_ltc = ec2.LaunchTemplateConfigs()

        spot_fleet_lts = ec2.FleetLaunchTemplateSpecification(
            LaunchTemplateId=Ref(launch_template),
            Version=GetAtt(launch_template, 'LatestVersionNumber')
        )
        spot_fleet_ltc.LaunchTemplateSpecification = spot_fleet_lts

        spot_fleet_ltc.Overrides = []

        if self.job.params.enable_efa_support:
            for instance_type in self.job.provisioning_options.instance_types:
                spot_fleet_ltc.Overrides.append(ec2.LaunchTemplateOverrides(
                    InstanceType=instance_type.name,
                    WeightedCapacity=instance_type.weighted_capacity
                ))
        else:
            for instance_type in self.job.provisioning_options.instance_types:
                for subnet_id in self.job.params.subnet_ids:
                    spot_fleet_ltc.Overrides.append(ec2.LaunchTemplateOverrides(
                        InstanceType=instance_type.name,
                        SubnetId=subnet_id,
                        WeightedCapacity=instance_type.weighted_capacity
                    ))

        spot_fleet_request.LaunchTemplateConfigs = [spot_fleet_ltc]

        tags = {constants.IDEA_TAG_NODE_TYPE: constants.NODE_TYPE_COMPUTE}

        spot_instance_request_tags = ec2.TagSpecifications(
            ResourceType='spot-instances-request',
            Tags=Tags(**self.get_common_tags(), **tags)
        )
        launch_template.LaunchTemplateData.TagSpecifications.append(spot_instance_request_tags)

        spot_fleet = ec2.SpotFleet('SpotFleet')
        spot_fleet.SpotFleetRequestConfigData = spot_fleet_request

        return spot_fleet

    def build_placement_group(self) -> Optional[PlacementGroup]:
        if not self.job.params.enable_placement_group:
            return None
        pg = PlacementGroup('ComputeNodePlacementGroup')
        pg.Strategy = self.context.config().get_string('scheduler.job_provisioning.placement_group.strategy', default=constants.EC2_PLACEMENT_GROUP_STRATEGY_CLUSTER)
        return pg

    def build_auto_scaling_group(self, launch_template: LaunchTemplate) -> AutoScalingGroup:
        asg = AutoScalingGroup('AutoScalingComputeGroup')
        asg.DependsOn = 'NodeLaunchTemplate'

        asg_lt = asg_LaunchTemplate()
        asg_lts = LaunchTemplateSpecification(
            LaunchTemplateId=Ref(launch_template),
            Version=GetAtt(launch_template, 'LatestVersionNumber')
        )
        asg_lt.LaunchTemplateSpecification = asg_lts

        # build placement group
        placement_group = self.build_placement_group()
        if placement_group is not None:
            self.template.add_resource(placement_group)

        if self.target_capacity_override is None:
            target_capacity = self.job.ondemand_capacity() + self.job.spot_capacity()
        else:
            target_capacity = self.target_capacity_override

        asg.MinSize = target_capacity
        asg.MaxSize = target_capacity
        asg.DesiredCapacity = str(target_capacity)
        asg.VPCZoneIdentifier = self.job.params.subnet_ids
        asg.CapacityRebalance = False

        if placement_group is not None:
            asg.PlacementGroup = Ref(placement_group)

        tags = {constants.IDEA_TAG_NODE_TYPE: constants.NODE_TYPE_COMPUTE}
        asg.Tags = AsgTags(**self.get_common_tags(), **tags)

        mip = MixedInstancesPolicy()
        mip.LaunchTemplate = asg_lt

        asg.MixedInstancesPolicy = mip

        asg_lt.Overrides = []
        for instance_type in self.job.provisioning_options.instance_types:
            asg_lt.Overrides.append(LaunchTemplateOverrides(
                InstanceType=instance_type.name,
                WeightedCapacity=str(instance_type.weighted_capacity)
            ))

        if self.job.is_mixed_capacity():
            distribution = InstancesDistribution()
            distribution.OnDemandAllocationStrategy = self.context.config().get_string('scheduler.job_provisioning.mixed_instances_policy.on_demand_allocation_strategy', default='prioritized')
            distribution.OnDemandBaseCapacity = self.job.ondemand_capacity()
            distribution.OnDemandPercentageAboveBaseCapacity = '0'  # force the other instances to be SPOT
            if self.job.params.spot_price and float(self.job.params.spot_price.amount) > 0:
                spot_max_price = str(self.job.params.spot_price.amount)
            else:
                spot_max_price = Ref('AWS::NoValue')
            distribution.SpotMaxPrice = spot_max_price
            distribution.SpotAllocationStrategy = self.job.params.spot_allocation_strategy.asg_value()
            mip.InstancesDistribution = distribution

        return asg

    def build_launch_template(self) -> LaunchTemplate:
        launch_template = LaunchTemplate('NodeLaunchTemplate')
        launch_template.LaunchTemplateName = self.job.get_compute_stack()

        launch_template_data = LaunchTemplateData('NodeLaunchTemplateData')
        launch_template.LaunchTemplateData = launch_template_data

        instance_type_option = self.job.default_instance_type_option

        launch_template_data.InstanceType = instance_type_option.name
        launch_template_data.EbsOptimized = instance_type_option.ebs_optimized

        if instance_type_option.cpu_options_supported:
            if self.job.is_hyper_threading_disabled and not self.job.is_multiple_instance_types:
                launch_template_data.CpuOptions = CpuOptions(
                    CoreCount=instance_type_option.default_core_count,
                    ThreadsPerCore=instance_type_option.threads_per_core
                )

        launch_template_data.IamInstanceProfile = IamInstanceProfile(
            Arn=self.job.params.instance_profile
        )

        launch_template_data.KeyName = self.context.config().get_string('cluster.network.ssh_key_pair', required=True)
        launch_template_data.ImageId = self.job.params.instance_ami

        if self.job.is_spot_capacity():
            if self.job.params.spot_price and float(self.job.params.spot_price.amount) > 0:
                spot_max_price = str(self.job.params.spot_price.amount)
            else:
                spot_max_price = Ref('AWS::NoValue')
            launch_template_data.InstanceMarketOptions = InstanceMarketOptions(
                MarketType='spot',
                SpotOptions=SpotOptions(
                    MaxPrice=spot_max_price
                )
            )

        if self.job.params.enable_efa_support:
            _max_efa_interfaces: int = Utils.get_as_int(
                self.context.aws_util().get_instance_efa_max_interfaces_supported(instance_type=launch_template_data.InstanceType),
                default=0
            )
            self.logger.info(f"EFA requested - determined Max EFA interfaces for instance {launch_template_data.InstanceType}: {_max_efa_interfaces}")

            launch_template_data.NetworkInterfaces = []
            _max_efa_interfaces: int = 1 #regressed in 3.1.7
            #_nci: int = 0  # NetworkCardIndex
            for _i in range(0, _max_efa_interfaces):
                self.logger.info(f"Adding EFA interface #{_i} - NetworkCardIndex: 0")
                launch_template_data.NetworkInterfaces.append(
                    NetworkInterfaces(
                        InterfaceType='efa',
                        DeleteOnTermination=True,
                        #DeviceIndex=1 if (_i > 0) else 0,
                        DeviceIndex=0,
                        NetworkCardIndex=_i,
                        Groups=self.job.params.security_groups
                    )
                )
                #_nci += 1
        else:
            launch_template_data.SecurityGroupIds = self.job.params.security_groups

        user_data = self.build_user_data()
        launch_template_data.UserData = Base64(Sub(user_data))

        kms_key_id = self.context.config().get_string('cluster.ebs.kms_key_id', required=False, default=None)
        if kms_key_id is None:
            kms_key_id = 'alias/aws/ebs'

        launch_template_data.BlockDeviceMappings = [
            LaunchTemplateBlockDeviceMapping(
                DeviceName=Utils.get_ec2_block_device_name(base_os=self.job.params.base_os),
                Ebs=EBSBlockDevice(
                    VolumeSize=self.job.params.root_storage_size.int_val(),
                    VolumeType=constants.DEFAULT_VOLUME_TYPE_COMPUTE,
                    DeleteOnTermination=not self.job.params.keep_ebs_volumes,
                    Encrypted=constants.DEFAULT_VOLUME_ENCRYPTION_COMPUTE,
                    KmsKeyId=kms_key_id
                )
            )
        ]

        if Utils.get_as_bool(self.job.params.enable_scratch, False) and Utils.are_equal('ebs', self.job.params.scratch_provider):
            iops = Utils.get_as_int(self.job.params.scratch_storage_iops, 0)
            launch_template_data.BlockDeviceMappings.append(
                BlockDeviceMapping(
                    DeviceName='/dev/xvdbx',
                    Ebs=EBSBlockDevice(
                        VolumeSize=self.job.params.scratch_storage_size.int_val(),
                        VolumeType=Utils.get_as_string(constants.DEFAULT_VOLUME_TYPE_SCRATCH, default='io1') if iops > 0 else Utils.get_as_string(constants.DEFAULT_VOLUME_TYPE_COMPUTE, default='gp3'),
                        Iops=iops if iops > 0 else Ref('AWS::NoValue'),
                        DeleteOnTermination=not self.job.params.keep_ebs_volumes,
                        Encrypted=Utils.get_as_bool(constants.DEFAULT_VOLUME_ENCRYPTION_COMPUTE, default=True),
                        KmsKeyId=kms_key_id
                    )
                )
            )

        tags = {
            constants.IDEA_TAG_NODE_TYPE: constants.NODE_TYPE_COMPUTE
        }
        launch_template_data.TagSpecifications = [
            ec2.TagSpecifications(
                ResourceType='instance',
                Tags=Tags(**self.get_common_tags(), **tags)
            ),
            ec2.TagSpecifications(
                ResourceType='volume',
                Tags=Tags(**self.get_common_tags(), **tags)
            )
        ]

        # Require IMDSv2
        launch_template_data.MetadataOptions = ec2.MetadataOptions(
            HttpEndpoint='enabled',
            HttpTokens='required',
            HttpPutResponseHopLimit=2
        )

        return launch_template

    def build_fsx_lustre(self):
        fsx_lustre = FileSystem('FSxForLustre')
        fsx_lustre.FileSystemType = 'LUSTRE'
        fsx_lustre.FileSystemTypeVersion = self.context.config().get_string('cluster.aws.fsx_lustre_version', required=False, default='2.15')
        fsx_lustre.StorageCapacity = self.job.params.fsx_lustre.size.int_val()
        fsx_lustre.SecurityGroupIds = self.job.params.security_groups
        fsx_lustre.SubnetIds = self.job.params.subnet_ids
        fsx_lustre_configuration = LustreConfiguration()
        deployment_type = self.job.params.fsx_lustre.deployment_type.upper()
        fsx_lustre_configuration.DeploymentType = deployment_type
        if deployment_type == 'PERSISTENT_1':
            fsx_lustre_configuration.PerUnitStorageThroughput = self.job.params.fsx_lustre.per_unit_throughput

        if self.job.params.fsx_lustre.s3_backend:
            fsx_lustre_configuration.ImportPath = self.job.get_fsx_lustre_import_path()
            fsx_lustre_configuration.ExportPath = self.job.get_fsx_lustre_export_path()

        fsx_tags = {constants.IDEA_TAG_FSX: 'true'}
        fsx_lustre.LustreConfiguration = fsx_lustre_configuration
        fsx_lustre.Tags = Tags(**self.get_common_tags(), **fsx_tags)
        return fsx_lustre

    def build_metrics(self) -> SocaAnonymousMetrics:
        spot_price = 'false'
        if self.job.params.spot:
            if self.job.params.spot_price:
                spot_price = str(self.job.params.spot_price.amount)
            else:
                spot_price = 'auto'
        metrics = SocaAnonymousMetrics('SendAnonymousData')
        solution_metrics_lambda_arn = self.context.config().get_string('cluster.solution.solution_metrics_lambda_arn', required=True)
        metrics.ServiceToken = solution_metrics_lambda_arn
        metrics.DesiredCapacity = str(self.job.ondemand_nodes() + self.job.spot_nodes())
        metrics.InstanceType = str(self.job.params.instance_types)
        metrics.Efa = str(self.job.params.enable_efa_support).lower()
        metrics.ScratchSize = str(self.job.params.scratch_storage_size.int_val())
        metrics.RootSize = str(self.job.params.root_storage_size.int_val())
        metrics.SpotPrice = spot_price
        metrics.BaseOS = str(self.job.params.base_os)
        metrics.StackUUID = self.job.provisioning_options.stack_uuid
        metrics.KeepForever = str(self.job.provisioning_options.keep_forever).lower()
        metrics.FsxLustre = str(self.job.params.fsx_lustre.enabled).lower()

        if Utils.get_as_bool(self.job.params.fsx_lustre.enabled, default=False):
            deployment_type = Utils.get_as_string(self.job.params.fsx_lustre.deployment_type, default=constants.DEFAULT_FSX_LUSTRE_DEPLOYMENT_TYPE).upper()

            if deployment_type in constants.FSX_LUSTRE_PER_UNIT_THROUGHPUT_TYPES:
                per_unit_throughput = Utils.get_as_int(self.job.params.fsx_lustre.per_unit_throughput, default=100)
            else:
                # Scratch filesystems
                per_unit_throughput = 200

            metrics.FsxLustreInfo = {
                'DeploymentType': deployment_type,
                'PerUnitStorageThroughput': per_unit_throughput,
                'Size': Utils.get_as_int(self.job.params.fsx_lustre.size.int_val(), default=0)
            }
        else:
            metrics.FsxLustreInfo = {}

        metrics.TerminateWhenIdle = str(self.job.provisioning_options.terminate_when_idle).lower()
        metrics.Dcv = 'false'
        metrics.Version = ideascheduler.__version__
        metrics.Region = self.context.config().get_string('cluster.aws.region', required=True)
        metrics.Misc = self.context.config().get_string('cluster.solution.custom_anonymous_metric_entry', required=False, default='')
        return metrics

    def build_template(self) -> str:

        # build launch template
        launch_template = self.build_launch_template()
        self.template.add_resource(launch_template)

        # build fleet
        if self.job.is_spot_capacity():

            # build spot fleet
            self.template.add_resource(self.build_spot_fleet(
                launch_template=launch_template
            ))

        else:

            # build mixed or ondemand fleet
            self.template.add_resource(self.build_auto_scaling_group(
                launch_template=launch_template
            ))

        # build fsx for lustre
        if self.job.params.fsx_lustre.enabled:
            self.template.add_resource(self.build_fsx_lustre())

        # send anonymous metrics to AWS
        if self.job.params.enable_anonymous_metrics:
            self.template.add_resource(self.build_metrics())

        return self.template.to_yaml()

    def build_cfn_stack_tags(self) -> list:
        # tags can be provided by user while provisioning always on capacity
        tags = self.job.provisioning_options.tags

        if tags is None:
            tags = {}

        common_tags = self.get_common_tags()
        del common_tags[constants.IDEA_TAG_COMPUTE_STACK]

        tags = {
            **tags,
            **common_tags,
            constants.IDEA_TAG_NODE_TYPE: constants.NODE_TYPE_COMPUTE,
            'Name': self.job.get_compute_stack()
        }

        return [{'Key': str(k), 'Value': str(v)} for k, v in tags.items() if v]

    def get_job_as_yaml_comment(self) -> str:
        """
        Add the Soca Job Information YML to Template as Comment.

        WARNING: Do not change this format. If changed, AWSUtil.get_soca_job_from_stack() will not be able to
        retrieve Soca Job information.
        :return:
        """
        job_json = Utils.to_json(self.job, indent=True)
        result = []
        lines = job_json.splitlines(keepends=True)
        result.append(f'{os.linesep}{os.linesep}{os.linesep}')
        result.append(f'# {"-" * 100} {os.linesep}')
        target_capacity_override = ''
        if self.target_capacity_override is not None:
            target_capacity_override = f' (TargetCapacity Override: {self.target_capacity_override})'
        result.append(f'# {" " * 40} IDEA Job{target_capacity_override}{os.linesep}')
        result.append(f'# {"-" * 100} {os.linesep}')
        for line in lines:
            result.append(f'# {line}')
        result.append(f'{os.linesep}')
        result.append(f'# {"-" * 100} {os.linesep}')
        return "".join(result)

    def get_template(self):
        yaml_job = self.get_job_as_yaml_comment()
        yaml_template = self.build_template()

        template = yaml_template + yaml_job

        return template

    def build(self) -> str:

        try:

            template = self.get_template()

            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f'---- CFN Template: Start ---{os.linesep}'
                                  f'{template}{os.linesep}'
                                  f'---- CFN Template: End ---')

            compute_stack = self.job.get_compute_stack()
            result = self.context.aws().cloudformation().create_stack(
                StackName=compute_stack,
                TemplateBody=template,
                Tags=self.build_cfn_stack_tags()
            )

            stack_id = Utils.get_value_as_string('StackId', result)

            return stack_id

        except Exception as e:
            raise exceptions.SocaException(
                error_code=errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED,
                message='cloudformation stack builder failed',
                exc=e
            )
