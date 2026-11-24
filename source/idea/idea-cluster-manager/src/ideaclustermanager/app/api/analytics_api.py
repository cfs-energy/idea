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
from ideasdk.auth import ApiAuthorizationType
from ideadatamodel.analytics import OpenSearchQueryRequest, OpenSearchQueryResult
from ideadatamodel import constants, exceptions
from ideasdk.utils import Utils


class AnalyticsAPI(BaseAPI):
    """
    Analytics API to query opensearch cluster.

    this is a stop-gap API and ideally, each module should implement their own version of Analytics API,
    scoped to the indices or aliases exposed by a particular module.

    any write or index settings apis must not be exposed via this class.
    an AnalyticsAdminAPI should be exposed with elevated access to enable such functionality.

    the admin desktop overview is its only caller, so a query reads only the desktop
    session alias, and only administrators, managers and desktop module administrators
    may send one
    """

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context

    def opensearch_query(self, context: ApiInvocationContext):
        """
        Send Raw ElasticSearch Query and Search Request
        """
        request = context.get_request_payload_as(OpenSearchQueryRequest)
        if Utils.is_empty(request.data):
            raise exceptions.invalid_params('data is required')
        alias = self.context.config().get_string(
            f'{constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER}.opensearch.dcv_session.alias'
        )
        if Utils.is_empty(alias) or request.data.get('index') != alias:
            raise exceptions.unauthorized_access()

        result = self.context.analytics_service().os_client.os_client.search(
            **request.data
        )

        context.success(OpenSearchQueryResult(data=result))

    def desktop_administrator(self, context: ApiInvocationContext) -> bool:
        authorization = context.get_authorization()
        if authorization.type != ApiAuthorizationType.USER:
            return False
        config = self.context.config()
        module = constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER
        if not config.is_module_enabled(module):
            return False
        group = self.context.accounts.group_name_helper.get_module_administrators_group(
            module_id=config.get_module_id(module)
        )
        return group in (authorization.groups or [])

    def invoke(self, context: ApiInvocationContext):
        if not (
            context.is_authorized(elevated_access=True)
            or self.desktop_administrator(context)
        ):
            raise exceptions.unauthorized_access()

        namespace = context.namespace
        if namespace == 'Analytics.OpenSearchQuery':
            self.opensearch_query(context)
