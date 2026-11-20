import pathlib
import re
from types import SimpleNamespace

import ideavirtualdesktopcontroller.app.api.virtual_desktop_api_invoker as api_invoker

NAMESPACE = re.compile(r"'([A-Z][A-Za-z]+\.[A-Z][A-Za-z]+)'")
HANDLER_MAP = re.compile(
    r'self\.namespace_handler_map[^=]*=\s*\{(.*?)\n\s*\}', re.DOTALL
)


def test_every_dispatched_namespace_is_in_a_handler_map():
    # the server's registered set is read from these maps at start, so a namespace
    # dispatched any other way would only ever report as the shared 'unknown' series
    folder = pathlib.Path(api_invoker.__file__).parent
    dispatched, mapped = set(), set()
    for source in folder.glob('*.py'):
        if source.name == pathlib.Path(api_invoker.__file__).name:
            continue
        text = source.read_text()
        dispatched |= set(NAMESPACE.findall(text))
        for block in HANDLER_MAP.findall(text):
            mapped |= set(NAMESPACE.findall(block))
    assert 'VirtualDesktopAdmin.RefreshImages' in mapped
    assert dispatched <= mapped


def test_the_registered_set_is_every_handler_maps_keys():
    invoker = api_invoker.VirtualDesktopApiInvoker.__new__(
        api_invoker.VirtualDesktopApiInvoker
    )
    invoker.INVOKER_MAP = {
        'VirtualDesktopAdmin': SimpleNamespace(
            namespace_handler_map={'VirtualDesktopAdmin.MoveSessionToProject': None}
        ),
        'VirtualDesktop': SimpleNamespace(
            namespace_handler_map={'VirtualDesktop.ListSessions': None}
        ),
        'App': SimpleNamespace(NAMESPACES=frozenset({'App.GetModuleInfo'})),
    }
    assert invoker.get_namespaces() == {
        'VirtualDesktopAdmin.MoveSessionToProject',
        'VirtualDesktop.ListSessions',
        'App.GetModuleInfo',
    }
