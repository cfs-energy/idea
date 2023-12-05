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

import ideaclustermanager

from ideasdk.api import ApiInvocationContext, BaseAPI
from ideadatamodel.cluster_settings import (
    ListClusterModulesResult,
    ListClusterHostsRequest,
    ListClusterHostsResult,
    GetModuleSettingsRequest,
    GetModuleSettingsResult,
    DescribeInstanceTypesResult
)
from ideadatamodel import exceptions, constants
from ideasdk.utils import Utils

from threading import RLock


class ClusterSettingsAPI(BaseAPI):

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.instance_types_lock = RLock()

    def list_cluster_modules(self, context: ApiInvocationContext):
        cluster_modules = self.context.get_cluster_modules()
        context.success(ListClusterModulesResult(
            listing=cluster_modules
        ))

    def get_module_settings(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetModuleSettingsRequest)

        module_id = request.module_id
        if Utils.is_empty(module_id):
            raise exceptions.invalid_params('module_id is required')

        module_config = self.context.config().get_config(module_id, module_id=module_id)

        context.success(GetModuleSettingsResult(
            settings=module_config.as_plain_ordered_dict()
        ))

    def list_cluster_hosts(self, context: ApiInvocationContext):
        # returns all infrastructure instances
        request = context.get_request_payload_as(ListClusterHostsRequest)
        ec2_instances = self.context.aws_util().ec2_describe_instances(
            filters=[
                {
                    'Name': 'instance-state-name',
                    'Values': ['pending', 'stopped', 'running']
                },
                {
                    'Name': f'tag:{constants.IDEA_TAG_CLUSTER_NAME}',
                    'Values': [self.context.cluster_name()]
                },
                {
                    'Name': f'tag:{constants.IDEA_TAG_NODE_TYPE}',
                    'Values': [constants.NODE_TYPE_INFRA, constants.NODE_TYPE_APP, constants.NODE_TYPE_AMI_BUILDER]
                }
            ],
            page_size=request.page_size
        )
        result = []
        for instance in ec2_instances:
            result.append(instance.instance_data())

        context.success(ListClusterHostsResult(
            listing=result
        ))

    def describe_instance_types(self, context: ApiInvocationContext):

        instance_types = self.context.cache().long_term().get('aws.ec2.all-instance-types')
        if instance_types is None:
            with self.instance_types_lock:

                instance_types = self.context.cache().long_term().get('aws.ec2.all-instance-types')
                if instance_types is None:
                    instance_types = []
                    has_more = True
                    next_token = None

                    while has_more:
                        if next_token is None:
                            result = self.context.aws().ec2().describe_instance_types(MaxResults=100)
                        else:
                            result = self.context.aws().ec2().describe_instance_types(MaxResults=100, NextToken=next_token)

                        next_token = Utils.get_value_as_string('NextToken', result)
                        has_more = Utils.is_not_empty(next_token)
                        current_instance_types = Utils.get_value_as_list('InstanceTypes', result)
                        if len(current_instance_types) > 0:
                            instance_types += current_instance_types

                    self.context.cache().long_term().set('aws.ec2.all-instance-types', instance_types)

        context.success(DescribeInstanceTypesResult(
            instance_types=instance_types
        ))

    def invoke(self, context: ApiInvocationContext):
        if not context.is_authenticated():
            raise exceptions.unauthorized_access()

        namespace = context.namespace
        if namespace == 'ClusterSettings.ListClusterModules':
            self.list_cluster_modules(context)
        elif namespace == 'ClusterSettings.GetModuleSettings':
            self.get_module_settings(context)
        elif namespace == 'ClusterSettings.ListClusterHosts':
            self.list_cluster_hosts(context)
        elif namespace == 'ClusterSettings.DescribeInstanceTypes':
            self.describe_instance_types(context)
