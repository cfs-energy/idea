import ideaclustermanager

from ideasdk.api import ApiInvocationContext, BaseAPI
from ideadatamodel import exceptions, ReportingPeriodRequest
from pydantic import ValidationError
from ideaclustermanager.app.reporting.insights_service import InsightsService

from ideaclustermanager.app.costs.personal_costs_store import StoredPersonalCostsService


class MyCostsAPI(BaseAPI):
    """
    what the cluster recorded against the caller. every method is scoped to the token's
    own username and the request model carries no username field, so no shape of this
    request asks about somebody else. an administrator uses the admin pages instead.
    """

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.insights = InsightsService(context)
        self.my_costs = StoredPersonalCostsService(context)
        self.monthly_costs = StoredPersonalCostsService(context)

    def get_summary(self, context: ApiInvocationContext):
        context.success(self.my_costs.get_summary(username=context.get_username()))

    def invoke(self, context: ApiInvocationContext):
        # authorized, not merely authenticated: removing a user from the module
        # group has to take the page away with them.
        if not context.is_authorized_user():
            raise exceptions.unauthorized_access()

        if context.namespace == 'MyCosts.GetInsights':

            def authorize():
                return (
                    context.is_authorized_user()
                    and context.has_access_token()
                    and context.is_authenticated_user()
                    and not context.is_unix_domain_socket_invocation()
                    and bool(context.get_username())
                )

            if not authorize():
                raise exceptions.unauthorized_access()
            if any(
                key in context.header
                for key in (
                    'actor',
                    'username',
                    'cluster',
                    'cluster_name',
                    'actor_id',
                    'cluster_id',
                )
            ):
                raise exceptions.invalid_params(
                    'Reporting identity comes from authentication'
                )
            try:
                request = ReportingPeriodRequest.model_validate(context.request_payload)
            except ValidationError:
                raise exceptions.invalid_params('Invalid reporting request') from None
            result = self.insights.get_insights(
                request, authorize, username=context.get_username()
            )
            payload = result.model_dump(mode='json', exclude_none=False)
            for section in ('jobs', 'desktops', 'storage'):
                payload[section].pop('by_user', None)
            context.success(payload)
        elif context.namespace == 'MyCosts.GetCosts':
            context.success(self.monthly_costs.get_costs(context.get_username()))
        elif context.namespace == 'MyCosts.GetCostTicker':
            context.success(self.monthly_costs.get_ticker(context.get_username()))
        elif context.namespace == 'MyCosts.Refresh':
            context.success(self.monthly_costs.refresh(context.get_username()))
        elif context.namespace == 'MyCosts.GetSummary':
            self.get_summary(context)
        else:
            raise exceptions.unauthorized_access()
