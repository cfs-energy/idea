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

from ideadatamodel import constants
from ideasdk.utils import Utils
from ideasdk.bootstrap import BootstrapUserDataBuilder

import ideaadministrator
from ideaadministrator.app.cdk.stacks import IdeaBaseStack
from ideaadministrator.app.cdk.constructs import (
    ExistingSocaCluster,
    OAuthClientIdAndSecret,
    SQSQueue,
    Policy,
    Role,
    WebPortalSecurityGroup,
    IdeaNagSuppression,
)
from typing import Optional
import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_cognito as cognito,
    aws_sqs as sqs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_autoscaling as asg,
    aws_kms as kms,
    aws_secretsmanager as secretsmanager,
)
import constructs


class ClusterManagerStack(IdeaBaseStack):
    """
    Setup infrastructure for IDEA Cluster Manager Module
    """

    def __init__(
        self,
        scope: constructs.Construct,
        cluster_name: str,
        aws_region: str,
        aws_profile: str,
        module_id: str,
        deployment_id: str,
        termination_protection: bool = True,
        env: cdk.Environment = None,
    ):
        super().__init__(
            scope=scope,
            cluster_name=cluster_name,
            aws_region=aws_region,
            aws_profile=aws_profile,
            module_id=module_id,
            deployment_id=deployment_id,
            termination_protection=termination_protection,
            description=f'ModuleId: {module_id}, Cluster: {cluster_name}, Version: {ideaadministrator.props.current_release_version}',
            tags={
                constants.IDEA_TAG_MODULE_ID: module_id,
                constants.IDEA_TAG_MODULE_NAME: constants.MODULE_CLUSTER_MANAGER,
                constants.IDEA_TAG_MODULE_VERSION: ideaadministrator.props.current_release_version,
            },
            env=env,
        )

        self.bootstrap_package_uri = self.stack.node.try_get_context(
            'bootstrap_package_uri'
        )
        self.cluster = ExistingSocaCluster(self.context, self.stack)

        self.oauth2_client_secret: Optional[OAuthClientIdAndSecret] = None
        self.jwt_signing_secret: Optional[cdk.aws_secretsmanager.Secret] = None
        self.cluster_tasks_sqs_queue: Optional[SQSQueue] = None
        self.notifications_sqs_queue: Optional[SQSQueue] = None
        self.cluster_manager_role: Optional[Role] = None
        self.cluster_manager_security_group: Optional[WebPortalSecurityGroup] = None
        self.auto_scaling_group: Optional[asg.AutoScalingGroup] = None
        self.web_portal_endpoint: Optional[cdk.CustomResource] = None
        self.external_endpoint: Optional[cdk.CustomResource] = None
        self.internal_endpoint: Optional[cdk.CustomResource] = None

        self.user_pool = self.lookup_user_pool()

        self.build_oauth2_client()
        self.build_jwt_signing_secret()
        self.build_access_control_groups(user_pool=self.user_pool)
        self.build_sqs_queues()
        self.build_iam_roles()
        self.build_security_groups()
        self.build_auto_scaling_group()
        self.build_endpoints()
        self.build_cluster_settings()

    def build_oauth2_client(self):
        # add resource server
        resource_server = self.user_pool.add_resource_server(
            id='resource-server',
            identifier=self.module_id,
            scopes=[
                cognito.ResourceServerScope(
                    scope_name='read', scope_description='Allow Read Access'
                ),
                cognito.ResourceServerScope(
                    scope_name='write', scope_description='Allow Write Access'
                ),
            ],
        )

        # add new client to user pool
        refresh_token_validity_hours = self.context.config().get_int(
            'cluster-manager.oauth2_client.refresh_token_validity_hours', default=24
        )
        client = self.user_pool.add_client(
            id=f'{self.module_id}-client',
            access_token_validity=cdk.Duration.hours(1),
            auth_flows=cognito.AuthFlow(admin_user_password=True),
            generate_secret=True,
            id_token_validity=cdk.Duration.hours(1),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[
                    cognito.OAuthScope.custom(f'{self.module_id}/read'),
                    cognito.OAuthScope.custom(f'{self.module_id}/write'),
                ],
            ),
            refresh_token_validity=cdk.Duration.hours(refresh_token_validity_hours),
            user_pool_client_name=self.module_id,
        )
        client.node.add_dependency(resource_server)

        # read secret value by invoking custom resource
        oauth_credentials_lambda_arn = self.context.config().get_string(
            'identity-provider.cognito.oauth_credentials_lambda_arn', required=True
        )
        client_secret = cdk.CustomResource(
            scope=self.stack,
            id=f'{self.module_id}-creds',
            service_token=oauth_credentials_lambda_arn,
            properties={
                'UserPoolId': self.user_pool.user_pool_id,
                'ClientId': client.user_pool_client_id,
            },
            resource_type='Custom::GetOAuthCredentials',
        )

        # save client id and client secret to AWS Secrets Manager
        self.oauth2_client_secret = OAuthClientIdAndSecret(
            context=self.context,
            secret_name_prefix=self.module_id,
            module_name=constants.MODULE_CLUSTER_MANAGER,
            scope=self.stack,
            client_id=client.user_pool_client_id,
            client_secret=client_secret.get_att_string('ClientSecret'),
        )

    def build_jwt_signing_secret(self):
        """
        Create a dedicated JWT signing secret for secure file download URLs.
        This secret is used to sign temporary download tokens.
        """
        kms_key_id = self.context.config().get_string(
            'cluster.secretsmanager.kms_key_id'
        )

        # Create the JWT signing secret using CloudFormation directly for CDK v2 compatibility
        self.jwt_signing_secret = secretsmanager.CfnSecret(
            self.stack,
            f'{self.module_id}-jwt-signing-secret',
            name=f'{self.cluster_name}-{self.module_id}-jwt-signing-secret',
            description=f'JWT signing secret for {self.module_id} secure file downloads',
            generate_secret_string=secretsmanager.CfnSecret.GenerateSecretStringProperty(
                secret_string_template='{}',
                generate_string_key='secret',
                exclude_characters=' "\'\\/`',
                include_space=False,
                password_length=64,
                require_each_included_type=False,
            ),
            kms_key_id=self.get_kms_key_arn(kms_key_id) if kms_key_id else None,
            tags=[
                cdk.CfnTag(key='idea:ClusterName', value=self.cluster_name),
                cdk.CfnTag(key='idea:ModuleName', value=self.module_id),
                cdk.CfnTag(key='idea:SecretType', value='jwt-signing'),
                cdk.CfnTag(key='idea:Purpose', value='file-download-authentication'),
            ],
        )

    def build_iam_roles(self):
        ec2_managed_policies = self.get_ec2_instance_managed_policies()

        self.cluster_manager_role = Role(
            context=self.context,
            name=f'{self.module_id}-role',
            scope=self.stack,
            description='IAM role assigned to the cluster-manager',
            assumed_by=['ssm', 'ec2'],
            managed_policies=ec2_managed_policies,
        )
        self.cluster_manager_role.attach_inline_policy(
            Policy(
                context=self.context,
                name='cluster-manager-policy',
                scope=self.stack,
                policy_template_name='cluster-manager.yml',
                module_id=self.module_id,
            )
        )

    def build_security_groups(self):
        self.cluster_manager_security_group = WebPortalSecurityGroup(
            context=self.context,
            name=f'{self.module_id}-security-group',
            scope=self.stack,
            vpc=self.cluster.vpc,
            bastion_host_security_group=self.cluster.get_security_group('bastion-host'),
            loadbalancer_security_group=self.cluster.get_security_group(
                'external-load-balancer'
            ),
        )

    def build_sqs_queues(self):
        kms_key_id = self.context.config().get_string('cluster.sqs.kms_key_id')

        self.cluster_tasks_sqs_queue = SQSQueue(
            self.context,
            'cluster-tasks-sqs-queue',
            self.stack,
            queue_name=f'{self.cluster_name}-{self.module_id}-tasks.fifo',
            fifo=True,
            content_based_deduplication=True,
            encryption_master_key=kms_key_id,
            visibility_timeout=cdk.Duration.seconds(constants.SQS_VISIBILITY_TASKS),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=Utils.get_as_int(
                    constants.SQS_MAX_RECEIVE_COUNT_CLUSTER_TASKS, default=16
                ),
                queue=SQSQueue(
                    self.context,
                    'cluster-tasks-sqs-queue-dlq',
                    self.stack,
                    queue_name=f'{self.cluster_name}-{self.module_id}-tasks-dlq.fifo',
                    fifo=True,
                    content_based_deduplication=True,
                    encryption_master_key=kms_key_id,
                    is_dead_letter_queue=True,
                ),
            ),
        )
        self.add_common_tags(self.cluster_tasks_sqs_queue)
        self.add_common_tags(self.cluster_tasks_sqs_queue.dead_letter_queue.queue)

        self.notifications_sqs_queue = SQSQueue(
            self.context,
            'notifications-sqs-queue',
            self.stack,
            queue_name=f'{self.cluster_name}-{self.module_id}-notifications.fifo',
            fifo=True,
            content_based_deduplication=True,
            encryption_master_key=kms_key_id,
            visibility_timeout=cdk.Duration.seconds(
                constants.SQS_VISIBILITY_NOTIFICATIONS
            ),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=Utils.get_as_int(
                    constants.SQS_MAX_RECEIVE_COUNT_NOTIFICATIONS, default=3
                ),
                queue=SQSQueue(
                    self.context,
                    'notifications-sqs-queue-dlq',
                    self.stack,
                    queue_name=f'{self.cluster_name}-{self.module_id}-notifications-dlq.fifo',
                    fifo=True,
                    content_based_deduplication=True,
                    encryption_master_key=kms_key_id,
                    is_dead_letter_queue=True,
                ),
            ),
        )
        self.add_common_tags(self.notifications_sqs_queue)
        self.add_common_tags(self.notifications_sqs_queue.dead_letter_queue.queue)

    def build_auto_scaling_group(self):
        key_pair_name = self.context.config().get_string(
            'cluster.network.ssh_key_pair', required=True
        )
        is_public = (
            self.context.config().get_bool(
                'cluster-manager.ec2.autoscaling.public', False
            )
            and len(self.cluster.public_subnets) > 0
        )
        base_os = self.context.config().get_string(
            'cluster-manager.ec2.autoscaling.base_os', required=True
        )
        instance_ami = self.context.config().get_string(
            'cluster-manager.ec2.autoscaling.instance_ami', required=True
        )
        instance_type = self.context.config().get_string(
            'cluster-manager.ec2.autoscaling.instance_type', required=True
        )
        volume_size = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.volume_size', default=200
        )
        enable_detailed_monitoring = self.context.config().get_bool(
            'cluster-manager.ec2.autoscaling.enable_detailed_monitoring', default=False
        )
        min_capacity = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.min_capacity', default=1
        )
        max_capacity = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.max_capacity', default=3
        )
        cooldown_minutes = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.cooldown_minutes', default=5
        )
        new_instances_protected_from_scale_in = self.context.config().get_bool(
            'cluster-manager.ec2.autoscaling.new_instances_protected_from_scale_in',
            default=True,
        )
        elb_healthcheck_grace_time_minutes = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.elb_healthcheck.grace_time_minutes',
            default=15,
        )
        scaling_policy_target_utilization_percent = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.cpu_utilization_scaling_policy.target_utilization_percent',
            default=80,
        )
        scaling_policy_estimated_instance_warmup_minutes = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.cpu_utilization_scaling_policy.estimated_instance_warmup_minutes',
            default=15,
        )
        rolling_update_max_batch_size = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.rolling_update_policy.max_batch_size',
            default=1,
        )
        rolling_update_min_instances_in_service = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.rolling_update_policy.min_instances_in_service',
            default=1,
        )
        rolling_update_pause_time_minutes = self.context.config().get_int(
            'cluster-manager.ec2.autoscaling.rolling_update_policy.pause_time_minutes',
            default=15,
        )
        metadata_http_tokens = self.context.config().get_string(
            'cluster-manager.ec2.autoscaling.metadata_http_tokens', required=True
        )
        https_proxy = self.context.config().get_string(
            'cluster.network.https_proxy', required=False, default=''
        )
        no_proxy = self.context.config().get_string(
            'cluster.network.no_proxy', required=False, default=''
        )
        proxy_config = {}
        if Utils.is_not_empty(https_proxy):
            proxy_config = {
                'http_proxy': https_proxy,
                'https_proxy': https_proxy,
                'no_proxy': no_proxy,
            }
        kms_key_id = self.context.config().get_string(
            'cluster.ebs.kms_key_id', required=False, default=None
        )
        if kms_key_id is not None:
            kms_key_arn = self.get_kms_key_arn(kms_key_id)
            ebs_kms_key = kms.Key.from_key_arn(
                scope=self.stack, id='ebs-kms-key', key_arn=kms_key_arn
            )
        else:
            ebs_kms_key = kms.Alias.from_alias_name(
                scope=self.stack, id='ebs-kms-key-default', alias_name='alias/aws/ebs'
            )

        if is_public:
            vpc_subnets = ec2.SubnetSelection(subnets=self.cluster.public_subnets)
        else:
            vpc_subnets = ec2.SubnetSelection(subnets=self.cluster.private_subnets)

        block_device_name = Utils.get_ec2_block_device_name(base_os)
        block_device_type_string = self.context.config().get_string(
            'cluster-manager.ec2.autoscaling.volume_type', default='gp3'
        )
        block_device_type_volumetype = (
            ec2.EbsDeviceVolumeType.GP3
            if block_device_type_string == 'gp3'
            else ec2.EbsDeviceVolumeType.GP2
        )

        user_data = BootstrapUserDataBuilder(
            aws_region=self.aws_region,
            bootstrap_package_uri=self.bootstrap_package_uri,
            install_commands=['/bin/bash cluster-manager/setup.sh'],
            proxy_config=proxy_config,
            base_os=base_os,
        ).build()

        launch_template = ec2.LaunchTemplate(
            self.stack,
            f'{self.module_id}-lt',
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ec2.MachineImage.generic_linux(
                {self.aws_region: instance_ami}
            ),
            security_group=self.cluster_manager_security_group,
            user_data=ec2.UserData.custom(cdk.Fn.sub(user_data)),
            key_pair=ec2.KeyPair.from_key_pair_name(
                self.stack, f'{self.module_id}-key-pair', key_pair_name
            ),
            block_devices=[
                ec2.BlockDevice(
                    device_name=block_device_name,
                    volume=ec2.BlockDeviceVolume(
                        ebs_device=ec2.EbsDeviceProps(
                            encrypted=True,
                            kms_key=ebs_kms_key,
                            volume_size=volume_size,
                            volume_type=block_device_type_volumetype,
                        )
                    ),
                )
            ],
            role=self.cluster_manager_role,
            require_imdsv2=True if metadata_http_tokens == 'required' else False,
        )

        self.auto_scaling_group = asg.AutoScalingGroup(
            self.stack,
            'cluster-manager-asg',
            vpc=self.cluster.vpc,
            vpc_subnets=vpc_subnets,
            auto_scaling_group_name=f'{self.cluster_name}-{self.module_id}-asg',
            launch_template=launch_template,
            instance_monitoring=asg.Monitoring.DETAILED
            if enable_detailed_monitoring
            else asg.Monitoring.BASIC,
            group_metrics=[asg.GroupMetrics.all()],
            min_capacity=min_capacity,
            max_capacity=max_capacity,
            new_instances_protected_from_scale_in=new_instances_protected_from_scale_in,
            cooldown=cdk.Duration.minutes(cooldown_minutes),
            health_checks=asg.HealthChecks.with_additional_checks(
                additional_types=[asg.AdditionalHealthCheckType.ELB],
                grace_period=cdk.Duration.minutes(elb_healthcheck_grace_time_minutes),
            ),
            update_policy=asg.UpdatePolicy.rolling_update(
                max_batch_size=rolling_update_max_batch_size,
                min_instances_in_service=rolling_update_min_instances_in_service,
                pause_time=cdk.Duration.minutes(rolling_update_pause_time_minutes),
            ),
            termination_policies=[asg.TerminationPolicy.DEFAULT],
        )

        self.auto_scaling_group.scale_on_cpu_utilization(
            'cpu-utilization-scaling-policy',
            target_utilization_percent=scaling_policy_target_utilization_percent,
            estimated_instance_warmup=cdk.Duration.minutes(
                scaling_policy_estimated_instance_warmup_minutes
            ),
        )

        cdk.Tags.of(self.auto_scaling_group).add(
            constants.IDEA_TAG_NODE_TYPE, constants.NODE_TYPE_APP
        )
        cdk.Tags.of(self.auto_scaling_group).add(
            constants.IDEA_TAG_NAME, f'{self.cluster_name}-{self.module_id}'
        )
        self.auto_scaling_group.node.add_dependency(self.cluster_tasks_sqs_queue)
        self.auto_scaling_group.node.add_dependency(self.notifications_sqs_queue)

        if not enable_detailed_monitoring:
            self.add_nag_suppression(
                construct=self.auto_scaling_group,
                suppressions=[
                    IdeaNagSuppression(
                        rule_id='AwsSolutions-EC28',
                        reason='detailed monitoring is a configurable option to save costs',
                    )
                ],
                apply_to_children=True,
            )

        self.add_nag_suppression(
            construct=self.auto_scaling_group,
            suppressions=[
                IdeaNagSuppression(
                    rule_id='AwsSolutions-AS3',
                    reason='ASG notifications scaling notifications can be managed via AWS Console',
                )
            ],
        )

    def build_endpoints(self):
        cluster_endpoints_lambda_arn = self.context.config().get_string(
            'cluster.cluster_endpoints_lambda_arn', required=True
        )

        external_https_listener_arn = self.context.config().get_string(
            'cluster.load_balancers.external_alb.https_listener_arn', required=True
        )
        # web portal endpoint
        default_target_group = elbv2.CfnTargetGroup(
            self.stack,
            'web-portal-target-group',
            port=8443,
            protocol='HTTPS',
            target_type='instance',
            vpc_id=self.cluster.vpc.vpc_id,
            name=self.get_target_group_name('web-portal'),
            health_check_path='/healthcheck',
        )

        self.web_portal_endpoint = cdk.CustomResource(
            self.stack,
            'web-portal-endpoint',
            service_token=cluster_endpoints_lambda_arn,
            properties={
                'endpoint_name': f'{self.module_id}-web-portal-endpoint',
                'listener_arn': external_https_listener_arn,
                'priority': 0,
                'default_action': True,
                'actions': [
                    {'Type': 'forward', 'TargetGroupArn': default_target_group.ref}
                ],
            },
            resource_type='Custom::WebPortalEndpoint',
        )

        # cluster manager api external endpoint
        external_endpoint_priority = self.context.config().get_int(
            'cluster-manager.endpoints.external.priority', required=True
        )
        external_endpoint_path_patterns = self.context.config().get_list(
            'cluster-manager.endpoints.external.path_patterns', required=True
        )
        external_target_group = elbv2.CfnTargetGroup(
            self.stack,
            f'{self.module_id}-external-target-group',
            port=8443,
            protocol='HTTPS',
            target_type='instance',
            vpc_id=self.cluster.vpc.vpc_id,
            name=self.get_target_group_name('cm-ext'),
            health_check_path='/healthcheck',
        )
        self.external_endpoint = cdk.CustomResource(
            self.stack,
            'external-endpoint',
            service_token=cluster_endpoints_lambda_arn,
            properties={
                'endpoint_name': f'{self.module_id}-external-endpoint',
                'listener_arn': external_https_listener_arn,
                'priority': external_endpoint_priority,
                'conditions': [
                    {'Field': 'path-pattern', 'Values': external_endpoint_path_patterns}
                ],
                'actions': [
                    {'Type': 'forward', 'TargetGroupArn': external_target_group.ref}
                ],
            },
            resource_type='Custom::ClusterManagerEndpointExternal',
        )

        # cluster manager api internal endpoint
        internal_https_listener_arn = self.context.config().get_string(
            'cluster.load_balancers.internal_alb.https_listener_arn', required=True
        )
        internal_endpoint_priority = self.context.config().get_int(
            'cluster-manager.endpoints.internal.priority', required=True
        )
        internal_endpoint_path_patterns = self.context.config().get_list(
            'cluster-manager.endpoints.internal.path_patterns', required=True
        )
        internal_target_group = elbv2.CfnTargetGroup(
            self.stack,
            f'{self.module_id}-internal-target-group',
            port=8443,
            protocol='HTTPS',
            target_type='instance',
            vpc_id=self.cluster.vpc.vpc_id,
            name=self.get_target_group_name('cm-int'),
            health_check_path='/healthcheck',
        )
        self.internal_endpoint = cdk.CustomResource(
            self.stack,
            'internal-endpoint',
            service_token=cluster_endpoints_lambda_arn,
            properties={
                'endpoint_name': f'{self.module_id}-internal-endpoint',
                'listener_arn': internal_https_listener_arn,
                'priority': internal_endpoint_priority,
                'conditions': [
                    {'Field': 'path-pattern', 'Values': internal_endpoint_path_patterns}
                ],
                'actions': [
                    {'Type': 'forward', 'TargetGroupArn': internal_target_group.ref}
                ],
            },
            resource_type='Custom::ClusterManagerEndpointInternal',
        )

        # register target groups with ASG
        self.auto_scaling_group.node.default_child.target_group_arns = [
            default_target_group.ref,
            internal_target_group.ref,
            external_target_group.ref,
        ]

    def build_cluster_settings(self):
        cluster_settings = {
            'deployment_id': self.deployment_id,
            'client_id': self.oauth2_client_secret.client_id.ref,
            'client_secret': self.oauth2_client_secret.client_secret.ref,
            'security_group_id': self.cluster_manager_security_group.security_group_id,
            'iam_role_arn': self.cluster_manager_role.role_arn,
            'task_queue_url': self.cluster_tasks_sqs_queue.queue_url,
            'task_queue_arn': self.cluster_tasks_sqs_queue.queue_arn,
            'notifications_queue_url': self.notifications_sqs_queue.queue_url,
            'notifications_queue_arn': self.notifications_sqs_queue.queue_arn,
            'asg_name': self.auto_scaling_group.auto_scaling_group_name,
            'asg_arn': self.auto_scaling_group.auto_scaling_group_arn,
        }

        # Add JWT signing secret ARN if available
        if hasattr(self, 'jwt_signing_secret') and self.jwt_signing_secret is not None:
            cluster_settings['jwt_signing_secret_arn'] = self.jwt_signing_secret.ref

        self.update_cluster_settings(cluster_settings)
