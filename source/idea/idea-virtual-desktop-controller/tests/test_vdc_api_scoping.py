"""
Test Cases for app (client-credentials) token scoping on the VDC admin and DCV APIs.

`is_authorized(elevated_access=True)` with no scopes denies an app token outright,
whatever scope it carries. Both invoke gates now pass the namespace's module scope,
so an integration holding vdc/read or vdc/write is admitted for the namespaces that
have no dependency on the caller's own username, and elevated humans are unaffected.
"""

import pytest

from ideadatamodel import exceptions, errorcodes
from ideavirtualdesktopcontroller.app.api.virtual_desktop_admin_api import (
    APP_TOKEN_NAMESPACE_ACCESS,
    VirtualDesktopAdminAPI,
)
from ideavirtualdesktopcontroller.app.api.virtual_desktop_dcv_api import (
    VirtualDesktopDCVAPI,
)

MODULE_ID = 'vdc'

# namespaces deliberately left denying app tokens: the first three resolve or record the
# caller's own username, which an app token does not carry, and the re-index pair is
# operator maintenance with no machine-to-machine caller.
ELEVATED_ONLY_NAMESPACES = [
    'VirtualDesktopAdmin.GetSessionConnectionInfo',
    'VirtualDesktopAdmin.GetSessionScreenshot',
    'VirtualDesktopAdmin.SetSessionCleanupExemption',
    'VirtualDesktopAdmin.ReIndexUserSessions',
    'VirtualDesktopAdmin.ReIndexSoftwareStacks',
]


class FakeAppContext:
    def module_id(self) -> str:
        return MODULE_ID


class FakeApiInvocationContext:
    """user and app authorization are mutually exclusive: an app token is never elevated"""

    def __init__(self, namespace: str, elevated: bool = False, app_scopes=None):
        self.namespace = namespace
        self._elevated = elevated
        self._app_scopes = app_scopes

    def is_authorized(self, elevated_access: bool, scopes=None) -> bool:
        if self._app_scopes is not None:
            return bool(scopes) and all(s in self._app_scopes for s in scopes)
        return elevated_access and self._elevated


def build_api(api_class, namespaces):
    # the invoke gate reads only context.module_id() and the handler map; the real
    # constructor builds db and broker clients the authorization check never touches
    api = object.__new__(api_class)
    api.context = FakeAppContext()
    api.handled = []
    api.namespace_handler_map = {
        namespace: api.handled.append for namespace in namespaces
    }
    return api


def build_admin_api():
    return build_api(
        VirtualDesktopAdminAPI,
        list(APP_TOKEN_NAMESPACE_ACCESS) + ELEVATED_ONLY_NAMESPACES,
    )


def build_dcv_api():
    return build_api(
        VirtualDesktopDCVAPI,
        ['VirtualDesktopDCV.DescribeServers', 'VirtualDesktopDCV.DescribeSessions'],
    )


def assert_denied(api, invocation):
    with pytest.raises(exceptions.SocaException) as exc_info:
        api.invoke(invocation)
    assert exc_info.value.error_code == errorcodes.UNAUTHORIZED_ACCESS
    assert api.handled == []


# ------------------------------------------------------------------ admin api


@pytest.mark.parametrize(
    'namespace,access',
    sorted(APP_TOKEN_NAMESPACE_ACCESS.items()),
)
def test_admin_app_token_with_the_namespaces_module_scope_is_served(namespace, access):
    api = build_admin_api()
    invocation = FakeApiInvocationContext(
        namespace=namespace, app_scopes=[f'{MODULE_ID}/{access}']
    )

    api.invoke(invocation)

    assert api.handled == [invocation]


@pytest.mark.parametrize('namespace', sorted(APP_TOKEN_NAMESPACE_ACCESS))
def test_admin_app_token_with_another_modules_scope_is_denied(namespace):
    api = build_admin_api()
    assert_denied(
        api,
        FakeApiInvocationContext(
            namespace=namespace, app_scopes=['cluster-manager/write']
        ),
    )


@pytest.mark.parametrize('namespace', sorted(APP_TOKEN_NAMESPACE_ACCESS))
def test_admin_elevated_user_is_still_served(namespace):
    api = build_admin_api()
    invocation = FakeApiInvocationContext(namespace=namespace, elevated=True)

    api.invoke(invocation)

    assert api.handled == [invocation]


@pytest.mark.parametrize('namespace', sorted(APP_TOKEN_NAMESPACE_ACCESS))
def test_admin_non_elevated_user_is_denied(namespace):
    api = build_admin_api()
    assert_denied(api, FakeApiInvocationContext(namespace=namespace, elevated=False))


def test_admin_read_scope_does_not_admit_a_mutation():
    """a read-only integration must not create sessions or software stacks"""
    api = build_admin_api()
    mutations = [
        namespace
        for namespace, access in APP_TOKEN_NAMESPACE_ACCESS.items()
        if access == 'write'
    ]
    assert mutations, 'expected the map to carry write namespaces'

    for namespace in mutations:
        api = build_admin_api()
        assert_denied(
            api,
            FakeApiInvocationContext(
                namespace=namespace, app_scopes=[f'{MODULE_ID}/read']
            ),
        )


@pytest.mark.parametrize('namespace', ELEVATED_ONLY_NAMESPACES)
def test_admin_unmapped_namespace_still_denies_every_app_token(namespace):
    """
    these resolve the caller's own username or are index maintenance: the write scope
    is the broadest an app token can hold, and it must not open them
    """
    assert namespace not in APP_TOKEN_NAMESPACE_ACCESS

    api = build_admin_api()
    assert_denied(
        api,
        FakeApiInvocationContext(
            namespace=namespace,
            app_scopes=[f'{MODULE_ID}/read', f'{MODULE_ID}/write'],
        ),
    )


@pytest.mark.parametrize('namespace', ELEVATED_ONLY_NAMESPACES)
def test_admin_unmapped_namespace_still_serves_an_elevated_user(namespace):
    api = build_admin_api()
    invocation = FakeApiInvocationContext(namespace=namespace, elevated=True)

    api.invoke(invocation)

    assert api.handled == [invocation]


# -------------------------------------------------------------------- dcv api


@pytest.mark.parametrize(
    'namespace',
    ['VirtualDesktopDCV.DescribeServers', 'VirtualDesktopDCV.DescribeSessions'],
)
def test_dcv_app_token_with_the_module_read_scope_is_served(namespace):
    api = build_dcv_api()
    invocation = FakeApiInvocationContext(
        namespace=namespace, app_scopes=[f'{MODULE_ID}/read']
    )

    api.invoke(invocation)

    assert api.handled == [invocation]


def test_dcv_app_token_with_another_modules_scope_is_denied():
    api = build_dcv_api()
    assert_denied(
        api,
        FakeApiInvocationContext(
            namespace='VirtualDesktopDCV.DescribeSessions',
            app_scopes=['scheduler/read'],
        ),
    )


def test_dcv_elevated_user_is_still_served():
    api = build_dcv_api()
    invocation = FakeApiInvocationContext(
        namespace='VirtualDesktopDCV.DescribeServers', elevated=True
    )

    api.invoke(invocation)

    assert api.handled == [invocation]


def test_dcv_non_elevated_user_is_denied():
    api = build_dcv_api()
    assert_denied(
        api,
        FakeApiInvocationContext(
            namespace='VirtualDesktopDCV.DescribeServers', elevated=False
        ),
    )
