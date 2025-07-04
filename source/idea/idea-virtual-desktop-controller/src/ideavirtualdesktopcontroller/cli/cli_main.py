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

import ideavirtualdesktopcontroller

from ideadatamodel.constants import CLICK_SETTINGS

from ideavirtualdesktopcontroller.cli.logs import logs

import sys
import click

from ideavirtualdesktopcontroller.cli.sessions import (
    reindex_user_sessions,
    batch_create_sessions,
    cleanup_orphaned_schedules,
    terminate_sessions,
)
from ideavirtualdesktopcontroller.cli.software_stacks import (
    reindex_software_stacks,
    merge_software_stacks,
    update_base_stacks,
)
from ideavirtualdesktopcontroller.cli.module import app_module_clean_up


@click.group(CLICK_SETTINGS)
@click.version_option(version=ideavirtualdesktopcontroller.__version__)
def main():
    """
    idea virtual desktop controller - manage virtual desktops in your cluster
    """
    pass


main.add_command(logs)
main.add_command(reindex_user_sessions)
main.add_command(batch_create_sessions)
main.add_command(cleanup_orphaned_schedules)
main.add_command(terminate_sessions)
main.add_command(reindex_software_stacks)
main.add_command(merge_software_stacks)
main.add_command(update_base_stacks)
main.add_command(app_module_clean_up)

# used only for local testing
if __name__ == '__main__':
    main(sys.argv[1:])
