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
from math import ceil

INSTANCE_TYPES_DEFAULT_SIZE: int = 512
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
            maxsize=INSTANCE_TYPES_DEFAULT_SIZE, ttl=INSTANCE_TYPES_TTL_SECS
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

            _ec2_paginator = (
                self._context.aws().ec2().get_paginator('describe_instance_types')
            )
            _ec2_iterator = _ec2_paginator.paginate(MaxResults=100)

            _page_num: int = 0
            for _page in _ec2_iterator:
                _page_num += 1
                _page_start: int = Utils.current_time_ms()

                # Once per page we consider increasing the cache size
                if self._cache.size() >= int(self._cache.maxsize * 0.90):
                    # Expand 15% at a time in minimum increments of 32
                    _new_size: int = 32 * round(ceil(self._cache.maxsize * 1.15) / 32)
                    self._logger.debug(
                        f'Auto-growing EC2 instance type cache size {self._cache.maxsize} -> {_new_size}'
                    )
                    self._cache.configure(maxsize=_new_size)

                _instance_types: list = Utils.get_value_as_list(
                    key='InstanceTypes', obj=_page
                )

                # Per instance grinding/update
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
                        key=_instance_name, value=EC2InstanceType(data=_instance_data)
                    )

                _page_stop: int = Utils.current_time_ms()
                self._logger.debug(
                    f'Instance Type Cache - Page #{_page_num}: Added {len(_instance_types)} to {self._cache.size()} - duration {_page_stop - _page_start}ms'
                )

            _end_ec2_data: int = Utils.current_time_ms()
            self._cache_last_refresh = Utils.current_time()
            self._logger.debug(
                f'AWS EC2 instance type cache refresh - Size: {self._cache.size()}/{self._cache.maxsize}  Duration: {_end_ec2_data - _start_ec2_data}ms.  TTL: {self._cache_refresh_interval}'
            )

    def _instance_type_names_from_botocore(self) -> List[str]:
        """
        Return all instance type names from the cache.
        This method exists primarily to be mocked during unit tests.
        """
        return list(self._cache.keys())

    def all_instance_type_names(self) -> Set[str]:
        return set(self._cache.keys())

    def get(self, instance_type: str) -> Optional[EC2InstanceType]:
        _current_time: int = Utils.current_time()
        if (_current_time - self._cache_refresh_interval) > self._cache_last_refresh:
            self._logger.debug(
                f'Refreshing EC2 Describe_Instance_Types cache... Last refresh: {self._cache_last_refresh} . Current: {_current_time}  MaxAllowed: {self._cache_refresh_interval}'
            )
            self._add_instance_data_to_cache()

        if instance_type not in self._cache:
            raise exceptions.SocaException(
                error_code=errorcodes.INVALID_EC2_INSTANCE_TYPE,
                message=f'ec2 instance_type is invalid: {instance_type}',
            )

        # If we get here it is refreshed or not real
        ec2_instance_type = self._cache.get(key=instance_type)

        if ec2_instance_type is not None:
            return ec2_instance_type
