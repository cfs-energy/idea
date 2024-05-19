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

__all__ = (
    'EC2InstanceIdentityDocument',
    'EC2InstanceMonitorEvent',
    'EC2SpotFleetInstance',
    'AutoScalingGroupInstance',
    'ServiceQuota',
    'CheckServiceQuotaResult',
    'EC2InstanceUnitPrice',
    'AwsProjectBudget',
    'SocaAnonymousMetrics',
    'SESSendEmailRequest',
    'SESSendRawEmailRequest',
    'AWSPartition',
    'AWSRegion',
    'EC2PrefixList',
    'EC2PrefixListEntry',
    'CognitoUser',
    'CognitoUserMFAOptions',
    'CognitoUserPoolPasswordPolicy'
)

from pydantic import Field

from ideadatamodel.aws import EC2Instance
from ideadatamodel.common import SocaAmount
from ideadatamodel.model_utils import ModelUtils
from ideadatamodel import SocaBaseModel

from typing import Optional, List, Dict
from troposphere.cloudformation import AWSCustomObject
from datetime import datetime


class EC2InstanceIdentityDocument(SocaBaseModel):
    accountId: Optional[str] = Field(default=None)
    architecture: Optional[str] = Field(default=None)
    availabilityZone: Optional[str] = Field(default=None)
    billingProducts: Optional[str] = Field(default=None)
    devpayProductCodes: Optional[str] = Field(default=None)
    marketplaceProductCodes: Optional[str] = Field(default=None)
    imageId: Optional[str] = Field(default=None)
    instanceId: Optional[str] = Field(default=None)
    instanceType: Optional[str] = Field(default=None)
    kernelId: Optional[str] = Field(default=None)
    pendingTime: Optional[str] = Field(default=None)
    privateIp: Optional[str] = Field(default=None)
    ramdiskId: Optional[str] = Field(default=None)
    region: Optional[str] = Field(default=None)
    version: Optional[str] = Field(default=None)


class EC2InstanceMonitorEvent(SocaBaseModel):
    type: Optional[str] = Field(default=None)
    instance: Optional[EC2Instance] = Field(default=None)


class EC2SpotFleetInstance(SocaBaseModel):
    InstanceId: Optional[str] = Field(default=None)
    InstanceType: Optional[str] = Field(default=None)
    SpotInstanceRequestId: Optional[str] = Field(default=None)
    InstanceHealth: Optional[str] = Field(default=None)


class AutoScalingGroupInstance(SocaBaseModel):
    ProtectedFromScaleIn: Optional[bool] = Field(default=None)
    AvailabilityZone: Optional[str] = Field(default=None)
    InstanceId: Optional[str] = Field(default=None)
    WeightedCapacity: Optional[str] = Field(default=None)
    HealthStatus: Optional[str] = Field(default=None)
    LifecycleState: Optional[str] = Field(default=None)
    InstanceType: Optional[str] = Field(default=None)


class ServiceQuota(SocaBaseModel):
    quota_name: Optional[str] = Field(default=None)
    available: Optional[int] = Field(default=None)
    consumed: Optional[int] = Field(default=None)
    desired: Optional[int] = Field(default=None)


class CheckServiceQuotaResult(SocaBaseModel):
    quotas: Optional[List[ServiceQuota]] = Field(default=None)

    def is_available(self) -> bool:
        if self.quotas is None:
            return False
        for quota in self.quotas:
            consumed = ModelUtils.get_as_int(quota.consumed, 0)
            desired = ModelUtils.get_as_int(quota.desired, 0)
            available = ModelUtils.get_as_int(quota.available, 0)
            if consumed + desired > available:
                return False
        return True

    def find_insufficient_quotas(self) -> List[ServiceQuota]:
        if self.quotas is None:
            return []
        result = []
        for quota in self.quotas:
            available = ModelUtils.get_as_int(quota.available, 0)
            desired = ModelUtils.get_as_int(quota.desired, 0)
            if desired > available:
                result.append(quota)
        return result

    def __bool__(self):
        return self.is_available()


class EC2InstanceUnitPrice(SocaBaseModel):
    reserved: Optional[float] = Field(default=None)
    ondemand: Optional[float] = Field(default=None)


class AwsProjectBudget(SocaBaseModel):
    budget_name: Optional[str] = Field(default=None)
    budget_limit: Optional[SocaAmount] = Field(default=None)
    actual_spend: Optional[SocaAmount] = Field(default=None)
    forecasted_spend: Optional[SocaAmount] = Field(default=None)


