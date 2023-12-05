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

import ideascheduler

from ideadatamodel import exceptions
from ideadatamodel.constants import CLICK_SETTINGS

from ideascheduler.cli.jobs import jobs
from ideascheduler.cli.logs import logs
from ideascheduler.cli.ami_builder import ami_builder
from ideascheduler.cli.module import app_module_clean_up
from ideascheduler.cli.nodes import provision_always_on_nodes

import os
import sys
import click
import traceback


@click.group(CLICK_SETTINGS)
@click.version_option(version=ideascheduler.__version__)
def main():
    """
    idea-scheduler - HPC job provisioning
    """
    pass


main.add_command(jobs)
main.add_command(logs)
main.add_command(ami_builder)
main.add_command(provision_always_on_nodes)
main.add_command(app_module_clean_up)


def main_wrapper(*args, **kwargs):
    try:
        main(*args, **kwargs)
    except exceptions.SocaException as e:
        click.secho(e.message, fg='red', bold=True)
        if 'IDEA_DEBUG' in os.environ:
            traceback.print_exc()
        sys.exit(1)
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        click.secho(f'Command failed with error: {e}', fg='red', bold=True)
        raise e


# used only for local testing
if __name__ == '__main__':
    main_wrapper(sys.argv[1:])
