import pathlib
import re

import ideascheduler.app.api.scheduler_api_invoker as api_invoker

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
    assert 'SchedulerAdmin.ListQueueProfiles' in dispatched
    assert dispatched <= api_invoker.SchedulerApiInvoker.NAMESPACES


def test_the_openpbs_hook_events_sent_to_the_server_are_registered():
    hooks = (
        pathlib.Path(api_invoker.__file__).parents[4]
        / 'resources/openpbs/hooks/openpbs_hook_handler.py'
    )
    events = set()
    for line in re.findall(r"create hook \w+ event='([a-z_,]+)'", hooks.read_text()):
        events |= set(line.split(','))
    over_the_socket = {e for e in events if not e.startswith('execjob_')}
    assert over_the_socket == {'queuejob', 'modifyjob', 'movejob', 'runjob'}
    assert {
        f'OpenPBSHook.{e}' for e in over_the_socket
    } <= api_invoker.SchedulerApiInvoker.NAMESPACES
