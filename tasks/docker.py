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

import os
from invoke import task, Context
import shutil


@task
def prepare_artifacts(c):
    # type: (Context) -> None
    """
    copy docker artifacts to deployment directory
    """
    release_version = idea.props.idea_release_version
    all_package_archive = os.path.join(idea.props.project_dist_dir, f'all-{release_version}.tar.gz')
    if not os.path.isfile(all_package_archive):
        raise Exception(f'${all_package_archive} not found')
    shutil.copy(all_package_archive, idea.props.deployment_administrator_dir)


@task
def build(c, no_cache=False):
    # type: (Context, bool) -> None
    """
    Build administrator Docker image for multiple architectures (AMD64 and ARM).
    """

    prepare_artifacts(c)

    release_version = idea.props.idea_release_version
    build_cmd_amd = str(f'docker buildx build --platform linux/amd64 '
                        f'--build-arg PUBLIC_ECR_TAG=v{release_version} '
                        f'-t idea-administrator-amd64:v{release_version} '
                        f'"{idea.props.deployment_administrator_dir}"')
    build_cmd_arm = str(f'docker buildx build --platform linux/arm64 '
                        f'--build-arg PUBLIC_ECR_TAG=v{release_version} '
                        f'-t idea-administrator-arm64:v{release_version} '
                        f'"{idea.props.deployment_administrator_dir}"')

    if no_cache:
        build_cmd_amd = f'{build_cmd_amd} --no-cache'
        build_cmd_arm = f'{build_cmd_arm} --no-cache'

    c.run(build_cmd_amd)
    c.run(build_cmd_arm)


def publish(c, ecr_registry, ecr_tag):
    # type: (Context, str, str) -> None
    """
    publish docker image to an ECR repository
    """
    local_image = f'idea-administrator:{ecr_tag}'
    c.run(f'docker tag {local_image} {ecr_registry}/{local_image}')


@task
def print_commands(c):
    # type: (Context) -> None
    """
    print docker push commands for ECR
    """
    pass
