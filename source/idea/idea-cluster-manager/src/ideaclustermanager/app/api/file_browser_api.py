from ideadatamodel import exceptions
from ideasdk.filesystem.filebrowser_api import FileBrowserAPI
from ideaclustermanager.app.filesystem.storage_usage import StorageUsageService


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
        result = self.storage_usage.get_usage(
            username, context.request_payload.get('folder')
        )
        collector = getattr(self.context, 'storage_metrics', None)
        quotas = []
        if collector is not None:
            quotas = collector.get_user_quotas(username)
            if quotas:
                result['quotas'] = quotas
        if self.has_ontap_storage(collector):
            result['quota_status'] = 'available' if quotas else 'unavailable'
        context.success(result)
