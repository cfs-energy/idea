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

import os
from invoke import Collection

import tasks.idea as idea

idea.utils.update_source_paths(
    [
        idea.props.data_model_src,
        idea.props.sdk_src,
        idea.props.administrator_src,
        idea.props.cluster_manager_src,
        idea.props.scheduler_src,
        idea.props.virtual_desktop_src,
        idea.props.site_packages,
    ]
)

# Task module imports must come after update_source_paths() call
import tasks.clean as clean  # noqa: E402
import tasks.build as build  # noqa: E402
import tasks.package as package  # noqa: E402
import tasks.release as release  # noqa: E402
import tasks.requirements as requirements  # noqa: E402
import tasks.cli as cli  # noqa: E402
import tasks.tests as tests  # noqa: E402
import tasks.devtool as devtool  # noqa: E402
import tasks.web_portal as web_portal  # noqa: E402
import tasks.docker as docker  # noqa: E402
import tasks.apispec as apispec  # noqa: E402
import tasks.admin as admin  # noqa: E402

ns = Collection()
ns.configure({'run': {'echo': True}})

ns.add_collection(devtool)
ns.add_collection(clean)
ns.add_collection(build)
ns.add_collection(package)
ns.add_collection(release)
ns.add_collection(requirements, name='req')
ns.add_collection(cli)
ns.add_collection(web_portal)
ns.add_collection(docker)
ns.add_collection(apispec)
ns.add_collection(admin)
ns.add_collection(tests)

# local development - unique for individual developer.
# local_dev.py is added to .gitignore and is not checked in.
local_dev = os.path.join(idea.props.project_root_dir, 'tasks', 'local_dev.py')
if os.path.isfile(local_dev):
    import tasks.local_dev as local_dev_module  # type: ignore # noqa: E402

    ns.add_collection(local_dev_module)
