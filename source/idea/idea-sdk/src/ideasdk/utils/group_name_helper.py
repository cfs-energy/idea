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
from ideadatamodel import constants, exceptions

from typing import Iterable

IDEA_GROUP_NAME_PREFIX = 'idea'
DEFAULT_OPERATIONS_LEADS_GROUP_NAME = 'operations-leads-cluster-group'


class GroupNameHelper:
    """
    Helper class to standardize all group names across cluster and modules
    """

    def __init__(self, context: SocaContextProtocol):
        self.context = context

    @staticmethod
    def _build_group_name(group_type: str, name: str) -> str:
        return f'{name}-{group_type}-group'

    def get_cluster_administrators_group(self) -> str:
        group_name = self.context.config().get_string(
            'identity-provider.cognito.administrators_group_name', required=True
        )
        if group_name.endswith(f'{constants.GROUP_TYPE_CLUSTER}-group'):
            return group_name
        return self._build_group_name(
            group_type=constants.GROUP_TYPE_CLUSTER, name=group_name
        )

    def get_cluster_managers_group(self) -> str:
        group_name = self.context.config().get_string(
            'identity-provider.cognito.managers_group_name', required=True
        )
        if group_name.endswith(f'{constants.GROUP_TYPE_CLUSTER}-group'):
            return group_name
        return self._build_group_name(
            group_type=constants.GROUP_TYPE_CLUSTER, name=group_name
        )

    @staticmethod
    def validate_operations_leads_group_name(
        group_name: str,
        administrators_group_name: str,
        managers_group_name: str,
        module_ids: Iterable[str] = (),
    ) -> str:
        """Normalize the reporting group and reject privileged group collisions."""
        if not isinstance(group_name, str) or not group_name.strip():
            raise exceptions.invalid_params('operations leads group name is required')
        names = [
            group_name,
            administrators_group_name,
            managers_group_name,
            *(f'{module_id}-administrators-module-group' for module_id in module_ids),
        ]
        normalized = [
            name
            if name.endswith(f'{constants.GROUP_TYPE_CLUSTER}-group')
            else GroupNameHelper._build_group_name(constants.GROUP_TYPE_CLUSTER, name)
            for name in names
        ]
        if normalized[0] in normalized[1:]:
            raise exceptions.invalid_params(
                'operations leads group must not match a privileged group'
            )
        return normalized[0]

    def get_cluster_operations_leads_group(self) -> str:
        return self.validate_operations_leads_group_name(
            group_name=self.context.config().get_string(
                'identity-provider.cognito.operations_leads_group_name',
                default=DEFAULT_OPERATIONS_LEADS_GROUP_NAME,
            ),
            administrators_group_name=self.get_cluster_administrators_group(),
            managers_group_name=self.get_cluster_managers_group(),
            module_ids=(
                module['module_id'] for module in self.context.get_cluster_modules()
            ),
        )

    def get_module_administrators_group(self, module_id: str) -> str:
        return self._build_group_name(
            group_type=constants.GROUP_TYPE_MODULE, name=f'{module_id}-administrators'
        )

    def get_module_users_group(self, module_id: str) -> str:
        return self._build_group_name(
            group_type=constants.GROUP_TYPE_MODULE, name=f'{module_id}-users'
        )

    def get_project_group(self, project_code: str) -> str:
        return self._build_group_name(
            group_type=constants.GROUP_TYPE_PROJECT, name=project_code
        )

    def get_default_project_group(self) -> str:
        return self.get_project_group(constants.DEFAULT_PROJECT)

    def get_user_group(self, username: str) -> str:
        return self._build_group_name(
            group_type=constants.GROUP_TYPE_USER, name=username
        )
