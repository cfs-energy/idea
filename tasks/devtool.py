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
import yaml
from typing import Dict
import os

from ideasdk.utils import Utils


def get_profile(profile_name: str = 'default') -> Dict:
    if not os.path.isfile(idea.props.idea_user_config_file):
        raise idea.exceptions.general_exception(
            'devtool not configured. please run: invoke devtool.configure'
        )
    with open(idea.props.idea_user_config_file, 'r') as f:
        user_config = yaml.safe_load(f)
    profiles_config = user_config.get('profiles')
    if profiles_config is None:
        raise idea.exceptions.general_exception(
            'devtool not configured. please run: invoke devtool.configure'
        )
    profile_config = profiles_config.get(profile_name)
    if profile_config is None:
        raise idea.exceptions.invalid_params(
            f'profile not found for name: {profile_name}'
        )
    return profile_config


@task
def configure(
    _,
    profile=None,
    cluster_name=None,
    cluster_s3_bucket=None,
    aws_profile=None,
    aws_region=None,
    bastion_ip=None,
    ssh_key_file=None,
):
    # type: (Context, str, str, str, str, str, str, str) -> None # type: ignore
    """
    configure devtool
    :param _: Context
    :param profile: name of the idea configuration profile
    :param cluster_name: name of the idea cluster
    :param cluster_s3_bucket: name of the idea cluster
    :param aws_profile: name of the aws-cli profile to be used to access the AWS account. eg. default
    :param aws_region: name of the aws-region eg. us-west-2
    :param bastion_ip: Workstation IP address to SSH into the cluster.
    :param ssh_key_file: path to the SSH Key file to access the cluster nodes. ~/.ssh/my-key.pem
    """
    if profile is None:
        profile = 'default'

    if os.path.isfile(idea.props.idea_user_config_file):
        with open(idea.props.idea_user_config_file, 'r') as f:
            user_config = yaml.safe_load(f)
    else:
        user_config = {}

    if 'profiles' in user_config:
        profiles_config = user_config.get('profiles')
    else:
        profiles_config = {}
        user_config['profiles'] = profiles_config

    if profile in profiles_config:
        profile_config = profiles_config.get(profile)
    else:
        profile_config = {}
        profiles_config[profile] = profile_config

    if cluster_name is None:
        if 'cluster_name' in profile_config:
            cluster_name = profile_config.get('cluster_name')
        if cluster_name is None:
            cluster_name = ''
    if cluster_s3_bucket is None:
        if 'cluster_s3_bucket' in profile_config:
            cluster_s3_bucket = profile_config.get('cluster_s3_bucket')
        if cluster_s3_bucket is None:
            cluster_s3_bucket = ''
    if aws_profile is None:
        if 'aws_profile' in profile_config:
            aws_profile = profile_config.get('aws_profile')
        if aws_profile is None:
            aws_profile = ''
    if aws_region is None:
        if 'aws_region' in profile_config:
            aws_region = profile_config.get('aws_region')
        if aws_region is None:
            aws_region = ''
    if bastion_ip is None:
        if 'bastion_ip' in profile_config:
            bastion_ip = profile_config.get('bastion_ip')
        if bastion_ip is None:
            bastion_ip = ''
    if ssh_key_file is None:
        if 'ssh_key_file' in profile_config:
            ssh_key_file = profile_config.get('ssh_key_file')
        if ssh_key_file is None:
            ssh_key_file = ''

    questions = []
    if profile is None:
        questions.append(
            {
                'type': 'text',
                'name': 'profile',
                'message': 'Enter the name of the profile',
                'default': profile,
            }
        )
    questions.append(
        {
            'type': 'text',
            'name': 'cluster_name',
            'message': 'Enter the cluster name',
            'default': cluster_name,
        }
    )
    questions.append(
        {
            'type': 'text',
            'name': 'aws_profile',
            'message': 'Enter the AWS CLI profile name',
            'default': aws_profile,
        }
    )
    questions.append(
        {
            'type': 'text',
            'name': 'aws_region',
            'message': 'Enter the AWS Region name',
            'default': aws_region,
        }
    )
    questions.append(
        {
            'type': 'text',
            'name': 'bastion_ip',
            'message': 'Enter the IP address of the workstation ',
            'default': bastion_ip,
        }
    )
    questions.append(
        {
            'type': 'text',
            'name': 'ssh_key_file',
            'message': 'Enter path of the SSH Key File',
            'default': ssh_key_file,
        }
    )
    questions.append(
        {
            'type': 'text',
            'name': 'cluster_s3_bucket',
            'message': 'Enter the name of the S3 bucket for the cluster',
            'default': cluster_s3_bucket,
        }
    )
    result = idea.console.ask(questions=questions)

    profile_config = {**profile_config, **result}
    profiles_config[profile] = profile_config

    with open(idea.props.idea_user_config_file, 'w') as f:
        f.write(yaml.safe_dump(user_config))

    idea.console.info(f'config file updated: {idea.props.idea_user_config_file}')


