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
    'SocaPayload',
    'get_payload_as',
    'SocaListingPayload',
    'SocaHeader',
    'SocaAuthScope',
    'SocaEnvelope',
    'SocaAnyPayload',
    'SocaBatchResponsePayload',
    'IdeaOpenAPISpecEntry',
)

from ideadatamodel import (
    SocaBaseModel,
    SocaDateRange,
    SocaSortBy,
    SocaPaginator,
    SocaFilter,
)
from ideadatamodel.model_utils import ModelUtils

from typing import Optional, Union, TypeVar, Type, List, Any
from types import SimpleNamespace
from pydantic import Field

SocaBaseModelType = TypeVar('SocaBaseModelType', bound='SocaBaseModel')


class SocaPayload(SocaBaseModel):
    pass


class SocaBatchResponsePayload(SocaPayload):
    failed: Optional[List[Any]] = Field(default=None)
    success: Optional[List[Any]] = Field(default=None)


SocaPayloadType = TypeVar('SocaPayloadType', bound='SocaPayload')


def get_payload_as(
    payload: Optional[Union[dict, SocaPayload]], payload_type: Type[SocaPayloadType]
) -> Optional[SocaPayloadType]:
    if payload is None:
        return None
    if isinstance(payload, payload_type):
        return payload
    elif isinstance(payload, SocaBaseModel):
        return payload_type(**payload.model_dump(exclude_none=True, by_alias=True))
    elif isinstance(payload, dict):
        return payload_type(**payload)
    return None


class SocaListingPayload(SocaPayload):
    paginator: Optional[SocaPaginator] = Field(default=None)
    sort_by: Optional[SocaSortBy] = Field(default=None)
    date_range: Optional[SocaDateRange] = Field(default=None)
    listing: Optional[List[Union[SocaBaseModel, Any]]] = Field(default=None)
    filters: Optional[List[SocaFilter]] = Field(default=None)

    @property
    def page_size(self) -> int:
        if self.paginator is None:
            return 50
        return ModelUtils.get_as_int(self.paginator.page_size)

    @property
    def cursor(self) -> Optional[str]:
        if self.paginator is None:
            return None
        return self.paginator.cursor

    @property
    def page_start(self) -> int:
        if self.paginator is None:
            return 0
        return ModelUtils.get_as_int(self.paginator.start)

    def get_filter(self, key: str) -> Optional[SocaFilter]:
        if self.filters is None:
            return None
        for filter_ in self.filters:
            if ModelUtils.are_equal(key, filter_.key):
                return filter_
        return None

    def add_filter(self, filter_: SocaFilter):
        if self.filters is None:
            self.filters = []
        self.filters.append(filter_)


class SocaHeader(SocaBaseModel):
    namespace: Optional[str] = Field(default=None)
    version: Optional[int] = Field(default=1)
    request_id: Optional[str] = Field(format='uuid')


class SocaAuthScope(SocaBaseModel):
    token_type: Optional[str] = Field(default=None)
    token: Optional[str] = Field(default=None)


class SocaEnvelope(SocaBaseModel):
    scope: Optional[SocaAuthScope] = Field(default=None)
    header: Optional[SocaHeader] = Field(default=None)
    success: Optional[bool] = Field(default=None)
    error_code: Optional[str] = Field(default=None)
    message: Optional[str] = Field(default=None)
    payload: Optional[Any] = Field(default=None)

    def payload_as(
        self, payload_type: Type[SocaPayloadType]
    ) -> Optional[SocaPayloadType]:
        return get_payload_as(payload=self.payload, payload_type=payload_type)

    @property
    def namespace(self) -> Optional[str]:
        if self.header is None:
            return None
        return self.header.namespace


class SocaAnyPayload(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class IdeaOpenAPISpecEntry(SocaBaseModel):
    namespace: str
    request: Type[SocaBaseModel]
    result: Type[SocaBaseModel]
    is_listing: bool
    is_public: bool
