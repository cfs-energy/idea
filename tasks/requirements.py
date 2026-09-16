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

import tasks.idea as idea

from invoke import task

import os
import sys


@task(optional=['name'])
def update(c, name=None, upgrade=False, package_name=None):
    # type: (Context, Optional[str], bool, str) -> None # type: ignore
    """
    Update python requirements using pip-compile.
    """
    import shlex

    command = [
        'python',
        os.path.join(idea.props.project_root_dir, 'scripts', 'requirements-locks.py'),
    ]
    if name:
        command.extend(['--name', name])
    if upgrade:
        command.append('--upgrade')
    if package_name:
        command.extend(['--package', package_name])
    c.run(shlex.join(command), echo=True)


@task(optional=['name'])
def install(c, name=None):
    # type: (Context, Optional[str]) -> None # type: ignore
    """
    Install python requirements
    """
    project_root = idea.props.project_root_dir

    if name is None:
        name = 'dev'

    req_txt = os.path.join(project_root, 'requirements', f'{name}.txt')
    if not os.path.isfile(req_txt):
        idea.console.error(f'Requirements .txt file not found: {req_txt}')
        sys.exit(1)

    c.run(f'pip install -r {req_txt}', echo=True)
