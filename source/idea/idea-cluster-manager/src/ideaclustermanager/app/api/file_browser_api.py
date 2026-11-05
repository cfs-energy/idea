from ideadatamodel import exceptions
from ideasdk.filesystem.filebrowser_api import FileBrowserAPI
from ideaclustermanager.app.filesystem.storage_usage import StorageUsageService
from ideaclustermanager.app.metrics.storage_metrics_service import (
    QUOTA_MAX_AGE_SECONDS,
)

QUOTA_REFRESHED_SECONDS = 60


class ClusterFileBrowserAPI(FileBrowserAPI):
    def __init__(self, context):
        super().__init__(context)
        if getattr(context, 'storage_usage', None) is None:
            context.storage_usage = StorageUsageService(context)
        self.storage_usage = context.storage_usage

    def has_ontap_storage(self, collector) -> bool:
        if collector is not None:
            return collector.has_ontap_storage()
        try:
            shared_storage = (
                self.context.config().get_config('shared-storage', default={}) or {}
            )
            return any(
                entry.get('provider') == 'fsx_netapp_ontap'
                for entry in shared_storage.values()
                if isinstance(entry, dict) or hasattr(entry, 'get')
            )
        except (AttributeError, TypeError):
            return False

    def invoke(self, context):
        if context.namespace == 'FileBrowser.DeleteFolder':
            if not context.is_authorized_user():
                raise exceptions.unauthorized_access()
            context.success(
                self.storage_usage.delete_folder(
                    context.get_username(),
                    context.request_payload.get('path'),
                    context.request_payload.get('identity'),
                )
            )
            return
        if context.namespace != 'FileBrowser.GetStorageUsage':
            return super().invoke(context)
        if not context.is_authorized_user():
            raise exceptions.unauthorized_access()
        username = context.get_username()
        refresh = context.request_payload.get('refresh') is True
        result = self.storage_usage.get_usage(
            username, context.request_payload.get('folder'), refresh
        )
        collector = getattr(self.context, 'storage_metrics', None)
        quotas = []
        if collector is not None:
            collector.refresh_quota_reports(
                QUOTA_REFRESHED_SECONDS if refresh else QUOTA_MAX_AGE_SECONDS
            )
            quotas = collector.get_user_quotas(username)
            if quotas:
                result['quotas'] = quotas
        volume = result.pop('volume', None)
        # A qtree's rows repeat bytes the volume's own row already counts.
        used = [
            quota['used_bytes']
            for quota in quotas
            if quota.get('volume') == volume and not quota.get('qtree')
        ]
        if volume and used:
            result['outside_home_bytes'] = max(0, sum(used) - result['total']['bytes'])
        if quotas:
            unattributed = collector.unattributed_quota(quotas[0].get('volume'))
            if isinstance(unattributed, dict) and unattributed.get('bytes'):
                result['unattributed_quota_bytes'] = unattributed['bytes']
        if self.has_ontap_storage(collector):
            result['quota_status'] = 'available' if quotas else 'unavailable'
        context.success(result)
