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
from ideadatamodel import exceptions, errorcodes, EC2InstanceType

from typing import Optional, Set, List
from cacheout import Cache
from threading import RLock

INSTANCE_TYPES_CACHE_SIZE: int = (
    2048  # AWS has 700-800+ instance types; use 2x for headroom
)
INSTANCE_TYPES_TTL_SECS: int = 15 * 24 * 60 * 60  # 15 days

# EC2 instance capability cache from describe_instance_types
# This is the amount of time that an instance type could remain
# unknown to the running scheduler.
FALLBACK_INSTANCE_TYPES_REFRESH_INTERVAL = int(
    12 * 60 * 60
)  # 12-hours default. Value is in seconds


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

    def _add_instance_data_to_cache(self):
        _start_ec2_data: int = Utils.current_time_ms()
        self._logger.debug('Starting EC2 instance type cache collection')

        with self._instance_types_lock:
            if self._cache.size():
                self._logger.info(
                    f'Emptying EC2 instance type cache: {self._cache.size()}'
                )
                self._cache.clear()

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

                        self._cache.set(
                            key=_instance_name,
                            value=EC2InstanceType(data=_instance_data),
                        )

                    _page_stop: int = Utils.current_time_ms()
                    self._logger.debug(
                        f'Instance Type Cache - Page #{_page_num}: Added {len(_instance_types)} to {self._cache.size()} - duration {_page_stop - _page_start}ms'
                    )

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
                # Re-raise if cache is empty - this is a critical failure
                if self._cache.size() == 0:
                    raise

    def _instance_type_names_from_botocore(self) -> List[str]:
        """
        Return all instance type names from the cache.
        This method exists primarily to be mocked during unit tests.
        """
        return list(self._cache.keys())

    def all_instance_type_names(self) -> Set[str]:
        return set(self._cache.keys())

    def _fetch_single_instance_type(
        self, instance_type: str
    ) -> Optional[EC2InstanceType]:
        """
        Fetch a single instance type from AWS EC2 API.
        Returns None if the instance type doesn't exist in the region.
        """
        try:
            response = (
                self._context.aws()
                .ec2()
                .describe_instance_types(InstanceTypes=[instance_type])
            )
            instance_types = Utils.get_value_as_list(
                key='InstanceTypes', obj=response, default=[]
            )
            if instance_types:
                instance_data = instance_types[0]
                ec2_instance_type = EC2InstanceType(data=instance_data)
                # Cache the instance type for future use
                with self._instance_types_lock:
                    self._cache.set(key=instance_type, value=ec2_instance_type)
                self._logger.info(
                    f'Successfully fetched and cached instance type: {instance_type}'
                )
                return ec2_instance_type
            return None
        except Exception as e:
            self._logger.debug(
                f'Failed to fetch instance type {instance_type} from AWS: {e}'
            )
            return None

    def get(self, instance_type: str) -> Optional[EC2InstanceType]:
        _current_time: int = Utils.current_time()
        if (_current_time - self._cache_refresh_interval) > self._cache_last_refresh:
            self._logger.debug(
                f'Refreshing EC2 Describe_Instance_Types cache... Last refresh: {self._cache_last_refresh} . Current: {_current_time}  MaxAllowed: {self._cache_refresh_interval}'
            )
            self._add_instance_data_to_cache()

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
