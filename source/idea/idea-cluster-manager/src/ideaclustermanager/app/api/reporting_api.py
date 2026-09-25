"""Authenticated, capability-gated reporting reads."""

from pydantic import ValidationError

from ideasdk.api import BaseAPI
from ideadatamodel import (
    GetReportingCapabilitiesRequest,
    GetReportingCapabilitiesResult,
    ReportingPeriodRequest,
    ListReportingRowsRequest,
    ExportReportingCsvRequest,
    exceptions,
)
from ideaclustermanager.app.reporting.reporting_service import ReportingService


class ReportingAPI(BaseAPI):
    def __init__(self, context):
        self.context = context
        self.service = ReportingService(context)
        self.service.store.initialize()

    @staticmethod
    def can_read(context):
        predicate = getattr(context, 'can_read_reporting', None)
        return bool(predicate and predicate())

    def invoke(self, context):
        methods = {
            'Reporting.GetCapabilities': GetReportingCapabilitiesRequest,
            'Reporting.GetSummary': ReportingPeriodRequest,
            'Reporting.ListRows': ListReportingRowsRequest,
            'Reporting.ExportCsv': ExportReportingCsvRequest,
        }
        if context.namespace not in methods:
            raise exceptions.unauthorized_access()
        if (
            not context.has_access_token()
            or context.is_unix_domain_socket_invocation()
            or not context.is_authenticated_user()
            or not context.get_username()
        ):
            raise exceptions.unauthorized_access()
        if context.namespace != 'Reporting.GetCapabilities' and not self.can_read(
            context
        ):
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
            request = methods[context.namespace].model_validate(context.request_payload)
        except ValidationError:
            raise exceptions.invalid_params('Invalid reporting request') from None
        actor = context.get_username()

        def authorize():
            return self.can_read(context)

        if context.namespace == 'Reporting.GetCapabilities':
            result = GetReportingCapabilitiesResult(can_read_reporting=authorize())
        elif context.namespace == 'Reporting.GetSummary':
            result = self.service.get_summary(actor, request, authorize)
        elif context.namespace == 'Reporting.ListRows':
            result = self.service.list_rows(actor, request, authorize)
        else:
            result = self.service.export(actor, request, authorize)
        context.success(result.model_dump(mode='json', exclude_none=False))
