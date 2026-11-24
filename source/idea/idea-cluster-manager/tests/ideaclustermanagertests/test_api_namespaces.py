import pathlib
import re

import ideaclustermanager.app.api.api_invoker as api_invoker

NAMESPACE = re.compile(r"'([A-Z][A-Za-z]+\.[A-Z][A-Za-z]+)'")


def test_every_dispatched_namespace_keeps_its_own_metric_series():
    # every name this service's handlers dispatch on, read from their source, is in the
    # table the server uses: a missing one would only report as the shared 'unknown' series.
    # The SDK's handlers (FileBrowser, App) keep their own tables, checked in the SDK.
    folder = pathlib.Path(api_invoker.__file__).parent
    dispatched = set()
    for source in folder.glob('*.py'):
        if source.name != pathlib.Path(api_invoker.__file__).name:
            dispatched |= set(NAMESPACE.findall(source.read_text()))
    assert 'FileBrowser.GetStorageUsage' in dispatched
    assert dispatched <= api_invoker.ClusterManagerApiInvoker.NAMESPACES
