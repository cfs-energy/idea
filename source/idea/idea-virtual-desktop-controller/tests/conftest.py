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
import pytest

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
    ):
        setattr(VirtualDesktopSessionUtils, name, None)
