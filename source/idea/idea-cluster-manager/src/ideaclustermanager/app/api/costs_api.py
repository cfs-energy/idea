import ideaclustermanager

from ideasdk.api import ApiInvocationContext, BaseAPI
from ideadatamodel import exceptions, GetUserCostsSummaryRequest
from ideasdk.utils import Utils

from ideaclustermanager.app.costs.my_costs_service import MyCostsService
from ideaclustermanager.app.costs.personal_costs_store import StoredPersonalCostsService


class CostsAPI(BaseAPI):
    """
    the same measurements MyCosts serves, for every user, to administrators only. kept a
    separate class from MyCostsAPI so that a self scoped page cannot quietly become an
    open one through a single invoke() holding both authorization rules.
    """

    def __init__(self, context: ideaclustermanager.AppContext):
        self.context = context
        self.my_costs = MyCostsService(context)
        # Stored projections only: an admin listing must never start a per-user computation.
        self.monthly_costs = StoredPersonalCostsService(context)

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
            'Costs.GetUserCosts': {
                'scope': self.SCOPE_READ,
                'method': self.get_user_costs,
            },
        }

    def list_user_costs(self, context: ApiInvocationContext):
        result = self.my_costs.list_user_costs()
        storage_metrics = getattr(self.context, 'storage_metrics', None)
        configuration = (
            storage_metrics.configuration_status()
            if storage_metrics is not None
            else dict(status='disabled', reason='metrics_disabled')
        )
        storage_disabled = (
            storage_metrics is None
            or not storage_metrics.is_enabled()
            or not storage_metrics.targets()
        )
        result.storage_disabled = storage_disabled
        result.storage_configuration_status = configuration.get('status')
        result.storage_configuration_reason = configuration.get('reason')
        result.storage_metrics_provider = configuration.get('provider')
        result.storage_has_efs = configuration.get('has_efs')
        result.storage_data_available = False
        if storage_disabled:
            for row in result.listing or []:
                row.total_cost_excludes_storage = True
            result.storage_unavailable = False
            context.success(result)
            return

        storage_unavailable = False
        for row in result.listing or []:
            try:
                costs = self.monthly_costs.get_costs(row.username)
            except Exception as e:
                self.context.logger().warning(
                    f'failed to read stored costs for {row.username}: {e}'
                )
                storage_unavailable = True
                row.total_cost_excludes_storage = True
                continue
            month = costs.current
            if (
                month is None
                or month.shared_storage.status
                not in ('ready', 'partial', 'estimated_share')
                or month.shared_storage.cost is None
            ):
                row.total_cost_excludes_storage = True
                continue
            result.storage_data_available = True
            row.storage_cost = month.shared_storage.cost
            row.storage_gb = round(
                sum(item.used_bytes or 0 for item in month.storage) / (1024**3), 2
            )
            row.storage_cost_period = f'{month.start_date} through {month.end_date}'
            row.total_cost = round((row.total_cost or 0) + (row.storage_cost or 0), 2)
        result.storage_unavailable = storage_unavailable
        context.success(result)

    def get_user_summary(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetUserCostsSummaryRequest)
        if Utils.is_empty(request.username):
            raise exceptions.invalid_params('username is required')
        context.success(self.my_costs.get_summary(username=request.username))

    def get_user_costs(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetUserCostsSummaryRequest)
        if Utils.is_empty(request.username):
            raise exceptions.invalid_params('username is required')
        context.success(self.monthly_costs.get_costs(request.username))

    def invoke(self, context: ApiInvocationContext):
        acl_entry = Utils.get_value_as_dict(context.namespace, self.acl)
        if acl_entry is None:
            raise exceptions.unauthorized_access()

        acl_entry_scope = Utils.get_value_as_string('scope', acl_entry)
        if context.is_authorized(elevated_access=True, scopes=[acl_entry_scope]):
            acl_entry['method'](context)
        else:
            raise exceptions.unauthorized_access()
