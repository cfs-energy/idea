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

from ideasdk.protocols import SocaContextProtocol
from ideasdk.utils import Utils
from ideasdk.utils.error_redaction import redact_aws_identifiers
from ideadatamodel import exceptions, errorcodes, EC2InstanceType

from typing import Dict, Optional, Set, List
from cacheout import Cache
from threading import RLock
import botocore.exceptions

INSTANCE_TYPES_CACHE_SIZE: int = (
    2048  # minimum size. grown to 2x the region inventory when that is larger
)
INSTANCE_TYPES_TTL_SECS: int = 15 * 24 * 60 * 60  # 15 days

# EC2 instance capability cache from describe_instance_types
# This is the amount of time that an instance type could remain
# unknown to the running scheduler.
FALLBACK_INSTANCE_TYPES_REFRESH_INTERVAL = int(
    12 * 60 * 60
)  # 12-hours default. Value is in seconds

# EC2 API error codes that mean the lookup failed, not that the instance type is
# unknown. These must not be reported to the user as an invalid instance type.
TRANSIENT_EC2_ERROR_CODES = (
    'AccessDenied',
    'AccessDeniedException',
    'AuthFailure',
    'InternalError',
    'InternalFailure',
    'RequestExpired',
    'RequestLimitExceeded',
    'RequestThrottled',
    'ServiceUnavailable',
    'Throttling',
    'ThrottlingException',
    'Unavailable',
    'UnauthorizedOperation',
)


