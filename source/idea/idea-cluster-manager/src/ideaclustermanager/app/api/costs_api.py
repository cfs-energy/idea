import ideaclustermanager

from ideasdk.api import ApiInvocationContext, BaseAPI
from ideadatamodel import exceptions, GetUserCostsSummaryRequest
from ideasdk.utils import Utils

from ideaclustermanager.app.costs.my_costs_service import MyCostsService


class CostsAPI(BaseAPI):
    """
    the same measurements MyCosts serves, for every user, to administrators only. kept a
    separate class from MyCostsAPI so that a self scoped page cannot quietly become an
    open one through a single invoke() holding both authorization rules.
    """

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.my_costs = MyCostsService(context)

        self.SCOPE_READ = f'{self.context.module_id()}/read'

        self.acl = {
            # an application token holding cluster-manager/read may list this,
            # which is what reporting integrations use.
            'Costs.ListUserCosts': {
                'scope': self.SCOPE_READ,
                'method': self.list_user_costs,
            },
            'Costs.GetUserSummary': {
                'scope': self.SCOPE_READ,
                'method': self.get_user_summary,
            },
        }

    def list_user_costs(self, context: ApiInvocationContext):
        context.success(self.my_costs.list_user_costs())

    def get_user_summary(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetUserCostsSummaryRequest)
        if Utils.is_empty(request.username):
            raise exceptions.invalid_params('username is required')
        context.success(self.my_costs.get_summary(username=request.username))

    def invoke(self, context: ApiInvocationContext):
        acl_entry = Utils.get_value_as_dict(context.namespace, self.acl)
        if acl_entry is None:
            raise exceptions.unauthorized_access()

        acl_entry_scope = Utils.get_value_as_string('scope', acl_entry)
        if context.is_authorized(elevated_access=True, scopes=[acl_entry_scope]):
            acl_entry['method'](context)
        else:
            raise exceptions.unauthorized_access()