class SocaAnonymousMetrics(AWSCustomObject):
    resource_type = "Custom::SendAnonymousMetrics"
    props = {
        "ServiceToken": (str, True),
        "DesiredCapacity": (str, True),
        "InstanceType": (str, True),
        "Efa": (str, True),
        "ScratchSize": (str, True),
        "RootSize": (str, True),
        "SpotPrice": (str, True),
        "BaseOS": (str, True),
        "StackUUID": (str, True),
        "KeepForever": (str, True),
        "TerminateWhenIdle": (str, True),
        "FsxLustre": (str, True),
        "FsxLustreInfo": (dict, True),
        "Dcv": (str, True),
        "Version": (str, True),
        "Misc": (str, True),
        "Region": (str, True)
    }


class SESSendEmailRequest(SocaBaseModel):
    to_addresses: Optional[List[str]] = Field(default=None)
    cc_addresses: Optional[List[str]] = Field(default=None)
    bcc_addresses: Optional[List[str]] = Field(default=None)
    subject: Optional[str] = Field(default=None)
    body: Optional[str] = Field(default=None)


class SESSendRawEmailRequest(SocaBaseModel):
    pass


class AWSRegion(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    region: Optional[str] = Field(default=None)


class AWSPartition(SocaBaseModel):
    name: Optional[str] = Field(default=None)
    partition: Optional[str] = Field(default=None)
    regions: List[AWSRegion]
    dns_suffix: Optional[str] = Field(default=None)

    def get_region(self, region: str) -> Optional[AWSRegion]:
        for region_ in self.regions:
            if region_.region == region:
                return region_
        return None


class EC2PrefixListEntry(SocaBaseModel):
    cidr: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)


class EC2PrefixList(SocaBaseModel):
    prefix_list_id: Optional[str] = Field(default=None)
    prefix_list_name: Optional[str] = Field(default=None)
    prefix_list_arn: Optional[str] = Field(default=None)
    max_entries: Optional[int] = Field(default=None)
    state: Optional[str] = Field(default=None)
    entries: Optional[List[EC2PrefixListEntry]] = Field(default=None)
    address_family: Optional[str] = Field(default=None)

    @staticmethod
    def from_aws_result(result: Dict) -> 'EC2PrefixList':
        return EC2PrefixList(
            prefix_list_id=ModelUtils.get_value_as_string('PrefixListId', result),
            prefix_list_name=ModelUtils.get_value_as_string('PrefixListName', result),
            prefix_list_arn=ModelUtils.get_value_as_string('PrefixListArn', result),
            state=ModelUtils.get_value_as_string('State', result),
            address_family=ModelUtils.get_value_as_string('AddressFamily', result),
            max_entries=ModelUtils.get_value_as_int('MaxEntries', result),
        )


class CognitoUserMFAOptions(SocaBaseModel):
    DeliveryMedium: Optional[str] = Field(default=None)
    AttributeName: Optional[str] = Field(default=None)


class CognitoUser(SocaBaseModel):
    Username: Optional[str] = Field(default=None)
    UserAttributes: Optional[List[Dict]] = Field(default=None)
    UserCreateDate: Optional[datetime] = Field(default=None)
    UserLastModifiedDate: Optional[datetime] = Field(default=None)
    Enabled: Optional[bool] = Field(default=None)
    UserStatus: Optional[str] = Field(default=None)
    MFAOptions: Optional[CognitoUserMFAOptions] = Field(default=None)
    PreferredMfaSetting: Optional[str] = Field(default=None)
    UserMFASettingList: Optional[List[str]] = Field(default=None)

    def get_user_attribute(self, name: str) -> Optional[str]:
        if ModelUtils.is_empty(self.UserAttributes):
            return None
        for attribute in self.UserAttributes:
            attr_name = ModelUtils.get_value_as_string('Name', attribute)
            if attr_name == name:
                return ModelUtils.get_value_as_string('Value', attribute)
        return None

    @property
    def email(self) -> Optional[str]:
        return self.get_user_attribute('email')

    @property
    def uid(self) -> Optional[int]:
        uid = self.get_user_attribute('custom:uid')
        return ModelUtils.get_as_int(uid, None)

    @property
    def gid(self) -> Optional[int]:
        gid = self.get_user_attribute('custom:gid')
        return ModelUtils.get_as_int(gid, None)


class CognitoUserPoolPasswordPolicy(SocaBaseModel):
    minimum_length: Optional[int] = Field(default=None)
    require_uppercase: Optional[bool] = Field(default=None)
    require_lowercase: Optional[bool] = Field(default=None)
    require_numbers: Optional[bool] = Field(default=None)
    require_symbols: Optional[bool] = Field(default=None)
    temporary_password_validity_days: Optional[int] = Field(default=None)
