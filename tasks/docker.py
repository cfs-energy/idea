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
import yaml
from invoke import task
import shutil


def get_software_versions():
    """
    Read software versions from software_versions.yml
    """
    software_versions_file = os.path.join(
        idea.props.project_root_dir, 'software_versions.yml'
    )
    with open(software_versions_file, 'r') as f:
        return yaml.safe_load(f)


def get_build_args_from_versions():
    """
    Get Docker build arguments from software versions
    """
    versions = get_software_versions()
    return (
        f'--build-arg PYTHON_VERSION={versions["python_version"]} '
        f'--build-arg NODE_VERSION={versions["node_version"]} '
        f'--build-arg NVM_VERSION={versions["nvm_version"]} '
        f'--build-arg AWS_CDK_VERSION={versions["aws_cdk_version"]} '
    )


@task
def prepare_artifacts(c):
    # type: (Context) -> None # type: ignore
    """
    copy docker artifacts to deployment directory
    """
    release_version = idea.props.idea_release_version
    all_package_archive = os.path.join(
        idea.props.project_dist_dir, f'all-{release_version}.tar.gz'
    )
    if not os.path.isfile(all_package_archive):
        raise Exception(f'${all_package_archive} not found')
    shutil.copy(all_package_archive, idea.props.deployment_administrator_dir)


@task
def build(c, no_cache=False, gha_cache=False):
    # type: (Context, bool, bool) -> None # type: ignore
    """
    build administrator docker image
    """

    prepare_artifacts(c)

    release_version = idea.props.idea_release_version
    version_args = get_build_args_from_versions()
    build_cmd = str(
        f'docker build '
        f'--build-arg PUBLIC_ECR_TAG=v{release_version} '
        f'{version_args}'
        f'-t idea-administrator:v{release_version} '
        f'"{idea.props.deployment_administrator_dir}"'
    )
    if no_cache:
        build_cmd = f'{build_cmd} --no-cache'
    elif gha_cache:
        build_cmd = f'{build_cmd} --cache-from type=gha --cache-to type=gha,mode=max'
    c.run(build_cmd)


@task
def build_push_multi(c, ecr_registry, ecr_tag, no_cache=False, gha_cache=False):
    # type: (Context, str, str, bool, bool) -> None # type: ignore
    """
    Build and publish docker image to an ECR repository using buildx and IAM instance profile
    """

    prepare_artifacts(c)

    release_version = idea.props.idea_release_version
    version_args = get_build_args_from_versions()
    build_cmd = (
        f'aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin {ecr_registry} && '
        f'docker buildx ls | grep multi-platform-builder && docker buildx rm multi-platform-builder || echo "No builder to remove" && '
        f'docker builder prune -f && '
        f'docker buildx create --use --platform=linux/arm64,linux/amd64 --name multi-platform-builder && '
        f'docker buildx inspect --bootstrap && '
        f'docker buildx build --push --platform linux/amd64,linux/arm64 '
        f'--build-arg PUBLIC_ECR_TAG=v{release_version} '
        f'{version_args}'
        f'-t {ecr_registry}/idea-administrator:v{release_version} '
        f'-t {ecr_registry}/idea-administrator:{ecr_tag} '
        f'-t {ecr_registry}/idea-administrator:latest '
        f'"{idea.props.deployment_administrator_dir}" '
    )

    if no_cache:
        build_cmd = f'{build_cmd} --no-cache'
    elif gha_cache:
        build_cmd = f'{build_cmd} --cache-from type=gha --cache-to type=gha,mode=max'
    c.run(build_cmd)


@task
def publish(c, ecr_registry, ecr_tag):
    # type: (Context, str, str) -> None # type: ignore
    """
    publish docker image to an ECR repository
    """
    local_image = f'idea-administrator:{ecr_tag}'
    latest_image = 'idea-administrator:latest'

    # Tag the image with given tag
    c.run(f'docker tag {local_image} {ecr_registry}/{local_image}')

    # Tag the image with 'latest' tag
    c.run(f'docker tag {local_image} {ecr_registry}/{latest_image}')

    # Push the tagged images
    c.run(f'docker push {ecr_registry}/{local_image}')
    c.run(f'docker push {ecr_registry}/{latest_image}')


@task
def print_versions(c):
    # type: (Context) -> None # type: ignore
    """
    print current software versions from software_versions.yml
    """
    versions = get_software_versions()
    print('Software Versions:')
    for key, value in versions.items():
        print(f'  {key}: {value}')
    print(f'\nBuild arguments: {get_build_args_from_versions()}')


@task
def print_commands(c):
    # type: (Context) -> None # type: ignore
    """
    print docker push commands for ECR
    """
    pass
