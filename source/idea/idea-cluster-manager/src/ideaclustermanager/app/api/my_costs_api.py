import ideaclustermanager

from ideasdk.api import ApiInvocationContext, BaseAPI
from ideadatamodel import exceptions

from ideaclustermanager.app.costs.personal_costs_store import StoredPersonalCostsService


class MyCostsAPI(BaseAPI):
    """
    what the cluster recorded against the caller. every method is scoped to the token's
    own username and the request model carries no username field, so no shape of this
    request asks about somebody else. an administrator uses the admin pages instead.
    """

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.my_costs = StoredPersonalCostsService(context)
        self.monthly_costs = StoredPersonalCostsService(context)

    def get_summary(self, context: ApiInvocationContext):
        context.success(self.my_costs.get_summary(username=context.get_username()))

    def invoke(self, context: ApiInvocationContext):
        # authorized, not merely authenticated: removing a user from the module
        # group has to take the page away with them.
        if not context.is_authorized_user():
            raise exceptions.unauthorized_access()

        if context.namespace == 'MyCosts.GetCosts':
            context.success(self.monthly_costs.get_costs(context.get_username()))
        elif context.namespace == 'MyCosts.GetCostTicker':
            context.success(self.monthly_costs.get_ticker(context.get_username()))
        elif context.namespace == 'MyCosts.Refresh':
            context.success(self.monthly_costs.refresh(context.get_username()))
        elif context.namespace == 'MyCosts.GetSummary':
            self.get_summary(context)
        else:
            raise exceptions.unauthorized_access()