class EC2InstanceTypesDB:
    def __init__(self, context: SocaContextProtocol):
        self._context = context
        self._logger = context.logger()
        self._cache_refresh_interval = self._context.config().get_int(
            key='scheduler.cache.instance_types_refresh_interval',
            default=int(FALLBACK_INSTANCE_TYPES_REFRESH_INTERVAL),
        )
        self._cache_last_refresh = int(0)  # Force refresh
        self._cache = Cache(
            maxsize=INSTANCE_TYPES_CACHE_SIZE, ttl=INSTANCE_TYPES_TTL_SECS
        )
        self._instance_types_lock = RLock()
        self._add_instance_data_to_cache()

    def _build_cache(self, instance_types: Dict[str, EC2InstanceType]) -> Cache:
        """
        Build a fully populated cache sized to hold every entry.
        Sizing up front is what keeps a large region from evicting instance
        types that were collected from an earlier page.
        """
        _maxsize: int = max(INSTANCE_TYPES_CACHE_SIZE, len(instance_types) * 2)
        _cache = Cache(maxsize=_maxsize, ttl=INSTANCE_TYPES_TTL_SECS)
        _cache.set_many(instance_types)
        return _cache

    def _add_instance_data_to_cache(self):
        _start_ec2_data: int = Utils.current_time_ms()
        self._logger.debug('Starting EC2 instance type cache collection')

        with self._instance_types_lock:
            _instance_types_by_name: Dict[str, EC2InstanceType] = {}

            try:
                _ec2_paginator = (
                    self._context.aws().ec2().get_paginator('describe_instance_types')
                )
                _ec2_iterator = _ec2_paginator.paginate(MaxResults=100)

                _page_num: int = 0
                for _page in _ec2_iterator:
                    _page_num += 1
                    _page_start: int = Utils.current_time_ms()

                    _instance_types: list = Utils.get_value_as_list(
                        key='InstanceTypes', obj=_page
                    )

                    for _instance_data in _instance_types:
                        _instance_name: str = Utils.get_value_as_string(
                            key='InstanceType', obj=_instance_data, default=None
                        )

                        if _instance_name is None:
                            self._logger.error(
                                f'Missing InstanceType? InstanceType: {_instance_name} for: {_instance_data}'
                            )
                            raise exceptions.SocaException(
                                error_code=errorcodes.INVALID_EC2_INSTANCE_TYPE,
                                message=f'ec2 instance_type is invalid: {_instance_name}',
                            )

                        _instance_types_by_name[_instance_name] = EC2InstanceType(
                            data=_instance_data
                        )

                    _page_stop: int = Utils.current_time_ms()
                    self._logger.debug(
                        f'Instance Type Cache - Page #{_page_num}: Added {len(_instance_types)} to {len(_instance_types_by_name)} - duration {_page_stop - _page_start}ms'
                    )

                # published in a single step so concurrent readers always observe a
                # complete cache, never a cleared or half-filled one
                self._cache = self._build_cache(_instance_types_by_name)

                _end_ec2_data: int = Utils.current_time_ms()
                self._cache_last_refresh = Utils.current_time()
                self._logger.info(
                    f'EC2 instance type cache refresh completed - '
                    f'Cached {self._cache.size()} instance types in {_end_ec2_data - _start_ec2_data}ms'
                )

            except Exception as e:
                self._logger.error(
                    f'Failed to populate EC2 instance type cache completely. Cache may be incomplete. Error: {e}',
                    exc_info=True,
                )
                # Set last refresh time anyway to avoid hammering the API
                self._cache_last_refresh = Utils.current_time()
                # An already populated cache is more complete than a partial collection,
                # so it is retained. Re-raise only when nothing is available at all.
                if self._cache.size() == 0:
                    if not _instance_types_by_name:
                        raise
                    self._cache = self._build_cache(_instance_types_by_name)

    def _instance_type_names_from_botocore(self) -> List[str]:
        """
        Return all instance type names from the cache.
        This method exists primarily to be mocked during unit tests.
        """
        return list(self._cache.keys())

    def all_instance_type_names(self) -> Set[str]:
        return set(self._cache.keys())

    def _is_cache_stale(self) -> bool:
        return (
            Utils.current_time() - self._cache_refresh_interval
        ) > self._cache_last_refresh

    def _refresh_cache_if_stale(self):
        if not self._is_cache_stale():
            return

        with self._instance_types_lock:
            # re-checked under the lock so callers queued behind an in-flight
            # refresh do not each trigger another full pagination
            if not self._is_cache_stale():
                return
            self._logger.debug(
                f'Refreshing EC2 Describe_Instance_Types cache... Last refresh: {self._cache_last_refresh} . Current: {Utils.current_time()}  MaxAllowed: {self._cache_refresh_interval}'
            )
            self._add_instance_data_to_cache()

    def _fetch_single_instance_type(
        self, instance_type: str
    ) -> Optional[EC2InstanceType]:
        """
        Fetch a single instance type from AWS EC2 API.
        Returns None if the instance type doesn't exist in the region.
        Raises SocaException if the lookup itself failed, so that a transient AWS
        error is never reported back to the user as an invalid instance type.
        """
        try:
            response = (
                self._context.aws()
                .ec2()
                .describe_instance_types(InstanceTypes=[instance_type])
            )
        except botocore.exceptions.ClientError as e:
            _error_code = Utils.get_value_as_string(
                key='Code',
                obj=Utils.get_value_as_dict(key='Error', obj=e.response, default={}),
                default='',
            )
            if _error_code in TRANSIENT_EC2_ERROR_CODES:
                raise exceptions.SocaException(
                    error_code=errorcodes.GENERAL_ERROR,
                    message=f'failed to look up ec2 instance_type: {instance_type} - {redact_aws_identifiers(e)}',
                )
            self._logger.debug(
                f'EC2 rejected instance type {instance_type}: {_error_code}'
            )
            return None
        except Exception as e:
            raise exceptions.SocaException(
                error_code=errorcodes.GENERAL_ERROR,
                message=f'failed to look up ec2 instance_type: {instance_type} - {redact_aws_identifiers(e)}',
            )

        instance_types = Utils.get_value_as_list(
            key='InstanceTypes', obj=response, default=[]
        )
        if not instance_types:
            return None

        ec2_instance_type = EC2InstanceType(data=instance_types[0])
        # Cache the instance type for future use
        with self._instance_types_lock:
            self._cache.set(key=instance_type, value=ec2_instance_type)
        self._logger.info(
            f'Successfully fetched and cached instance type: {instance_type}'
        )
        return ec2_instance_type

    def get(self, instance_type: str) -> Optional[EC2InstanceType]:
        self._refresh_cache_if_stale()

        # Check cache first
        if instance_type in self._cache:
            ec2_instance_type = self._cache.get(key=instance_type)
            if ec2_instance_type is not None:
                return ec2_instance_type

        # Instance type not in cache - try to fetch it directly from AWS
        self._logger.warning(
            f'Instance type {instance_type} not found in cache. Attempting on-demand fetch from AWS API.'
        )
        ec2_instance_type = self._fetch_single_instance_type(instance_type)

        if ec2_instance_type is not None:
            return ec2_instance_type

        # Instance type truly doesn't exist or isn't available in this region
        raise exceptions.SocaException(
            error_code=errorcodes.INVALID_EC2_INSTANCE_TYPE,
            message=f'ec2 instance_type is invalid or not available in region: {instance_type}',
        )
