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
    'FetchPricingRatesRequest',
    'FetchPricingRatesResult',
    'PricingRateKey',
    'DescribeSettingsCatalogRequest',
    'DescribeSettingsCatalogResult',
    'SettingDefinition',
    'SettingValidation',
    'ListClusterModulesRequest',
    'ListClusterModulesResult',
    'GetModuleSettingsResult',
    'GetModuleSettingsRequest',
    'UpdateModuleSettingsRequest',
    'UpdateModuleSettingsResult',
    'ListClusterServicesRequest',
    'ListClusterServicesResult',
    'ClusterService',
    'ClusterServiceTask',
    'ListClusterHostsRequest',
    'ListClusterHostsResult',
    'DescribeInstanceTypesRequest',
    'DescribeInstanceTypesResult',
    'OPEN_API_SPEC_ENTRIES_CLUSTER_SETTINGS',
)

from ideadatamodel import SocaPayload, SocaListingPayload, IdeaOpenAPISpecEntry

from typing import Optional, List, Any, Dict, Literal, Annotated
from pydantic import Field


PricingRateKey = Literal[
    'ebs_gp3_storage', 'ebs_io1_storage', 'provisioned_iops', 'fsx_lustre'
]


class FetchPricingRatesRequest(SocaPayload):
    region: str = Field(min_length=1, max_length=64)


class FetchPricingRatesResult(SocaPayload):
    region: str
    as_of: str
    rates: Dict[PricingRateKey, Annotated[float, Field(ge=0, allow_inf_nan=False)]] = (
        Field(default_factory=dict)
    )
    unavailable: Dict[PricingRateKey, str] = Field(default_factory=dict)
    assumptions: List[str] = Field(default_factory=list)


class SettingValidation(SocaPayload):
    required: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    pattern: Optional[str] = None


class SettingDefinition(SocaPayload):
    key: str
    module: str
    path: str
    group: str
    section: str
    label: str
    description: str
    value_type: Literal[
        'string', 'integer', 'number', 'boolean', 'list', 'enum', 'secret'
    ]
    choices: List[str] = Field(default_factory=list)
    validation: SettingValidation
    advanced: bool
    read_only: bool = False
    hidden: bool = False
    effect: Literal['runtime', 'restart', 'deployment']


class DescribeSettingsCatalogRequest(SocaPayload):
    pass


class DescribeSettingsCatalogResult(SocaPayload):
    settings: List[SettingDefinition]


# ClusterSettings.ListClusterModules


class ListClusterModulesRequest(SocaListingPayload):
    pass


class ListClusterModulesResult(SocaListingPayload):
    listing: Optional[List[Any]] = Field(default=None)


# ClusterSettings.GetClusterModule


class GetModuleSettingsRequest(SocaPayload):
    module_id: Optional[str] = Field(default=None)


class GetModuleSettingsResult(SocaPayload):
    settings: Optional[Any] = Field(default=None)


# ClusterSettings.UpdateModuleSettings


class UpdateModuleSettingsRequest(SocaPayload):
    module_id: Optional[str] = Field(default=None)
    settings: Optional[Any] = Field(default=None)


class UpdateModuleSettingsResult(SocaPayload):
    success: Optional[bool] = Field(default=True)
    effects: Dict[str, str] = Field(default_factory=dict)


# ClusterSettings.ListClusterHosts
class ListClusterHostsRequest(SocaListingPayload):
    instance_ids: Optional[List[str]] = Field(default=None)


class ListClusterHostsResult(SocaListingPayload):
    listing: Optional[List[Any]] = Field(default=None)


class ClusterServiceTask(SocaPayload):
    task_id: str
    started_at: Optional[str] = None
    health: str = 'UNKNOWN'


class ClusterService(SocaPayload):
    name: str
    desired: int = 0
    running: int = 0
    pending: int = 0
    images: List[str] = Field(default_factory=list)
    rollout_state: Optional[str] = None
    updated_at: Optional[str] = None
    tasks: List[ClusterServiceTask] = Field(default_factory=list)


class ListClusterServicesRequest(SocaPayload):
    pass


class ListClusterServicesResult(SocaPayload):
    listing: List[ClusterService] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


# ClusterSettings.DescribeInstanceTypes
class DescribeInstanceTypesRequest(SocaPayload):
    pass


class DescribeInstanceTypesResult(SocaPayload):
    instance_types: List[Any]


OPEN_API_SPEC_ENTRIES_CLUSTER_SETTINGS = [
    IdeaOpenAPISpecEntry(
        namespace='ClusterSettings.FetchPricingRates',
        request=FetchPricingRatesRequest,
        result=FetchPricingRatesResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='ClusterSettings.ListClusterServices',
        request=ListClusterServicesRequest,
        result=ListClusterServicesResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='ClusterSettings.DescribeSettingsCatalog',
        request=DescribeSettingsCatalogRequest,
        result=DescribeSettingsCatalogResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='ClusterSettings.ListClusterModules',
        request=ListClusterModulesRequest,
        result=ListClusterModulesResult,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='ClusterSettings.GetModuleSettings',
        request=GetModuleSettingsRequest,
        result=GetModuleSettingsResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='ClusterSettings.UpdateModuleSettings',
        request=UpdateModuleSettingsRequest,
        result=UpdateModuleSettingsResult,
        is_listing=False,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='ClusterSettings.ListClusterHosts',
        request=ListClusterHostsRequest,
        result=ListClusterHostsResult,
        is_listing=True,
        is_public=False,
    ),
    IdeaOpenAPISpecEntry(
        namespace='ClusterSettings.DescribeInstanceTypes',
        request=DescribeInstanceTypesRequest,
        result=DescribeInstanceTypesResult,
        is_listing=False,
        is_public=False,
    ),
]