@task
def upload_packages(c, profile_name=None, module=None):
    # type: (Context, str, str) -> None # type: ignore
    """
    upload packages
    """
    files = []
    if os.path.isdir(idea.props.project_dist_dir):
        files = os.listdir(idea.props.project_dist_dir)

    if len(files) == 0:
        idea.console.error(
            f'{idea.props.project_dist_dir} is empty or does not exist. '
            f'run invoke package to build package distributions'
        )
        return

    packages = []
    idea_release_version = idea.props.idea_release_version
    for file in files:
        if not file.endswith('tar.gz'):
            continue
        if idea_release_version not in file:
            continue
        if file.startswith('all'):
            continue
        if module is not None and module not in file:
            continue
        packages.append(file)

    if len(packages) == 0:
        idea.console.error('no packages found to upload')
        return

    if Utils.is_empty(profile_name):
        profile_name = 'default'

    profile_config = get_profile(profile_name)

    aws_profile = Utils.get_value_as_string('aws_profile', profile_config)
    aws_region = Utils.get_value_as_string('aws_region', profile_config)

    cluster_s3_bucket = Utils.get_value_as_string('cluster_s3_bucket', profile_config)
    if Utils.is_empty(cluster_s3_bucket):
        idea.console.error('cluster_s3_bucket not found in profile config')
        return

    s3_uri = f's3://{cluster_s3_bucket}/idea/releases/'

    idea.console.info(f'uploading {len(packages)} packages to {s3_uri} ...')
    with c.cd(idea.props.project_dist_dir):
        for file in packages:
            c.run(
                f'aws s3 cp {file} {s3_uri}{file} --profile {aws_profile} --region {aws_region}'
            )


@task()
def sync(c, profile_name=None):
    # type: (Context, str) -> None # type: ignore
    """
    rsync local sources with remote development server
    """

    if Utils.is_empty(profile_name):
        profile_name = 'default'

    profile_config = get_profile(profile_name)

    host = Utils.get_value_as_string('bastion_ip', profile_config)
    ssh_key_file = Utils.get_value_as_string('ssh_key_file', profile_config)

    cmd = f"""rsync -azvv \\
    --exclude ".git" \\
    --exclude ".DS*" \\
    --exclude "venv" \\
    --exclude "node_modules" \\
    --exclude "installer/resources" \\
    --exclude "docs" \\
    --exclude "source/idea/cluster_*" \\
    -e "ssh -i {ssh_key_file}" \\
    {idea.props.project_root_dir}/ ec2-user@{host}:/home/ec2-user/project/
    """
    c.run(cmd)


@task()
def build(c, module=None):
    # type: (Context, str) -> None # type: ignore
    """
    wrapper utility for invoke clean.<module> build.<module> package.<module>
    """
    if Utils.is_empty(module):
        idea.console.error('--module is required')
        return

    module_name = idea.utils.get_module_name(module)
    if Utils.is_empty(module_name):
        return idea.console.error(f'{module} not supported')

    # clean sdk and datamodel to trigger rebuild
    with c.cd(idea.props.project_root_dir):
        c.run('invoke clean.data-model build.data-model')
        c.run('invoke clean.sdk build.sdk')
        c.run(f'invoke clean.{module_name} build.{module_name} package.{module_name}')


@task()
def ssh(_, profile_name=None):
    # type: (Context, str) -> None # type: ignore
    """
    ssh into the workstation
    """
    if Utils.is_empty(profile_name):
        profile_name = 'default'

    profile_config = get_profile(profile_name)
    host = Utils.get_value_as_string('bastion_ip', profile_config)
    ssh_key = Utils.get_value_as_string('ssh_key_file', profile_config)

    os.system(
        f'ssh -i {ssh_key} -o ServerAliveInterval=10 -o ServerAliveCountMax=2 ec2-user@{host}'
    )
