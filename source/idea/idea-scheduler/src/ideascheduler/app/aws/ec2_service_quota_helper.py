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
from ideasdk.utils import Utils
from ideadatamodel import (
    exceptions, errorcodes, constants,
    CheckServiceQuotaResult, ServiceQuota
)

from typing import Optional, List, Dict, Tuple, Set

MAX_RESULTS = 100


class EC2ServiceQuotaHelper:
    """
    Helper class to find if capacity is available for a given instance type to check agaisnt EC2 service quotas.

    > for on-demand capacity:
        EC2ServiceQuotaHelper(
            context=context,
            instance_types=['c5.large', 'c5.xlarge'],
            quota_type=EC2_SERVICE_QUOTA_ONDEMAND
        ).is_capacity_available()

    > for spot capacity
        EC2ServiceQuotaHelper(
            context=context,
            instance_types=['c5.large', 'c5.xlarge'],
            quota_type=EC2_SERVICE_QUOTA_SPOT
        ).is_capacity_available()

    > for dedicated:
        EC2ServiceQuotaHelper(
            context=context,
            instance_types=['c5.large', 'c5.xlarge'],
            quota_type=EC2_SERVICE_QUOTA_DEDICATED
        ).is_capacity_available()

    implementation notes:
        > performance of this class will be optimal if given instance types belong to the same instance class.
        > if instance types are from multiple instance classes, multiple quota's will be applicable to check
        > dedicated instances will result in separate checks as quota for each instance family is different.
    """

    def __init__(self, context: ideascheduler.AppContext,
                 instance_types: List[str],
                 quota_type: int,
                 desired_capacity: int = 0):
        self._context = context
        self._logger = context.logger()
        self.instance_types = instance_types
        self.quota_type = quota_type
        self.desired_capacity = desired_capacity

    def get_quota_type_name(self) -> str:
        if self.quota_type == constants.EC2_SERVICE_QUOTA_ONDEMAND:
            return 'on-demand'
        if self.quota_type == constants.EC2_SERVICE_QUOTA_SPOT:
            return 'spot'
        if self.quota_type == constants.EC2_SERVICE_QUOTA_DEDICATED:
            return 'dedicated'
        return 'unknown'  # should never happen

    def get_ec2_service_quotas(self) -> List[Dict]:
        """
        returns a cached value of ec2 service quotas
        todo - documentation note:
          if service quota is updated, soca administrator must reset the cache key: aws.ec2.service-quota.all
        """
        cache_key = 'aws.ec2.service-quota.all'
        result = self._context.cache().long_term().get(key=cache_key)
        if result is not None:
            return result

        result = []
        has_more = True
        next_token = None
        while has_more:

            if next_token:
                response = self._context.aws().service_quotas().list_service_quotas(
                    ServiceCode='ec2',
                    MaxResults=MAX_RESULTS,
                    NextToken=next_token
                )
            else:
                response = self._context.aws().service_quotas().list_service_quotas(
                    ServiceCode='ec2',
                    MaxResults=MAX_RESULTS
                )

            if 'Quotas' in response:
                result += response['Quotas']

            if 'NextToken' in response:
                next_token = response['NextToken']
                has_more = True
            else:
                has_more = False

        self._context.cache().long_term().set(key=cache_key, value=result)
        return result

    def get_instance_classes_from_quota(self, quota: Dict) -> Tuple[Optional[List[str]], Optional[int]]:
        """
        given a service quota, returns the instance class
        refer to response of:
         > aws service-quotas list-service-quotas --service-code ec2

         :returns Tuple with:
         > list of applicable instance classes [A, D, R ..]
         > int value representing:
            1: on-demand
            2: spot
            3: dedicated
        """

        quota_name = Utils.get_value_as_string('QuotaName', quota)
        usage_metric = Utils.get_value_as_dict('UsageMetric', quota)
        metric_dimensions = Utils.get_value_as_dict('MetricDimensions', usage_metric)
        metric_cls = Utils.get_value_as_string('Class', metric_dimensions)

        if metric_cls is not None:
            if metric_cls.startswith('Standard/'):
                # metric_cls examples:
                # > Standard/On-Demand
                # > Standard/Spot
                # quota_name examples:
                # > "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances"
                # > "All Standard (A, C, D, H, I, M, R, T, Z) Spot Instance Requests"
                quota_type = constants.EC2_SERVICE_QUOTA_SPOT if 'Spot' in metric_cls else constants.EC2_SERVICE_QUOTA_ONDEMAND
                token = quota_name[quota_name.index('(') + 1:quota_name.index(')')]
                instance_classes = token.replace(' ', '').split(',')
                return instance_classes, quota_type
            else:
                # metric_cls examples:
                # > F/OnDemand
                # > G/OnDemand
                # > HighMem/Spot
                # > Inf/Spot
                tokens = metric_cls.split('/')
                if len(tokens) == 2:
                    quota_type = constants.EC2_SERVICE_QUOTA_SPOT if 'Spot' in metric_cls else constants.EC2_SERVICE_QUOTA_ONDEMAND
                    if tokens[0] == 'G':
                        return ['G', 'VT'], quota_type
                    else:
                        return [metric_cls.split('/')[0]], quota_type
                else:
                    return None, None

        elif 'Dedicated' in quota_name:
            # examples:
            # > "Running Dedicated u-6tb1 Hosts"
            # > "Running Dedicated p4d Hosts"
            family = quota_name.split(' ')[2]
            instance_class, _ = self._context.aws_util().get_instance_type_class(instance_type=family)
            if instance_class is not None:
                return [instance_class], constants.EC2_SERVICE_QUOTA_DEDICATED

        return None, None

    def get_applicable_quotas(self) -> Optional[List[Dict]]:
        """
        returns applicable quota info from the list of ec2 service quotas
        :return: Optional[Dict] with below values:
            {
              "QuotaName": "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances",
              "UsageMetric": {
                "MetricDimensions": {
                  "Resource": "vCPU",
                  "Type": "Resource",
                  "Class": "Standard/OnDemand",
                  "Service": "EC2"
                },
                "MetricStatisticRecommendation": "Maximum",
                "MetricNamespace": "AWS/Usage",
                "MetricName": "ResourceCount"
              },
              "Adjustable": true,
              "QuotaArn": "arn:aws:servicequotas:...",
              "Value": 512.0,
              "ServiceName": "Amazon Elastic Compute Cloud (Amazon EC2)",
              "GlobalQuota": false,
              "ServiceCode": "ec2",
              "QuotaCode": "L-1216C47A",
              "Unit": "None"
            }
        """

        quotas = self.get_ec2_service_quotas()

        instance_classes = set()
        for instance_type in self.instance_types:
            instance_class, _ = self._context.aws_util().get_instance_type_class(instance_type=instance_type)
            instance_classes.add(instance_class)

        existing = set()
        result = []
        for quota in quotas:

            quota_classes, quota_type = self.get_instance_classes_from_quota(quota=quota)

            if quota_classes is None or quota_type is None:
                continue

            if self.quota_type != quota_type:
                continue

            for instance_class in instance_classes:
                if instance_class in quota_classes:
                    quota_code = quota['QuotaCode']
                    if quota_code in existing:
                        continue
                    result.append(quota)
                    existing.add(quota_code)

        return result

    def get_active_vcpu_count(self, instance_types: Set[str]) -> int:

        vcpu_count = 0
        instances = self._context.instance_cache.list_instances()
        for instance in instances:

            if instance.state not in ('running', 'pending', 'stopping', 'shutting-down'):
                continue

            if instance.instance_type not in instance_types:
                continue

            if self.quota_type == constants.EC2_SERVICE_QUOTA_SPOT:
                if instance.instance_lifecycle != 'spot':
                    continue
            elif self.quota_type == constants.EC2_SERVICE_QUOTA_ONDEMAND:
                if instance.instance_lifecycle == 'spot':
                    continue
            elif self.quota_type == constants.EC2_SERVICE_QUOTA_DEDICATED:
                if instance.placement_tenancy != 'dedicated':
                    continue

            ec2_instance_type = self._context.aws_util().get_ec2_instance_type(instance_type=instance.instance_type)
            vcpu_count += ec2_instance_type.vcpu_info_default_vcpus

        return vcpu_count

    def check_service_quota(self) -> CheckServiceQuotaResult:
        """
        :return: True if capacity is available, False if quota has exceeded
        :raise: SocaException if quota is not found for the given options.
            error codes:
            > SERVICE_QUOTA_NOT_FOUND
        """

        quotas = self.get_applicable_quotas()

        if quotas is None or len(quotas) == 0:
            raise exceptions.SocaException(
                error_code=errorcodes.SERVICE_QUOTA_NOT_FOUND,
                message=f'failed to find quota for instance_types: {self.instance_types}, '
                        f'quota_type: {self.get_quota_type_name()}'
            )

        result = CheckServiceQuotaResult(quotas=[])

        for quota in quotas:
            instance_types = set()
            instance_classes, _ = self.get_instance_classes_from_quota(quota=quota)
            for instance_class in instance_classes:
                instance_types |= set(self._context.aws_util().get_instance_types_for_class(
                    instance_class=instance_class
                ))

            vcpu_count = self.get_active_vcpu_count(instance_types=instance_types)

            # if any quota does not have capacity, return False
            # note: this could cause problems with weighted capacities, as not all instances
            #   will be launched
            # caller can decide to send only one instance type instead of multiple
            result.quotas.append(ServiceQuota(
                quota_name=quota['QuotaName'],
                available=int(quota['Value']),
                consumed=vcpu_count,
                desired=self.desired_capacity
            ))

        return result
