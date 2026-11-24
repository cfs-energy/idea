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

from ideadatamodel import errorcodes, exceptions
from ideadatamodel.model_utils import ModelUtils as Utils

from pyhocon import ConfigTree, tool, ConfigException, ConfigFactory
from typing import Optional, List, Dict, Any
import copy


def logged_value(key: Any, value: Any) -> Any:
    """
    what a log line shows for a setting: the value, except the storage usage snapshot, which
    lists every user with their bytes and is shown as counts and a total. every place that
    logs a setting's value goes through here: the write, the stream of changes each module
    follows, and the settings a module prints when it starts.
    """
    if str(key).rsplit('.', 1)[-1] != 'usage_snapshot' or value is None:
        return value
    try:
        snapshots = list(Utils.from_json(value).values())
        users = sum(len(snapshot['users']) for snapshot in snapshots)
        total = sum(snapshot['total_bytes'] for snapshot in snapshots)
        return f'<{len(snapshots)} file systems, {users} users, {total} bytes>'
    except Exception:
        return f'<{len(str(value))} characters>'


def is_null_value(value: Any) -> bool:
    """
    an empty list is a real config value: `key: []` is not `key: ~`, and must round-trip
    through the cluster settings table as a list. every other empty value counts as null.
    """
    return Utils.is_empty(value) and not isinstance(value, list)


class SocaConfig:
    def __init__(self, config: Dict):
        self._config = ConfigFactory.from_dict(config)

    def pop(self, key, default=None, required=False):
        if required:
            self._config.pop(key)
        else:
            self._config.pop(key, default=default)

    def put(self, key, value):
        if is_null_value(value):
            value = None
        self._config.put(key, value)

    @staticmethod
    def handle_exception(e: Exception, key: str):
        if isinstance(e, KeyError):
            raise exceptions.soca_exception(
                error_code=errorcodes.CONFIG_KEY_NOT_FOUND,
                message=f'{str(e)}, key: {key}',
            )
        elif isinstance(e, ConfigException):
            raise exceptions.soca_exception(
                error_code=errorcodes.CONFIG_TYPE_ERROR, message=str(e)
            )
        else:
            raise e

    def get(self, key, default=None, required=False, **kwargs) -> Optional[Any]:
        try:
            if required:
                value = self._config.get(key)
            else:
                value = self._config.get(key, default=default)

            if is_null_value(value):
                return default
            return value

        except Exception as e:
            self.handle_exception(e, key)

    def get_string(self, key, default=None, required=False, **kwargs) -> Optional[str]:
        try:
            if required:
                value = self._config.get_string(key)
            else:
                value = self._config.get_string(key, default=default)

            if Utils.is_empty(value):
                return default
            return value

        except Exception as e:
            self.handle_exception(e, key)

    def get_int(self, key, default=None, required=False, **kwargs) -> Optional[int]:
        try:
            if required:
                value = self._config.get_int(key)
            else:
                value = self._config.get_int(key, default=default)

            if Utils.is_empty(value):
                return default
            return value
        except Exception as e:
            self.handle_exception(e, key)

    def get_float(self, key, default=None, required=False, **kwargs) -> Optional[float]:
        try:
            if required:
                value = self._config.get_float(key)
            else:
                value = self._config.get_float(key, default=default)

            if Utils.is_empty(value):
                return default
            return value

        except Exception as e:
            self.handle_exception(e, key)

    def get_bool(self, key, default=None, required=False, **kwargs) -> Optional[bool]:
        try:
            if required:
                value = self._config.get_bool(key)
            else:
                value = self._config.get_bool(key, default=default)

            if Utils.is_empty(value):
                return default
            return value

        except Exception as e:
            self.handle_exception(e, key)

    def get_list(self, key, default=None, required=False, **kwargs) -> Optional[List]:
        try:
            if required:
                value = self._config.get_list(key)
            else:
                value = self._config.get_list(key, default=default)

            if is_null_value(value):
                return default
            return value

        except Exception as e:
            self.handle_exception(e, key)

    def get_config(
        self, key, default=None, required=False, **kwargs
    ) -> Optional[ConfigTree]:
        try:
            if required:
                value = self._config.get_config(key)
            else:
                value = self._config.get_config(key, default=default)

            if Utils.is_empty(value):
                return default

            return value

        except Exception as e:
            self.handle_exception(e, key)

    def get_secret(self, key, default=None, required=False, **kwargs) -> Optional[str]:
        raise exceptions.not_supported(
            'get_secret() not supported on config base class'
        )

    def get_cluster_external_endpoint(self) -> str:
        raise exceptions.not_supported(
            'get_cluster_external_endpoint() not supported on config base class'
        )

    def get_cluster_internal_endpoint(self) -> str:
        raise exceptions.not_supported(
            'get_cluster_internal_endpoint() not supported on config base class'
        )

    def get_module_id(self, module_name: str) -> str:
        raise exceptions.not_supported(
            'get_module_id() not supported on config base class'
        )

    def is_module_enabled(self, module_name: str) -> bool:
        raise exceptions.not_supported(
            'is_module_enabled() not supported on config base class'
        )

    def as_yaml(self):
        return tool.HOCONConverter().to_yaml(self._config)

    def as_logged_yaml(self):
        def logged(tree):
            for key, value in tree.items():
                if isinstance(value, dict):
                    logged(value)
                else:
                    tree[key] = logged_value(key, value)

        config = copy.deepcopy(self._config)
        logged(config)
        return tool.HOCONConverter().to_yaml(config)

    def as_json(self):
        return tool.HOCONConverter().to_json(self._config)

    def as_properties(self):
        return tool.HOCONConverter().to_properties(self._config)

    def as_dict(self):
        return self._config.as_plain_ordered_dict()

    def as_hocon(self):
        return tool.HOCONConverter().to_hocon(self._config)

    def raw(self) -> ConfigTree:
        return self._config
