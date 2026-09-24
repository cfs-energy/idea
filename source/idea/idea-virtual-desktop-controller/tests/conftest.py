#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ideadatamodel import (
    SocaListingPayload,
    SocaPaginator,
    VirtualDesktopBaseOS,
    VirtualDesktopServer,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
)
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.events.service.controller_queue_monitor_service import (
    ControllerQueueMonitorService,
)
from ideavirtualdesktopcontroller.app.servers.virtual_desktop_server_db import (
    VirtualDesktopServerDB,
)
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_db import (
    VirtualDesktopSSMCommandsDB,
)
from ideavirtualdesktopcontroller.app.ssm_commands.virtual_desktop_ssm_commands_utils import (
    VirtualDesktopSSMCommandsUtils,
)

from ideavirtualdesktopcontroller.app.sessions.virtual_desktop_session_utils import (
    VirtualDesktopSessionUtils,
)


@pytest.fixture(autouse=True)
def sweep_cursors_start_at_the_first_page():
    """
    the sweep resume cursors live on the class so every queue handler thread shares one
    position, which also means one test's leftover position would move the next test's
    starting page. Every test starts at the first page.
    """
    for name in (
        '_instance_profile_repair_cursor',
        '_provisioning_timeout_cursor',
        '_stopped_session_cleanup_cursor',
        '_bootstrap_refresh_cursor',
    ):
        setattr(VirtualDesktopSessionUtils, name, None)


class BootstrapRefreshTable:
    def __init__(self, key):
        self.key = key
        self.rows = {}
        self.updates = []

    def get_item(self, Key):
        row = self.rows.get(Key[self.key])
        return {} if row is None else {'Item': deepcopy(row)}

    def put_item(self, Item):
        self.rows[Item[self.key]] = deepcopy(Item)

    def delete_item(self, Key):
        self.rows.pop(Key[self.key], None)

    def update_item(self, **request):
        self.updates.append(request)
        row = self.rows.get(request['Key'][self.key])
        version = request['ExpressionAttributeValues'][':version']
        if row is None or (
            'bootstrap_refresh_version' in row
            and row['bootstrap_refresh_version'] >= version
        ):
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'UpdateItem'
            )
        row['bootstrap_refresh_version'] = version


class BootstrapRefreshSSM:
    def __init__(self):
        self.requests = []
        self.failure = None

    def send_command(self, **request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return {'Command': {'CommandId': f'command-{len(self.requests)}'}}


class BootstrapRefreshSessionDB:
    def __init__(self, server_db):
        self.server_db = server_db
        self.sessions = []
        self.cursors = []

    @staticmethod
    def cursor_for(session):
        return session.idea_session_id

    def list_all_from_db(self, request):
        self.cursors.append(request.cursor)
        start = 0
        if request.cursor is not None:
            start = next(
                index + 1
                for index, session in enumerate(self.sessions)
                if session.idea_session_id == request.cursor
            )
        page = self.sessions[start : start + 3]
        cursor = page[-1].idea_session_id if start + 3 < len(self.sessions) else None
        return SocaListingPayload(
            listing=deepcopy(page), paginator=SocaPaginator(cursor=cursor)
        )


class BootstrapRefreshHarness:
    def __init__(self):
        self.servers = BootstrapRefreshTable('instance_id')
        self.commands = BootstrapRefreshTable('command_id')
        self.ssm = BootstrapRefreshSSM()
        self.context = Mock()
        self.context.cluster_name.return_value = 'cluster'
        self.context.module_id.return_value = 'vdc'
        self.context.get_bootstrap_dir.return_value = str(
            Path(__file__).resolve().parents[2] / 'idea-bootstrap'
        )
        self.context.aws().ssm.return_value = self.ssm
        self.context.aws().dynamodb_table().Table.side_effect = lambda name: (
            self.servers if name.endswith('.servers') else self.commands
        )
        self.context.config().get_string.side_effect = lambda key, **kwargs: key
        self.server_db = VirtualDesktopServerDB(self.context)
        self.session_db = BootstrapRefreshSessionDB(self.server_db)
        self.sweep = VirtualDesktopSessionUtils.__new__(VirtualDesktopSessionUtils)
        self.sweep.context = self.context
        self.sweep._logger = self.context.logger()
        self.sweep._session_db = self.session_db
        self.command_utils = VirtualDesktopSSMCommandsUtils(
            self.context, VirtualDesktopSSMCommandsDB(self.context)
        )
        self.monitor = ControllerQueueMonitorService(self.context)

    def add_session(
        self,
        state=VirtualDesktopSessionState.READY,
        base_os=VirtualDesktopBaseOS.ROCKY9,
        version=None,
        legacy_marked=False,
    ):
        index = len(self.session_db.sessions)
        server = VirtualDesktopServer(
            instance_id=f'i-{index}',
            idea_session_id=f'session-{index}',
            idea_session_owner='owner',
            state='RUNNING',
            locked=True,
        )
        session = VirtualDesktopSession(
            idea_session_id=server.idea_session_id,
            owner='owner',
            state=state,
            base_os=base_os,
            server=server,
        )
        self.session_db.sessions.append(session)
        row = self.server_db.convert_server_object_to_db_dict(server)
        if version is not None:
            row['bootstrap_refresh_version'] = version
        if legacy_marked:
            row['ssh_kex_refreshed_at'] = '2026-09-21T12:00:00+00:00'
        self.servers.put_item(Item=row)
        return session

    def complete(self, status, command_id='command-1'):
        self.monitor._handle_ssm_commands(
            'message-1',
            {'Message': Utils.to_json({'commandId': command_id, 'status': status})},
        )


class BootstrapRefreshLoopExit:
    def __init__(self, clock, ticks):
        self.clock = clock
        self.ticks = iter(ticks)
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        try:
            self.clock.now = next(self.ticks)
        except StopIteration:
            self.stopped = True


@pytest.fixture
def refresh():
    return BootstrapRefreshHarness()


@pytest.fixture
def refresh_loop(monkeypatch, refresh):
    from ideavirtualdesktopcontroller.app import (
        virtual_desktop_controller_app as app_module,
    )

    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(app_module.time, 'monotonic', lambda: clock.now)
    app = app_module.VirtualDesktopControllerApp.__new__(
        app_module.VirtualDesktopControllerApp
    )
    app.context = refresh.context
    app._bootstrap_session_utils = Mock()
    app._bootstrap_exit = BootstrapRefreshLoopExit(
        clock, [60, 21599, 21600, 21660, 43200]
    )
    return app, clock


@pytest.fixture
def refresh_app(monkeypatch, refresh_loop):
    from ideavirtualdesktopcontroller.app import (
        virtual_desktop_controller_app as app_module,
    )

    app, _ = refresh_loop
    monkeypatch.setattr(
        app_module, 'preferred_subnet_pin_warning', lambda context: None
    )
    app._bootstrap_thread = Mock()
    app._bootstrap_exit = Mock()
    return app
