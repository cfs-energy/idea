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

from ideasdk.open_api import OpenAPITool
from ideasdk.utils import Utils
from ideadatamodel import IdeaOpenAPISpecEntry, constants

import os
from typing import List
from pathlib import Path


def _build_output(
    module: str,
    api_doc_file: str,
    spec_entries: List[IdeaOpenAPISpecEntry],
    enable_file_transfer_entries: bool = False,
    server_url: str = None,
    output_file: str = None,
):
    with open(api_doc_file, 'r') as f:
        api_doc = Utils.from_yaml(f.read())

    tool = OpenAPITool(
        api_doc=api_doc,
        entries=spec_entries,
        server_url=server_url,
        module_version=Path(os.environ['IDEA_PROJECT_ROOT'], 'IDEA_VERSION.txt')
        .read_text()
        .strip(),
        enable_file_transfer_entries=enable_file_transfer_entries,
    )

    output_format = (
        'json' if output_file is not None and output_file.endswith('.json') else 'yaml'
    )
    spec_content = tool.generate(output_format=output_format)

    if output_file is not None:
        output_file_parent = Path(output_file).parent
        if not output_file_parent.is_dir():
            os.makedirs(output_file_parent)

        with open(output_file, 'w') as f:
            f.write(spec_content)
    else:
        print(spec_content)


def cluster_manager(_, output_file=None, server_url=None):
    # type: (Context, str, str) -> None # type: ignore
    """
    cluster-manager api spec
    """
    from ideadatamodel import (
        OPEN_API_SPEC_ENTRIES_AUTH,
        OPEN_API_SPEC_ENTRIES_EMAIL_TEMPLATES,
        OPEN_API_SPEC_ENTRIES_PROJECTS,
        OPEN_API_SPEC_ENTRIES_CLUSTER_SETTINGS,
        OPEN_API_SPEC_ENTRIES_FILE_BROWSER,
        OPEN_API_SPEC_ENTRIES_REPORTING_INSIGHTS,
    )

    spec_entries = []
    spec_entries += OPEN_API_SPEC_ENTRIES_AUTH
    spec_entries += OPEN_API_SPEC_ENTRIES_EMAIL_TEMPLATES
    spec_entries += OPEN_API_SPEC_ENTRIES_PROJECTS
    spec_entries += OPEN_API_SPEC_ENTRIES_CLUSTER_SETTINGS
    spec_entries += OPEN_API_SPEC_ENTRIES_FILE_BROWSER
    spec_entries += OPEN_API_SPEC_ENTRIES_REPORTING_INSIGHTS

    api_doc_file = os.path.join(
        os.path.join(
            os.environ['IDEA_PROJECT_ROOT'], 'source', 'idea', 'idea-cluster-manager'
        ),
        'resources',
        'api',
        'api_doc.yml',
    )

    _build_output(
        module=constants.MODULE_CLUSTER_MANAGER,
        api_doc_file=api_doc_file,
        spec_entries=spec_entries,
        server_url=server_url,
        output_file=output_file,
        enable_file_transfer_entries=True,
    )


def virtual_desktop_controller(_, output_file=None, server_url=None):
    # type: (Context, str, str) -> None # type: ignore
    """
    virtual desktop controller api spec
    """
    from ideadatamodel import OPEN_API_SPEC_ENTRIES_VIRTUAL_DESKTOP

    spec_entries = OPEN_API_SPEC_ENTRIES_VIRTUAL_DESKTOP

    api_doc_file = os.path.join(
        os.path.join(
            os.environ['IDEA_PROJECT_ROOT'],
            'source',
            'idea',
            'idea-virtual-desktop-controller',
        ),
        'resources',
        'api',
        'api_doc.yml',
    )

    _build_output(
        module=constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER,
        api_doc_file=api_doc_file,
        spec_entries=spec_entries,
        server_url=server_url,
        output_file=output_file,
    )


def scheduler(_, output_file=None, server_url=None):
    # type: (Context, str, str) -> None # type: ignore
    """
    scheduler api spec
    """
    from ideadatamodel import OPEN_API_SPEC_ENTRIES_SCHEDULER

    spec_entries = OPEN_API_SPEC_ENTRIES_SCHEDULER

    api_doc_file = os.path.join(
        os.path.join(
            os.environ['IDEA_PROJECT_ROOT'], 'source', 'idea', 'idea-scheduler'
        ),
        'resources',
        'api',
        'api_doc.yml',
    )

    _build_output(
        module=constants.MODULE_SCHEDULER,
        api_doc_file=api_doc_file,
        spec_entries=spec_entries,
        server_url=server_url,
        output_file=output_file,
    )


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        'module', choices=['cluster-manager', 'scheduler', 'virtual-desktop-controller']
    )
    parser.add_argument('--output-file')
    parser.add_argument('--server-url')
    args = parser.parse_args()
    globals()[args.module.replace('-', '_')](None, args.output_file, args.server_url)
