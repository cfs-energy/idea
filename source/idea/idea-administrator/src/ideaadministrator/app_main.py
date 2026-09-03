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

from ideadatamodel import (
    exceptions,
    errorcodes,
    constants,
    SocaKeyValue,
    SocaUserInputParamMetadata,
    SocaUserInputParamType,
    SocaUserInputValidate,
    SocaUserInputChoice,
)
from ideadatamodel.constants import CLICK_SETTINGS
from ideasdk.utils import Utils, ModuleMetadataHelper
from ideasdk.aws.image_builds import COMPUTE_IMAGE_PREFIX, image_state
from ideasdk.user_input.framework import SocaUserInputParamRegistry, SocaUserInputArgs
from ideasdk.config.cluster_config_db import ClusterConfigDB
from ideasdk.config.cluster_config import ClusterConfig
from ideasdk.config.soca_config import SocaConfig
from ideasdk.context import SocaCliContext, SocaContextOptions

import ideaadministrator
from ideaadministrator import app_constants
from ideaadministrator.app_utils import AdministratorUtils
from ideaadministrator.app.installer_params import QuickSetupPromptFactory
from ideaadministrator.app_props import AdministratorProps
from ideaadministrator.app.cdk.cdk_invoker import CdkInvoker
from ideaadministrator.app.config_generator import ConfigGenerator
from ideaadministrator.app.region_ami_config import resolve_region_ami
from ideaadministrator.app.delete_cluster import DeleteCluster
from ideaadministrator.app.patch_helper import PatchHelper
from ideaadministrator.app.deployment_helper import DeploymentHelper
from ideaadministrator.app.single_sign_on_helper import SingleSignOnHelper
from ideaadministrator.integration_tests.test_context import TestContext
from ideaadministrator.integration_tests.test_invoker import TestInvoker
from ideaadministrator.app.values_diff import ValuesDiff
from ideaadministrator.app.vpc_endpoints_helper import VpcEndpointsHelper
from ideaadministrator.app.aws_service_availability_helper import (
    AwsServiceAvailabilityHelper,
)
from ideaadministrator.app.cluster_prefix_list_helper import ClusterPrefixListHelper
from ideaadministrator.app.support_helper import SupportHelper
from ideaadministrator.app.directory_service_helper import DirectoryServiceHelper
from ideaadministrator.app.shared_storage_helper import SharedStorageHelper

from prettytable import PrettyTable
from typing import Dict, List, Optional, Set, Tuple
import os
import re
import sys
import click
import requests
import warnings
from rich.table import Table
from rich.console import Console
import time
import botocore.exceptions


@click.group(context_settings=CLICK_SETTINGS)
@click.version_option(version=ideaadministrator.__version__)
def main():
    """
    IDEA Administrator - install, deploy and manage your clusters

    \b
    * Deploy/Install IDEA from scratch
    ./idea-admin.sh quick-setup

    \b
    * Deploy/Install IDEA using existing resources running on your AWS account
    ./idea-admin.sh quick-setup --existing-resources

    * For all other operations (update/delete/check config...) please refer to the help section below
    """
    pass


@click.group()
def config():
    """
    configuration management options
    """
    pass


@click.group()
def cdk():
    """
    cdk app
    """
    pass


@click.group()
def sso():
    """
    single sign-on configuration options
    """
    pass


@click.group()
def directoryservice():
    """
    directory service utilities
    """
    pass


@click.group()
def utils():
    """
    cluster configuration utilities
    """
    pass


@utils.group()
def vpc_endpoints():
    """
    vpc endpoint utilities
    """
    pass


@utils.group()
def cluster_prefix_list():
    """
    cluster prefix list utilities
    """
    pass


@click.group()
def support():
    """
    support options
    """
    pass


@click.group()
def shared_storage():
    """
    shared-storage options
    """
    pass


@config.command('generate', context_settings=CLICK_SETTINGS)
@click.option('--values-file', help='path to values.yml file')
@click.option('--config-dir', help='path to where to create config directory')
@click.option('--force', is_flag=True, help='Skip all confirmation prompts.')
@click.option(
    '--existing-resources',
    is_flag=True,
    help='Generate configuration using existing resources',
)
@click.option(
    '--regenerate',
    is_flag=True,
    help='Regenerate configuration for an existing cluster. Enables skipping validations such as existing cluster name and CIDR block.',
)
def config_generate(
    values_file: str,
    config_dir: str,
    force: bool,
    existing_resources: bool = False,
    regenerate: bool = False,
):
    """
    generate configuration
    """

    context = SocaCliContext(
        options=SocaContextOptions(enable_aws_client_provider=True)
    )

    if config_dir is not None:
        if not os.path.isdir(config_dir):
            context.error(f'{config_dir} not found or is not a valid directory.')
            raise SystemExit

    if Utils.is_empty(values_file):
        param_registry = SocaUserInputParamRegistry(
            context=context, file=ideaadministrator.props.install_params_file
        )

        installer_args = SocaUserInputArgs(context, param_registry=param_registry)
        installer_args.set('_regenerate', regenerate)

        if existing_resources:
            deployment_option = (
                app_constants.DEPLOYMENT_OPTION_INSTALL_IDEA_USING_EXISTING_RESOURCES
            )
        else:
            deployment_option = app_constants.DEPLOYMENT_OPTION_INSTALL_IDEA

        prompt_factory = QuickSetupPromptFactory(
            context=context, args=installer_args, param_registry=param_registry
        )

        user_input_module = prompt_factory.build_module(
            user_input_module=deployment_option
        )
        user_input_module.safe_ask()
        values = installer_args.build()

        cluster_name = Utils.get_value_as_string('cluster_name', values)
        aws_region = Utils.get_value_as_string('aws_region', values)

        if not Utils.are_empty(cluster_name, aws_region):
            if not Utils.is_empty(config_dir):
                cluster_region_dir = config_dir
            else:
                props = AdministratorProps()
                cluster_dir = props.cluster_dir(cluster_name)
                cluster_region_dir = props.cluster_region_dir(cluster_dir, aws_region)

            if not force:
                if AdministratorUtils.cluster_region_dir_exists_and_has_config(
                    cluster_region_dir
                ):
                    confirm = context.prompt(
                        f'Config directory: {cluster_region_dir} is not empty, would you like to overwrite it?',
                        default=True,
                    )
                    if not confirm:
                        context.info('Aborted!')
                        raise SystemExit
                    AdministratorUtils.cleanup_cluster_region_dir(cluster_region_dir)

            os.makedirs(cluster_region_dir, exist_ok=True)
            values_file = os.path.join(cluster_region_dir, 'values.yml')

            context.info(f'saving values to: {values_file}')
            with open(values_file, 'w') as f:
                f.write(Utils.to_yaml(values))
        else:
            context.warning('Cluster name and AWS region are required')
            raise SystemExit

    # values file is given
    else:
        if not Utils.is_file(values_file):
            raise exceptions.invalid_params(f'file not found: {values_file}')

        with open(values_file, 'r') as f:
            values = Utils.from_yaml(f.read())

        props = AdministratorProps()
        # config dir is not provided
        if Utils.is_empty(config_dir):
            cluster_dir = props.cluster_dir(
                Utils.get_value_as_string('cluster_name', values)
            )
            cluster_region_dir = props.cluster_region_dir(
                cluster_dir, Utils.get_value_as_string('aws_region', values)
            )
        else:
            cluster_region_dir = config_dir

        values_file_copy = os.path.join(cluster_region_dir, 'values.yml')

        preserve_values_file = False
        if Utils.are_equal(values_file, values_file_copy):
            preserve_values_file = True

        if not force:
            if AdministratorUtils.cluster_region_dir_exists_and_has_config(
                cluster_region_dir
            ):
                confirm = context.prompt(
                    f'Config directory: {cluster_region_dir} is not empty, would you like to overwrite it?',
                    default=True,
                )
                if not confirm:
                    context.info('Aborted!')
                    raise SystemExit
                AdministratorUtils.cleanup_cluster_region_dir(
                    cluster_region_dir, preserve_values_file
                )

        os.makedirs(cluster_region_dir, exist_ok=True)

        context.info(f'saving values to: {values_file_copy}')
        with open(values_file_copy, 'w') as f:
            f.write(Utils.to_yaml(values))

    config_generator = ConfigGenerator(values)

    context.print_title('generating config from templates ...')

    if Utils.is_empty(config_dir):
        config_generator.generate_config_from_templates()
    else:
        config_generator.generate_config_from_templates(True, cluster_region_dir)

    # values returned by generate_config are used by quick setup flow. do not remove the return statement.
    return values


def _values_file_s3_location(
    cluster_name: str, aws_region: str, aws_profile: str, context: SocaCliContext
) -> Tuple[str, str]:
    """(bucket name, s3 key) of the cluster's saved values.yml"""
    cluster_config_db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    return (
        get_bucket_name(cluster_name, aws_region, cluster_config_db, context),
        ValuesDiff(cluster_name, aws_region).get_values_file_s3_key(),
    )


def download_values_file(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    context: SocaCliContext,
    values_file: str = None,
) -> str:
    """
    download values.yml from the cluster's s3 bucket to values_file, defaulting to the local
    cluster region directory. exits naming both locations when the bucket has no copy.
    """
    if Utils.is_empty(values_file):
        values_file = ValuesDiff(cluster_name, aws_region).get_values_file_path()

    bucket_name, s3_key = _values_file_s3_location(
        cluster_name, aws_region, aws_profile, context
    )

    try:
        response = context.aws().s3().get_object(Bucket=bucket_name, Key=s3_key)
    except Exception as e:
        context.error(
            f'Values file not found at {values_file} and could not be downloaded from '
            f's3://{bucket_name}/{s3_key}: {e}. Restore values.yml to {values_file} from '
            f'a backup, then upload it with: idea-admin.sh config save-values'
        )
        raise SystemExit(1)

    with open(values_file, 'w') as f:
        f.write(Utils.to_yaml(Utils.from_yaml(response['Body'])))

    context.info(f'downloaded s3://{bucket_name}/{s3_key} to {values_file}')
    return values_file


def _warn_values_file_drift(
    context: SocaCliContext,
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    values_file: str,
) -> None:
    """
    The local values.yml is the one the upgrade uses, so say so when the cluster's bucket holds a
    different one. Best effort: a bucket that cannot be read is not a reason to stop an upgrade.
    """
    try:
        bucket_name, s3_key = _values_file_s3_location(
            cluster_name, aws_region, aws_profile, context
        )
        response = context.aws().s3().get_object(Bucket=bucket_name, Key=s3_key)
        saved = Utils.get_as_dict(Utils.from_yaml(response['Body']), {})
        with open(values_file, 'r') as f:
            local = Utils.get_as_dict(Utils.from_yaml(f.read()), {})
    except Exception:
        return

    differing = sorted(
        key for key in set(saved) | set(local) if saved.get(key) != local.get(key)
    )
    if not differing:
        return

    shown = ', '.join(differing[:10])
    if len(differing) > 10:
        shown += f', and {len(differing) - 10} more'
    context.warning(
        f'the local {values_file} and s3://{bucket_name}/{s3_key} differ in '
        f'{len(differing)} key(s): {shown}. The upgrade uses the local copy.'
    )


def upload_values_file(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    context: SocaCliContext,
    values_file: str = None,
) -> str:
    """
    upload values.yml to the cluster's s3 bucket, defaulting to the local cluster region
    directory. returns the s3 uri written.
    """
    values_diff = ValuesDiff(cluster_name, aws_region)
    if Utils.is_empty(values_file):
        values_file = values_diff.get_values_file_path()

    cluster_config_db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    bucket_name = get_bucket_name(cluster_name, aws_region, cluster_config_db, context)
    s3_key = values_diff.get_values_file_s3_key()

    context.aws().s3().upload_file(Bucket=bucket_name, Filename=values_file, Key=s3_key)

    context.info(f'saved {values_file} to s3://{bucket_name}/{s3_key}')
    return f's3://{bucket_name}/{s3_key}'


@config.command('save-values', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--values-file', help='path to values.yml file')
def save_values(cluster_name: str, aws_profile: str, aws_region: str, values_file: str):
    """
    save values file in s3 bucket
    """

    context = SocaCliContext(
        options=SocaContextOptions(enable_aws_client_provider=True)
    )

    if Utils.is_empty(values_file):
        values_file = ValuesDiff(cluster_name, aws_region).get_values_file_path()
        print_using_default_warning('Values file', values_file, context)

    upload_values_file(cluster_name, aws_region, aws_profile, context, values_file)


@config.command('update', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--force', is_flag=True, help='Skip all confirmation prompts.')
@click.option(
    '--overwrite',
    is_flag=True,
    help='Overwrite existing db config entries. '
    'Default behavior is to skip if the config entry exists.',
)
@click.option(
    '--key-prefix',
    help='Update configuration for the keys matching the given key prefix.',
)
@click.option(
    '--config-dir',
    help='Path to Config Directory; Uses default location if not provided',
)
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
def config_update(
    cluster_name: str,
    aws_profile: str,
    aws_region: str,
    force: bool,
    overwrite: bool,
    key_prefix: str,
    config_dir: str,
    module_set: str,
):
    """
    update configuration from local file system to cluster settings db
    """

    context = SocaCliContext()

    if Utils.is_empty(module_set):
        module_set = constants.DEFAULT_MODULE_SET

    values = {
        'cluster_name': cluster_name,
        'aws_profile': aws_profile,
        'aws_region': aws_region,
    }
    config_generator = ConfigGenerator(values)

    if Utils.is_not_empty(config_dir):
        cluster_config_dir = os.path.join(config_dir, 'config')
        if not os.path.isdir(cluster_config_dir):
            context.error(f'{cluster_config_dir} does not exist')
            raise SystemExit
    else:
        cluster_config_dir = config_generator.get_cluster_config_dir()

    local_config_dict = config_generator.read_config_from_files(
        config_dir=cluster_config_dir
    )
    local_config = SocaConfig(config=local_config_dict)

    # perform a basic sanity check to see if the config in config dir is indeed the configuration for the expected cluster/aws region
    # this scenario is more likely to happen when using the --config-dir option
    #  where the configurations in the provided config dir does not match given --cluster-name and --aws-region
    cluster_module_id = local_config.get_string(
        f'global-settings.module_sets.{module_set}.cluster.module_id', required=True
    )
    local_config_cluster_name = local_config.get_string(
        f'{cluster_module_id}.cluster_name', required=True
    )
    if local_config_cluster_name != cluster_name:
        raise exceptions.cluster_config_error(
            f'local configuration in {cluster_config_dir} does not match the given cluster name: {cluster_name}'
        )
    local_config_aws_region = local_config.get_string(
        f'{cluster_module_id}.aws.region', required=True
    )
    if local_config_aws_region != aws_region:
        raise exceptions.cluster_config_error(
            f'local configuration in {cluster_config_dir} does not match the given aws region: {aws_region}'
        )

    def read_local_config_entries():
        context.info(f'reading cluster settings from {cluster_config_dir} ...')
        config_entries_ = config_generator.convert_config_to_key_value_pairs(
            key_prefix=key_prefix, path=cluster_config_dir
        )
        table = PrettyTable(['Key', 'Value'])
        table.align = 'l'
        for entry in config_entries_:
            key = Utils.get_value_as_string('key', entry, '-')
            value = Utils.get_any_value('value', entry, '-')
            if isinstance(value, list):
                value = Utils.to_yaml(value)
            table.add_row([key, value])
        print(table)
        return config_entries_

    config_entries = read_local_config_entries()
    if not force:
        while True:
            result = context.prompt(
                message='Are you sure you want to update cluster settings db with above configuration from local file system?',
                default='Yes',
                choices=['Yes', 'Reload Changes', 'Exit'],
            )
            if result == 'Exit':
                context.info('Aborted!')
                raise SystemExit
            elif result == 'Reload Changes':
                config_entries = read_local_config_entries()
            else:
                break

    local_config_dynamodb_kms_key_id = local_config.get_string(
        f'{cluster_module_id}.dynamodb.kms_key_id', required=False, default=None
    )

    cluster_config_db = ClusterConfigDB(
        cluster_name=config_generator.get_cluster_name(),
        aws_region=config_generator.get_aws_region(),
        aws_profile=config_generator.get_aws_profile(),
        dynamodb_kms_key_id=local_config_dynamodb_kms_key_id,
        create_database=True,
    )
    if config_dir:
        modules = config_generator.read_modules_from_files(cluster_config_dir)
    else:
        modules = config_generator.read_modules_from_files()
    cluster_config_db.sync_modules_in_db(modules)
    cluster_config_db.sync_cluster_settings_in_db(config_entries, overwrite)


@config.command('set', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--force', is_flag=True, help='Skip confirmation prompts')
@click.argument('entries', required=True, nargs=-1)
def set_config(
    cluster_name: str, aws_profile: str, aws_region: str, force: bool, entries
):
    """
    set config entries

    \b
    entry must be of below format: Key=KEY_NAME,Type=[str|int|float|bool|list<str>|list<int>|list<float>|list<bool>],Value=[VALUE|[VALUE1,VALUE2,...]] ...
    config key names cannot contain: comma(,), colon(:)

    Examples:

    \b
    1) To set a string config type:
    ./idea-admin.sh config set Key=global-settings.string_val,Type=string,Value=stringcontent --cluster-name YOUR_CLUSTER_NAME --aws-region YOUR_AWS_REGION

    \b
    2) To set an integer config type:
    ./idea-admin.sh config set Key=global-settings.int_val,Type=int,Value=12 --cluster-name YOUR_CLUSTER_NAME --aws-region YOUR_AWS_REGION

    \b
    3) To set a config with list of strings:
    ./idea-admin.sh config set "Key=my_config.string_list,Type=list<str>,Value=value1,value2" --cluster-name YOUR_CLUSTER_NAME --aws-region YOUR_AWS_REGION

    \b
    4) Update multiple config entries:
    ./idea-admin.sh config set Key=global-settings.string_val,Type=string,Value=stringcontent \\
                               "Key=global-settings.integer_list,Type=list<int>,Value=1,2" \\
                               "Key=global-settings.string_list,Type=list<str>,Value=str1,str2" \\
                               --cluster-name YOUR_CLUSTER_NAME \\
                               --aws-region YOUR_AWS_REGION
    """

    config_entries = []
    for index, entry in enumerate(entries):
        tokens = entry.split(',', 2)
        key = tokens[0].split('Key=')[1].strip()
        data_type = tokens[1].split('Type=')[1].strip()
        value = tokens[2].split('Value=')[1].strip()

        if Utils.is_empty(key):
            raise exceptions.cluster_config_error(f'[{index}] Key is required')
        if ',' in key or ':' in key:
            raise exceptions.cluster_config_error(
                f'[{index}] Invalid Key: {key}. comma(,) and colon(:) are not allowed in key names.'
            )
        if Utils.is_empty(data_type):
            raise exceptions.cluster_config_error(f'[{index}] Type is required')
        if Utils.is_empty(value):
            raise exceptions.cluster_config_error(f'[{index}] Value is required')

        is_list = False
        if data_type in ('str', 'string'):
            data_type = 'str'
        elif data_type in ('int', 'integer'):
            data_type = 'int'
        elif data_type in ('bool', 'boolean'):
            data_type = 'bool'
        elif data_type in ('float', 'decimal'):
            data_type = 'float'
        elif data_type in ('list<str>', 'list<string>'):
            data_type = 'str'
            is_list = True
        elif data_type in ('list<int>', 'list<integer>'):
            data_type = 'int'
            is_list = True
        elif data_type in ('list<bool>', 'list<boolean>'):
            data_type = 'bool'
            is_list = True
        elif data_type in ('list<float>', 'list<decimal>'):
            data_type = 'float'
            is_list = True
        else:
            raise exceptions.cluster_config_error(
                f'[{index}] Type: {data_type} not supported'
            )

        if is_list:
            tokens = value.split(',')
            value = []
            for token in tokens:
                if Utils.is_empty(token):
                    continue
                value.append(token.strip())
            if data_type == 'int':
                for val in value:
                    if not Utils.is_int(val):
                        raise exceptions.cluster_config_error(
                            f'[{index}] Value: {value} is not a valid list<{data_type}>'
                        )
                value = Utils.get_as_int_list(value)
            elif data_type == 'float':
                for val in value:
                    if not Utils.is_float(val):
                        raise exceptions.cluster_config_error(
                            f'[{index}] Value: {value} is not a valid list<{data_type}>'
                        )
                value = Utils.get_as_float_list(value)
            elif data_type == 'int':
                value = Utils.get_as_bool_list(value)
            else:
                value = Utils.get_as_string_list(value)
        else:
            if data_type == 'int':
                if not Utils.is_int(value):
                    raise exceptions.cluster_config_error(
                        f'[{index}] Value: {value} is not a valid {data_type}'
                    )
                value = Utils.get_as_int(value)
            elif data_type == 'float':
                if not Utils.is_float(value):
                    raise exceptions.cluster_config_error(
                        f'[{index}] Value: {value} is not a valid {data_type}'
                    )
                value = Utils.get_as_float(value)
            elif data_type == 'bool':
                value = Utils.get_as_bool(value)
            else:
                value = Utils.get_as_string(value)

        config_entries.append({'key': key, 'value': value})

    context = SocaCliContext()

    table = PrettyTable(['Key', 'Value'])
    table.align = 'l'
    for config_entry in config_entries:
        key = Utils.get_value_as_string('key', config_entry, '-')
        value = Utils.get_any_value('value', config_entry, '-')
        if isinstance(value, list):
            value = Utils.to_yaml(value)
        table.add_row([key, value])

    print(table)
    if not force:
        confirm = context.prompt(
            'Are you sure you want to update above config entries?'
        )
        if not confirm:
            context.info('Abort!')
            raise SystemExit

    db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    for config_entry in config_entries:
        db.set_config_entry(config_entry['key'], config_entry['value'])


@config.command('export', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option(
    '--export-dir',
    help='Export Directory. Defaults to: ~/.idea/clusters/<cluster-name>/<aws-region>/config',
)
def export_config(
    cluster_name: str, aws_profile: str, aws_region: str, export_dir: str
):
    """
    export configuration
    """

    if Utils.is_empty(export_dir):
        props = AdministratorProps()
        cluster_dir = props.cluster_dir(cluster_name)
        cluster_region_dir = props.cluster_region_dir(cluster_dir, aws_region)
        export_dir = os.path.join(cluster_region_dir, 'config')

    if Utils.is_dir(export_dir):
        files = os.listdir(export_dir)
        applicable_files = []
        for file in files:
            if file == '.DS_Store':
                continue
            applicable_files.append(file)

        if len(applicable_files) > 0:
            raise exceptions.general_exception(
                f'export directory: {export_dir} already exists and can cause merge conflicts. '
                f'backup your existing configuration to another directory and try again.'
            )

    db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    print(f'exporting config from db to {export_dir} ...')
    os.makedirs(export_dir, exist_ok=True)
    cluster_config = db.build_config_from_db()
    config_dict = cluster_config.as_dict()

    modules = db.get_cluster_modules()

    idea_config = {'modules': []}
    for module in modules:
        module_id = module['module_id']
        module_name = module['name']
        module_type = module['type']
        module_export_dir = os.path.join(export_dir, module_id)
        os.makedirs(module_export_dir, exist_ok=True)
        module_settings = Utils.get_value_as_dict(module_id, config_dict)
        module_settings_file = os.path.join(module_export_dir, 'settings.yml')
        with open(module_settings_file, 'w') as f:
            f.write(Utils.to_yaml(module_settings))
        idea_config['modules'].append(
            {
                'name': module_name,
                'id': module_id,
                'type': module_type,
                'config_files': ['settings.yml'],
            }
        )

    with open(os.path.join(export_dir, 'idea.yml'), 'w') as f:
        f.write(Utils.to_yaml(idea_config))


@config.command('download-values', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--values-dir', help='Path to folder to save values.yml file')
def download_values(
    cluster_name: str, aws_profile: str, aws_region: str, values_dir: str
):
    """
    download values.yml from s3 bucket to default or provided location
    """

    context = SocaCliContext(
        options=SocaContextOptions(enable_aws_client_provider=True)
    )

    values_file = None
    if Utils.is_empty(values_dir):
        print_using_default_warning(
            'Values file directory',
            ValuesDiff(cluster_name, aws_region).get_cluster_region_dir(),
            context,
        )
    else:
        os.makedirs(values_dir, exist_ok=True)
        values_file = os.path.join(values_dir, 'values.yml')

    download_values_file(cluster_name, aws_region, aws_profile, context, values_file)


@config.command('diff', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option(
    '--config-dir',
    help='Path to local config folder; default location will be used if none provided',
)
def diff_config(cluster_name: str, aws_profile: str, aws_region: str, config_dir: str):
    """
    diff configuration files between the latest config and the config in the db
    """

    props = AdministratorProps()
    if Utils.is_empty(config_dir):
        cluster_home = props.cluster_dir(cluster_name)
        cluster_region_dir = props.cluster_region_dir(cluster_home, aws_region)
        cluster_config_dir = os.path.join(cluster_region_dir, 'config')
        print_using_default_warning('Configuration Directory', cluster_config_dir)

    db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )

    db_config_entries = {}
    config_entries = db.get_config_entries()

    for entry in config_entries:
        db_config_entries[Utils.get_value_as_string('key', entry)] = (
            Utils.get_value_as_string('value', entry, '-')
        )

    local_config_entries = {}
    values = {
        'cluster_name': cluster_name,
        'aws_profile': aws_profile,
        'aws_region': aws_region,
    }
    config_generator = ConfigGenerator(values)
    local_config = config_generator.get_config_local(config_dir)

    for entry in local_config:
        local_config_entries[Utils.get_value_as_string('key', entry)] = (
            Utils.get_value_as_string('value', entry, '-')
        )

    set1 = set(local_config_entries.items())
    set2 = set(db_config_entries.items())

    set_x = set2 - set1
    set_y = set1 - set2

    table = Table()
    table.add_column('Key', justify='left', style='cyan', no_wrap=False)
    table.add_column('Old Value', justify='left', style='red', no_wrap=False)
    table.add_column('New Value', justify='left', style='green', no_wrap=False)
    table.add_column('Status', justify='left', style='magenta', no_wrap=False)

    rows = []

    for item in set_x:
        if item[0] in local_config_entries:
            rows.append(
                (item[0], item[1], local_config_entries.get(item[0]), 'MODIFIED')
            )
        else:
            rows.append((item[0], item[1], 'n/a', 'DELETED'))
    for item in set_y:
        if item[0] in db_config_entries:
            continue
        rows.append((item[0], 'n/a', item[1], 'ADDED'))

    for row in sorted(rows):
        table.add_row(row[0], row[1], row[2], row[3])

    console = Console()
    console.print(table)


@config.command('show', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option(
    '-q',
    '--query',
    help='Search Query for configuration entries. Accepts a regular expression.',
)
@click.option(
    '--format',
    'output_format',
    help='Output format. One of [table, yaml, raw]. Default: table',
)
def show_config(
    cluster_name: str, aws_profile: str, aws_region: str, query: str, output_format: str
):
    """
    show configuration for a cluster as yaml
    """
    cluster_config_db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    if output_format == 'yaml':
        cluster_config = cluster_config_db.build_config_from_db(query=query)
        print(cluster_config.as_yaml())
    elif output_format == 'raw':
        entries = cluster_config_db.get_config_entries(query=query)
        for entry in entries:
            value = entry.get('value')
            if value is not None:
                print(str(value))
    else:
        config_entries = cluster_config_db.get_config_entries(query=query)
        table = PrettyTable(['Key', 'Value', 'Version'])
        table.align = 'l'
        for entry in config_entries:
            key = Utils.get_value_as_string('key', entry, '-')
            value = Utils.get_any_value('value', entry, '-')
            if isinstance(value, list):
                value = Utils.to_yaml(value)
            version = Utils.get_value_as_int('version', entry, 0)
            table.add_row([key, value, version])
        print(table)


@config.command('delete', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.argument('config-key-prefixes', nargs=-1, required=True)
def delete_config(
    cluster_name: str, aws_profile: str, aws_region: str, config_key_prefixes
):
    """
    delete all configuration entries for a given config key prefix.

    to delete all configuration entries for module id: analytics, run:
    idea-admin config delete analytics.

    to delete all configuration entries for alb.listener_rules.*, run:
    idea-admin config delete alb.listener_rules.
    """
    db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    for config_key_prefix in config_key_prefixes:
        config_key_prefix = config_key_prefix.strip()
        db.delete_config_entries(config_key_prefix)


@click.command('bootstrap')
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option(
    '--termination-protection',
    default=True,
    help='Set termination protection to true or false. Default: true',
)
@click.option(
    '--custom-permissions-boundary',
    default='',
    help='Name of a custom permissions boundary to pass to CDK (Default to none)',
)
@click.option(
    '--cloudformation-execution-policies',
    default='',
    help='Customize CDK CloudFormation execution policies',
)
@click.option(
    '--public-access-block-configuration',
    default=True,
    help='Include S3 Block Public Access configuration for CDK staging bucket. Set to false for restricted S3 environments.',
)
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
def bootstrap_cluster(
    cluster_name: str,
    aws_profile: str,
    aws_region: str,
    termination_protection: bool,
    module_set: str,
    custom_permissions_boundary: str,
    cloudformation_execution_policies: str,
    public_access_block_configuration: bool,
):
    """
    bootstrap cluster
    """

    context = SocaCliContext()

    db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )

    cluster_s3_bucket = db.get_cluster_s3_bucket()
    with context.spinner(
        f'bootstrapping cluster CDK stack and S3 bucket: {cluster_s3_bucket} ...'
    ):
        CdkInvoker(
            module_id='bootstrap',
            module_set=module_set,
            cluster_name=cluster_name,
            aws_region=aws_region,
            aws_profile=aws_profile,
            termination_protection=termination_protection,
            custom_permissions_boundary=custom_permissions_boundary,
            cloudformation_execution_policies=cloudformation_execution_policies,
            public_access_block_configuration=public_access_block_configuration,
        ).bootstrap_cluster(cluster_bucket=cluster_s3_bucket)


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option(
    '--termination-protection',
    default=True,
    help='Set termination protection to true or false. Default: true',
)
@click.option('--deployment-id', help='A UUID to identify the deployment.')
@click.option(
    '--upgrade',
    is_flag=True,
    help='Upgrade the module by re-running the CDK stack if the module has already been deployed.',
)
@click.option(
    '--force-build-bootstrap',
    is_flag=True,
    help='If the bootstrap package directory for a given DeploymentId already exists, '
    'the directory will be deleted and rendered again.',
)
@click.option(
    '--rollback/--no-rollback',
    default=True,
    help='Rollback stack to stable state on failure. Defaults to "true", iterate more rapidly with --no-rollback.',
)
@click.option(
    '--optimize-deployment',
    is_flag=True,
    help='If flag is provided, deployment will be optimized and applicable stacks will be deployed in parallel.',
)
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
@click.argument('MODULES', required=True, nargs=-1)
def deploy(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    termination_protection: bool,
    deployment_id: str,
    upgrade: bool,
    force_build_bootstrap: bool,
    rollback: bool,
    optimize_deployment: bool,
    module_set: str,
    modules,
):
    """
    deploy modules

    deploy an IDEA module using the Module Id. You can provide multiple module ids and each module will be deployed sequentially.
    The order of module deployment will be handled automatically to ensure correct module dependencies.

    Use `all` as the module id to deploy all modules
    The default behavior of deploy is to skip deployment of a module if it's already deployed.
    Use --upgrade to re-run the cdk stack for the module.

    Experimental:
    The --optimize-deployment flag can be provided to optimize deployment time and deploy applicable modules in parallel.
    """

    # dedupe and convert to list
    module_ids_to_deploy = []
    all_modules = False
    for module_id in modules:
        if module_id == 'all':
            all_modules = True
        if module_id in module_ids_to_deploy:
            continue
        module_ids_to_deploy.append(module_id)

    if all_modules:
        if len(module_ids_to_deploy) > 1:
            raise exceptions.invalid_params(
                'fatal error - use of "all" deployment must be the only requested module'
            )
        module_ids_to_deploy = None

    DeploymentHelper(
        cluster_name=cluster_name,
        aws_region=aws_region,
        termination_protection=termination_protection,
        deployment_id=deployment_id,
        upgrade=upgrade,
        module_set=module_set,
        all_modules=all_modules,
        force_build_bootstrap=force_build_bootstrap,
        optimize_deployment=optimize_deployment,
        module_ids=module_ids_to_deploy,
        aws_profile=aws_profile,
        rollback=rollback,
    ).invoke()


@cdk.command('synth')
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--deployment-id', help='A UUID to identify the deployment.')
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
@click.argument('module', required=True)
def cdk_synth(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    deployment_id: str,
    module_set: str,
    module: str,
):
    """
    synthesize cloudformation template for a module
    """
    CdkInvoker(
        module_id=module,
        module_set=module_set,
        cluster_name=cluster_name,
        aws_region=aws_region,
        deployment_id=deployment_id,
        aws_profile=aws_profile,
    ).cdk_synth()


@cdk.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--deployment-id', help='A UUID to identify the deployment.')
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
@click.argument('module', required=True)
def diff(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    deployment_id: str,
    module_set: str,
    module: str,
):
    """
    compares the specified module with the deployed module
    """
    CdkInvoker(
        module_id=module,
        module_set=module_set,
        cluster_name=cluster_name,
        aws_region=aws_region,
        deployment_id=deployment_id,
        aws_profile=aws_profile,
    ).cdk_diff()


@cdk.command(context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--module-name', required=True, help='module name')
@click.option('--module-id', required=True, help='module id')
@click.option('--deployment-id', help='A UUID to identify the deployment.')
@click.option(
    '--termination-protection',
    default=True,
    help='Toggle termination protection for the cloud formation stack. Default: true',
)
def cdk_app(
    cluster_name,
    aws_profile,
    aws_region,
    module_name,
    module_id,
    deployment_id,
    termination_protection,
):
    """
    cdk app
    """
    from ideaadministrator.app.cdk.cdk_app import CdkApp

    CdkApp(
        cluster_name=cluster_name,
        aws_profile=aws_profile,
        aws_region=aws_region,
        module_name=module_name,
        module_id=module_id,
        deployment_id=deployment_id,
        termination_protection=termination_protection,
    ).invoke()


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--deployment-id', help='Deployment Id')
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
@click.argument('module', required=True)
def upload_packages(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    deployment_id: str,
    module_set: str,
    module: str,
):
    """
    upload applicable packages for a module
    """
    CdkInvoker(
        module_id=module,
        module_set=module_set,
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        deployment_id=deployment_id,
    ).invoke(
        upload_bootstrap_package=True, upload_release_package=True, deploy_stack=False
    )


@click.command('patch')
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option(
    '--package-uri', help='S3 package URI or package file path on local file system'
)
@click.option('--component', help='Component name')
@click.option('--instance-selector', help='Can be one of: [all, one]')
@click.option('--patch-command', help='Patch Command')
@click.option('--force', is_flag=True, help='Skip all confirmation prompts')
@click.argument('module', required=True)
def patch_module(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    package_uri: str,
    component: str,
    instance_selector: str,
    force: bool,
    patch_command: str,
    module: str,
):
    """
    patch application module with the current release

    only supported for modules with type = 'app'
    """

    PatchHelper(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        package_uri=package_uri,
        component=component,
        instance_selector=instance_selector,
        module_id=module,
        force=force,
        patch_command=patch_command,
    ).apply()


@click.command('check-cluster-status')
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option(
    '--wait', is_flag=True, help='Wait until all cluster endpoints are healthy.'
)
@click.option(
    '--wait-timeout',
    default=900,
    help='Wait timeout in seconds. Default: 900 (15 mins)',
)
@click.option('--debug', is_flag=True, help='Print debug messages')
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
def check_cluster_status(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    wait: bool,
    wait_timeout: int,
    debug: bool,
    module_set: str,
):
    """
    check status for all applicable cluster endpoints
    """

    def check_status(endpoint_url):
        def get_status() -> bool:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    result = requests.get(url=endpoint_url, verify=False)
                    if debug:
                        print(
                            f'{endpoint_url} - {result.status_code} - {Utils.to_json(result.text)}'
                        )
                    if result.status_code == 200:
                        return True
                    else:
                        return False
            except Exception as e:
                if debug:
                    print(f'{e}')
                return False

        return get_status

    cluster_config = ClusterConfig(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        module_set=module_set,
    )

    context = SocaCliContext()
    module_metadata = ModuleMetadataHelper()

    cluster_endpoint = cluster_config.get_cluster_external_endpoint()
    cluster_modules = cluster_config.db.get_cluster_modules()

    endpoints = []

    for cluster_module in cluster_modules:
        module_id = cluster_module['module_id']
        module_name = cluster_module['name']
        module_type = cluster_module['type']
        if module_name == constants.MODULE_ANALYTICS:
            url = f'{cluster_endpoint}/_dashboards/'
            endpoints.append(
                {
                    'name': 'OpenSearch Service Dashboard',
                    'endpoint': url,
                    'check_status': check_status(url),
                }
            )
        elif module_type == constants.MODULE_TYPE_APP:
            url = f'{cluster_endpoint}/{module_id}/healthcheck'
            endpoints.append(
                {
                    'name': module_metadata.get_module_title(module_name),
                    'endpoint': url,
                    'check_status': check_status(url),
                }
            )

    current_time = Utils.current_time_ms()
    end_time = current_time + (wait_timeout * 1000)
    fail_count = 0
    keyboard_interrupt = False

    while current_time < end_time:
        context.info(
            f'checking endpoint status for cluster: {cluster_name}, url: {cluster_endpoint} ...'
        )

        table = PrettyTable(['Module', 'Endpoint', 'Status'])
        table.align = 'l'

        fail_count = 0
        for endpoint in endpoints:
            success = endpoint['check_status']()
            if success:
                status = 'SUCCESS'
            else:
                status = 'FAIL'
                fail_count += 1

            table.add_row([endpoint['name'], endpoint['endpoint'], status])

        print(table)

        if not wait:
            break

        if fail_count == 0:
            break

        print(
            'failed to verify all cluster endpoints. wait ... (Press Ctrl + C to exit) '
        )
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            keyboard_interrupt = True
            print('Check-endpoint status aborted.')
            break
        current_time = Utils.current_time_ms()

    if not keyboard_interrupt:
        if wait and current_time >= end_time:
            context.warning(
                "check endpoint status timed-out. please verify your cluster's External ALB Security Group configuration and check correct ingress rules have been configured."
            )

        if fail_count > 0:
            raise SystemExit(1)


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--deployment-id', help='Deployment Id')
@click.option(
    '--force-build',
    is_flag=True,
    help='Delete and re-build bootstrap package if directory already exists.',
)
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
@click.argument('module', required=True)
def build_bootstrap_package(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    deployment_id: str,
    force_build: bool,
    module_set: str,
    module: str,
):
    """
    build bootstrap package for a module
    """
    CdkInvoker(
        module_id=module,
        module_set=module_set,
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        deployment_id=deployment_id,
    ).invoke(
        render_bootstrap_package=True,
        force_build_bootstrap=force_build,
        upload_bootstrap_package=False,
        upload_release_package=False,
        deploy_stack=False,
    )


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
def list_modules(cluster_name: str, aws_region: str, aws_profile: str):
    """
    list all modules for a cluster
    """
    db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    modules = db.get_cluster_modules()
    table = PrettyTable(
        ['Title', 'Name', 'Module ID', 'Type', 'Stack Name', 'Version', 'Status']
    )
    table.align = 'l'
    for module in modules:
        table.add_row(
            [
                Utils.get_value_as_string('title', module),
                Utils.get_value_as_string('name', module),
                Utils.get_value_as_string('module_id', module),
                Utils.get_value_as_string('type', module),
                Utils.get_value_as_string('stack_name', module, '-'),
                Utils.get_value_as_string('version', module, '-'),
                Utils.get_value_as_string('status', module),
            ]
        )
    print(table)


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
def show_connection_info(
    cluster_name: str, aws_region: str, aws_profile: str, module_set: str
):
    """
    print cluster connection information
    """
    cluster_config = ClusterConfig(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        module_set=module_set,
    )

    connection_info_entries = []

    cluster_modules = cluster_config.db.get_cluster_modules()

    cluster_endpoint = cluster_config.get_cluster_external_endpoint()

    if Utils.is_not_empty(cluster_endpoint):
        for cluster_module in cluster_modules:
            module_name = cluster_module['name']
            module_id = cluster_module['module_id']
            module_status = cluster_module['status']
            if module_status != 'deployed':
                continue
            if module_name == constants.MODULE_CLUSTER_MANAGER:
                connection_info_entries.append(
                    {'key': 'Web Portal', 'value': cluster_endpoint, 'weight': 0}
                )
            elif module_name == constants.MODULE_ANALYTICS:
                connection_info_entries.append(
                    {
                        'key': 'Analytics Dashboard',
                        'value': f'{cluster_endpoint}/_dashboards',
                        'weight': 3,
                    }
                )
            elif module_name == constants.MODULE_BASTION_HOST:
                key_pair_name = cluster_config.get_string(
                    'cluster.network.ssh_key_pair'
                )
                ip_address = cluster_config.get_string(f'{module_id}.public_ip')
                if Utils.is_empty(ip_address):
                    ip_address = cluster_config.get_string(f'{module_id}.private_ip')
                if Utils.is_not_empty(ip_address):
                    base_os = cluster_config.get_string(
                        f'{module_id}.base_os', required=True
                    )
                    ec2_username = AdministratorUtils.get_ec2_username(base_os)
                    connection_info_entries.append(
                        {
                            'key': 'Bastion Host (SSH Access)',
                            'value': f'ssh -i ~/.ssh/{key_pair_name}.pem {ec2_username}@{ip_address}',
                            'weight': 1,
                        }
                    )

                instance_id = cluster_config.get_string(f'{module_id}.instance_id')
                if Utils.is_not_empty(instance_id):
                    aws_partition = cluster_config.get_string(
                        'cluster.aws.partition', required=True
                    )
                    connection_manager_url = AdministratorUtils.get_session_manager_url(
                        aws_partition, aws_region, instance_id
                    )
                    connection_info_entries.append(
                        {
                            'key': 'Bastion Host (Session Manager URL)',
                            'value': connection_manager_url,
                            'weight': 2,
                        }
                    )

    context = SocaCliContext()

    if len(connection_info_entries) > 0:
        connection_info_entries.sort(key=lambda x: x['weight'])
        for entry in connection_info_entries:
            key = entry['key']
            value = entry['value']
            context.print(f'{key}: {value}')
    else:
        context.error(
            f'No connection information found for cluster: {cluster_name}. Is the cluster deployed?'
        )


@click.command()
def quick_setup_help():
    """
    display quick-setup help
    """
    values_file = ideaadministrator.props.default_values_file
    with open(values_file, 'r') as f:
        content = f.read()
    print(content)


@click.command()
@click.option('--values-file', help='path to values.yml file')
@click.option(
    '--existing-resources', is_flag=True, help='Install IDEA using existing resources'
)
@click.option(
    '--termination-protection',
    default=True,
    help='enable/disable termination protection for all stacks',
)
@click.option('--deployment-id', help='Deployment Id')
@click.option(
    '--optimize-deployment',
    is_flag=True,
    help='If flag is provided, deployment will be optimized and applicable stacks will be deployed in parallel.',
)
@click.option('--force', is_flag=True, help='Skip all confirmation prompts')
@click.option(
    '--skip-config',
    is_flag=True,
    help='Skip config generation and update steps. Assumes configuration tables are already created and configuration has already been synced. --values-file is required, when --skip-config flag is provided.',
)
@click.option(
    '--rollback/--no-rollback',
    default=True,
    help='Rollback stack to stable state on failure. Defaults to "true", iterate more rapidly with --no-rollback.',
)
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
@click.pass_context
def quick_setup(
    ctx,
    values_file: str,
    existing_resources: bool,
    termination_protection: bool,
    deployment_id: str,
    optimize_deployment: bool,
    force: bool,
    skip_config: bool,
    rollback: bool,
    module_set: str,
):
    """
    Install a new cluster
    """

    cli = SocaCliContext()
    if skip_config:
        if Utils.is_empty(values_file):
            cli.error('--values-file is required when --skip-config flag is provided.')
            raise SystemExit(1)
        if not Utils.is_file(values_file):
            cli.error(f'File not found: {values_file}')
            raise SystemExit(1)
        with open(values_file, 'r') as f:
            values = Utils.from_yaml(f.read())

        cluster_name = Utils.get_value_as_string('cluster_name', values)
        aws_region = Utils.get_value_as_string('aws_region', values)
        aws_profile = Utils.get_value_as_string('aws_profile', values)

    else:
        ctx.invoke(about)

        # generate config
        values = ctx.invoke(
            config_generate,
            values_file=values_file,
            existing_resources=existing_resources,
            force=force,
        )

        cluster_name = Utils.get_value_as_string('cluster_name', values)
        aws_region = Utils.get_value_as_string('aws_region', values)
        aws_profile = Utils.get_value_as_string('aws_profile', values)

        # update config
        ctx.invoke(
            config_update,
            cluster_name=cluster_name,
            aws_region=aws_region,
            aws_profile=aws_profile,
            force=force,
        )

    # print cluster configuration
    ctx.invoke(
        show_config,
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
    )

    # print module info
    ctx.invoke(
        list_modules,
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
    )

    if not force:
        continue_deployment = cli.prompt(
            'Are you sure you want to deploy above IDEA modules with applicable configuration settings?',
            default=True,
        )
        if not continue_deployment:
            cli.info('Deployment aborted!')
            raise SystemExit

    # todo - check required services using AwsServiceAvailabilityHelper before proceeding ahead with deployment

    # bootstrap cluster
    ctx.invoke(
        bootstrap_cluster,
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        termination_protection=termination_protection,
    )

    # deploy stacks
    deployment_helper = DeploymentHelper(
        cluster_name=cluster_name,
        aws_region=aws_region,
        termination_protection=termination_protection,
        deployment_id=deployment_id,
        module_set=module_set,
        force_build_bootstrap=True,
        optimize_deployment=optimize_deployment,
        aws_profile=aws_profile,
        all_modules=True,
        upgrade=False,
    )
    module_ids = deployment_helper.get_deployment_order()
    if len(module_ids) > 0:
        if optimize_deployment:
            deployment_order = deployment_helper.get_optimized_deployment_order()
        else:
            deployment_order = module_ids
        with cli.spinner(f'deploying modules: {deployment_order}'):
            ctx.invoke(
                deploy,
                cluster_name=cluster_name,
                aws_profile=aws_profile,
                aws_region=aws_region,
                termination_protection=termination_protection,
                deployment_id=deployment_id,
                rollback=rollback,
                optimize_deployment=optimize_deployment,
                modules=tuple(module_ids),
            )
    else:
        cli.info('all modules are already deployed. skipping deployment.')

    # wait for cluster endpoints to be healthy
    ctx.invoke(
        check_cluster_status,
        cluster_name=cluster_name,
        aws_profile=aws_profile,
        aws_region=aws_region,
        wait=True,
        wait_timeout=30 * 60,
    )

    # print module info
    ctx.invoke(
        list_modules,
        cluster_name=cluster_name,
        aws_profile=aws_profile,
        aws_region=aws_region,
    )

    # print connection information
    cli.print_rule('Cluster Connection Info')
    ctx.invoke(
        show_connection_info,
        cluster_name=cluster_name,
        aws_profile=aws_profile,
        aws_region=aws_region,
    )
    cli.print_rule()


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--delete-bootstrap', is_flag=True, help='Delete Bootstrap and S3 bucket')
@click.option('--delete-databases', is_flag=True, help='Delete Databases')
@click.option('--delete-backups', is_flag=True, help='Delete Backups')
@click.option('--delete-cloudwatch-logs', is_flag=True, help='Delete CloudWatch Logs')
@click.option('--delete-all', is_flag=True, help='Delete all')
@click.option('--force', is_flag=True, help='Skip confirmation prompts')
def delete_cluster(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    delete_bootstrap: bool,
    delete_databases: bool,
    delete_backups: bool,
    delete_cloudwatch_logs: bool,
    delete_all: bool,
    force: bool,
):
    """
    delete cluster
    """

    DeleteCluster(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        delete_bootstrap=delete_bootstrap,
        delete_databases=delete_databases,
        delete_backups=delete_backups,
        delete_cloudwatch_logs=delete_cloudwatch_logs,
        delete_all=delete_all,
        force=force,
    ).invoke()


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--force', is_flag=True, help='Skip confirmation prompts')
def delete_backups(cluster_name: str, aws_region: str, aws_profile: str, force: bool):
    """
    delete all recovery points in the cluster's backup vault
    """

    context = SocaCliContext()
    confirm_delete_backups = force
    if not force:
        confirm_delete_backups = context.prompt(
            f'Are you sure you want to delete all the backup recovery points for cluster {cluster_name} ?'
        )

    if not confirm_delete_backups:
        return

    DeleteCluster(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        delete_bootstrap=False,
        delete_databases=False,
        delete_backups=True,
        delete_cloudwatch_logs=False,
        delete_all=False,
        force=force,
    ).delete_backup_vault_recovery_points()


@click.command()
@click.option(
    '--no-banner', is_flag=True, help='Do not print graphics and additional information'
)
def about(no_banner: bool):
    """
    print IDEA release version info
    """
    props = AdministratorProps()
    dev_mode = props.is_dev_mode()
    if no_banner:
        tokens = [
            'Integrated Digital Engineering on AWS (IDEA)',
            f'Version: v{ideaadministrator.__version__}',
        ]
        if dev_mode:
            tokens.append('(Developer Mode)')
        print(', '.join(tokens))
    else:
        context = SocaCliContext()
        meta_info = [SocaKeyValue(key='Version', value=ideaadministrator.__version__)]
        if dev_mode:
            meta_info.append(SocaKeyValue(key='(Developer Mode)'))
        context.print_banner(meta_info=meta_info)


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--admin-username', required=True, help='Cluster Administrator Username')
@click.option('--admin-password', required=True, help='Cluster Administrator Password')
@click.option(
    '--test-case-id',
    help='Provide specific Test Case Id to execute. Multiple test case ids can be provided as a comma separated string.',
)
@click.option('--debug', is_flag=True, help='Enable debug logging')
@click.option(
    '--param',
    '-p',
    help='Additional test case parameters used by individual test cases. Format: Key=param1,Value=value1',
    multiple=True,
)
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
@click.argument('MODULES', required=True, nargs=-1)
def run_integration_tests(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    admin_username: str,
    admin_password: str,
    test_case_id: str,
    debug: bool,
    param: tuple,
    module_set: str,
    modules,
):
    """
    run integration tests for a module

    examples:

    \b
    * Run all scheduler test cases. All job test cases will be executed for the configured Compute Node OS and Compute Node AMI for the cluster
    ./idea-admin.sh run-integration-tests scheduler \\
            --cluster-name idea-test1 \\
            --aws-region eu-west-1 \\
            --admin-username YOUR_ADMIN_USER \\
            --admin-password YOUR_ADMIN_PASSWORD

    \b
    * Run all scheduler job submission test cases for all multiple base os
    ./idea-admin.sh run-integration-tests scheduler \\
            --cluster-name idea-test1 \\
            --aws-region eu-west-1 \\
            --admin-username YOUR_ADMIN_USER \\
            --admin-password YOUR_ADMIN_PASSWORD \\
            --param base_os=amazonlinux2023 \\
            --test-case-id SCHEDULER_JOB_TEST_CASES

    \b
    * Run scheduler job submission test cases: hello_world and custom_instance_type for base_os: amazonlinux2023 with a custom ami
    ./idea-admin.sh run-integration-tests scheduler \\
            --cluster-name idea-test1 \\
            --aws-region eu-west-1 \\
            --admin-username YOUR_ADMIN_USER \\
            --admin-password YOUR_ADMIN_PASSWORD \\
            --param job_test_cases=hello_world,custom_instance_type \\
            --param base_os=amazonlinux2023:YOUR_CUSTOM_AMI \\
            --test-case-id SCHEDULER_JOB_TEST_CASES
    """

    # dedupe and convert to list
    module_ids_to_test = []
    for module_id in modules:
        if module_id in module_ids_to_test:
            continue
        module_ids_to_test.append(module_id)

    extra_params = {}
    if Utils.is_not_empty(param):
        for token in param:
            kv = token.split('=', 1)
            if len(kv) == 2:
                key = kv[0]
                value = kv[1]
                extra_params[key] = value

    if not Utils.is_empty(test_case_id):
        test_case_ids = test_case_id.split(',')
    else:
        test_case_ids = []

    test_context = TestContext(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        admin_username=admin_username,
        admin_password=admin_password,
        debug=debug,
        extra_params=extra_params,
        test_case_ids=test_case_ids,
        module_set=module_set,
        module_ids=module_ids_to_test,
    )

    test_invoker = TestInvoker(test_context=test_context, module_ids=module_ids_to_test)

    test_invoker.invoke()


@sso.command('show-idp-info', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option(
    '--provider-type',
    required=True,
    help='Identity Provider Type. Can be one of [OIDC, SAML].',
)
def sso_show_idp_info(
    cluster_name: str, aws_region: str, aws_profile: str, provider_type: str
):
    """
    print single sign-on IDP redirect uri and related information for the cluster

    \b
    When provider_type = OIDC:
    Identity Provider Redirect URI = [cognito_domain_url]/oauth2/idpresponse

    \b
    When provider_type = SAML:
    Identity Provider Redirect URI = [cognito_domain_url]/saml2/idpresponse
    Entity ID = urn:amazon:cognito:sp:[cognito_user_pool_id]

    Use this URI to configure your Identity Provider before you enable SSO for the cluster.
    """
    sso_helper = SingleSignOnHelper(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    context = SocaCliContext()

    if provider_type.strip().upper() not in (
        constants.SSO_IDP_PROVIDER_SAML,
        constants.SSO_IDP_PROVIDER_OIDC,
    ):
        context.error('Invalid provider type. Must be one of: [SAML, OIDC]')
        raise SystemExit(1)

    context.print_title('Redirect URL')
    context.print(sso_helper.get_idp_redirect_uri(provider_type=provider_type))

    if provider_type == constants.SSO_IDP_PROVIDER_SAML:
        context.print_title('Entity ID')
        context.print(sso_helper.get_entity_id())

    context.new_line()
    context.info(
        'Configure the above information in your IDP prior to running ./idea-admin.sh sso configure ...'
    )


@sso.command('configure', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--provider-name', required=True, help='Identity Provider Name')
@click.option(
    '--provider-type',
    required=True,
    help='Identity Provider Type. Can be one of [OIDC, SAML].',
)
@click.option(
    '--provider-email-attribute',
    required=True,
    help='The name of the email attribute from Provider.',
)
@click.option(
    '--refresh-token-validity-hours',
    type=int,
    help='Refresh token validity in hours. Default: 12',
)
@click.option('--oidc-client-id', help='OIDC Client Id.')
@click.option('--oidc-client-secret', help='OIDC Client Secret')
@click.option(
    '--oidc-issuer',
    help='OIDC Issuer. The issuer URL you received from the OIDC provider.',
)
@click.option('--oidc-attributes-request-method', help='OIDC Attributes Request Method')
@click.option('--oidc-authorize-scopes', help='OIDC Authorize Scopes')
@click.option(
    '--oidc-authorize-url',
    help='OIDC Authorize URL. The endpoint a user is redirected to at sign-in.',
)
@click.option(
    '--oidc-token-url',
    help="OIDC Token URL. The endpoint Amazon Cognito uses to exchange the code received in a user's request for an ID token.",
)
@click.option(
    '--oidc-attributes-url',
    help='OIDC Attributes URL. Also called as UserInfo endpoint. The userInfo endpoint is used by Cognito to retrieve information about the authenticated user. ',
)
@click.option(
    '--oidc-jwks-uri',
    help='OIDC JWKS URI. The endpoint used to decode and verify tokens issued by the identity provider.',
)
@click.option('--saml-metadata-url', help='SAML Metadata URL')
@click.option('--saml-metadata-file', help='SAML Metadata File')
def sso_configure(cluster_name: str, aws_region: str, aws_profile: str, **kwargs):
    """
    configure single sign-on for the cluster
    """
    sso_helper = None
    try:
        sso_helper = SingleSignOnHelper(
            cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
        )
        sso_helper.configure_sso(**kwargs)
    except exceptions.SocaException as e:
        if e.error_code == errorcodes.INVALID_PARAMS:
            sso_helper.context.error(f'{e}')
        else:
            raise e


def print_using_default_warning(arg: str, path: str, cli: SocaCliContext = None):
    if cli is None:
        cli = SocaCliContext()
    cli.warning(f'WARNING: {arg} was not specified; Using default location: {path}')


def get_bucket_name(
    cluster_name: str,
    aws_region: str,
    cluster_config_db: ClusterConfigDB,
    context: SocaCliContext,
):
    config_entry = cluster_config_db.get_config_entry('cluster.cluster_s3_bucket')
    bucket_name = None
    if Utils.is_not_empty(config_entry):
        bucket_name = config_entry['value']
    if Utils.is_empty(bucket_name):
        aws_account_id = context.aws().aws_account_id()
        bucket_name = f'{cluster_name}-cluster-{aws_region}-{aws_account_id}'
    return bucket_name


@vpc_endpoints.command('service-info', context_settings=CLICK_SETTINGS)
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
def vpc_endpoints_service_info(aws_region: str, aws_profile: str):
    """
    print available vpc endpoint services in the region
    """
    vpc_endpoint_helper = VpcEndpointsHelper(
        aws_region=aws_region, aws_profile=aws_profile
    )
    vpc_endpoint_helper.print_vpc_endpoint_services()


@utils.command(context_settings=CLICK_SETTINGS)
def aws_services():
    """
    print all AWS services (required and optional) used by IDEA
    """
    helper = AwsServiceAvailabilityHelper()
    helper.print_idea_services()


@utils.command('check-aws-services', context_settings=CLICK_SETTINGS)
@click.option('--aws-profile', help='AWS Profile Name')
@click.argument('AWS_REGION', nargs=-1, required=True)
def check_aws_services(aws_profile: str, aws_region):
    """
    check and print availability of AWS services required by IDEA for a given AWS region
    """
    helper = AwsServiceAvailabilityHelper(aws_profile=aws_profile)
    helper.print_availability_matrix(aws_regions=list(aws_region))


@cluster_prefix_list.command('show', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
def cluster_prefix_list_show(cluster_name: str, aws_region: str, aws_profile: str):
    """
    print all CIDR entries in the cluster prefix list
    """
    entries = ClusterPrefixListHelper(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    ).list_entries()

    table = PrettyTable(['CIDR', 'Description'])
    table.align = 'l'
    for entry in entries:
        cidr = Utils.get_value_as_string('cidr', entry)
        description = Utils.get_value_as_string('description', entry, '-')
        table.add_row([cidr, description])
    print(table)


@cluster_prefix_list.command('add-entry', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--cidr', required=True, help='CIDR Entry')
@click.option('--description', required=True, help='CIDR Entry Description')
def cluster_prefix_list_add_entry(
    cluster_name: str, aws_region: str, aws_profile: str, cidr: str, description: str
):
    """
    add CIDR entry to cluster prefix list
    """
    ClusterPrefixListHelper(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    ).add_entry(cidr=cidr, description=description)


@cluster_prefix_list.command('remove-entry', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--cidr', required=True, help='CIDR Entry')
def cluster_prefix_list_remove_entry(
    cluster_name: str, aws_region: str, aws_profile: str, cidr: str
):
    """
    remove CIDR entry from cluster prefix list
    """
    ClusterPrefixListHelper(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    ).remove_entry(cidr=cidr)


@support.command('deployment', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
def support_deployment(
    cluster_name: str, aws_region: str, aws_profile: str, module_set: str
):
    """
    build deployment support debug package
    """
    SupportHelper(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        module_set=module_set,
    ).invoke()


@directoryservice.command(
    'create-service-account-secrets', context_settings=CLICK_SETTINGS
)
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--username', help='Service Account Username')
@click.option('--password', help='Service Account Password')
@click.option('--kms-key-id', help='KMS Key ID')
@click.option('--purpose', help='Account Purpose (e.g. service-account, clusteradmin)')
def ds_create_service_secrets(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    username: str,
    password: str,
    kms_key_id: str,
    purpose: str,
):
    """
    create service account secrets for directory service
    """

    context = SocaCliContext()

    helper = DirectoryServiceHelper(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )

    if Utils.is_any_empty(username, password):
        ask_result = context.ask(
            title='Directory Service - Service Account Credentials',
            description='Enter the account credentials to create secrets in AWS Secrets Manager with applicable tags',
            questions=[
                SocaUserInputParamMetadata(
                    name='purpose',
                    title='Purpose',
                    description='Enter account purpose',
                    data_type='str',
                    param_type=SocaUserInputParamType.SELECT,
                    multiple=False,
                    choices=[
                        SocaUserInputChoice(
                            title='clusteradmin    - Used for initial login/configuration of the IDEA cluster',
                            value='clusteradmin',
                        ),
                        SocaUserInputChoice(
                            title='service-account - Used for binding to the Active Directory/LDAP',
                            value='service-account',
                        ),
                    ],
                    default=purpose,
                    validate=SocaUserInputValidate(required=True),
                ),
                SocaUserInputParamMetadata(
                    name='username',
                    title='Username',
                    description='Enter account username',
                    data_type='str',
                    param_type=SocaUserInputParamType.TEXT,
                    default=username,
                    validate=SocaUserInputValidate(required=True),
                ),
                SocaUserInputParamMetadata(
                    name='password',
                    title='Password',
                    description='Enter account password',
                    data_type='str',
                    param_type=SocaUserInputParamType.PASSWORD,
                    default=username,
                    validate=SocaUserInputValidate(required=True),
                ),
            ],
        )
        purpose = ask_result['purpose']
        username = ask_result['username']
        password = ask_result['password']

    with context.spinner(f'creating {purpose} secrets ...'):
        secret_arns = helper.create_service_account_secrets(
            purpose=purpose, username=username, password=password, kms_key_id=kms_key_id
        )

    username_secret_arn = secret_arns['username_secret_arn']
    password_secret_arn = secret_arns['password_secret_arn']

    context.success(f'directory service {purpose} secrets created successfully: ')
    print(f'Account Purpose: {purpose}')
    print(f'Username Secret ARN: {username_secret_arn}')
    print(f'Password Secret ARN: {password_secret_arn}')


@shared_storage.command('add-file-system', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--kms-key-id', help='KMS Key ID')
def add_file_system(
    cluster_name: str, aws_region: str, aws_profile: str, kms_key_id: str
):
    """
    add new shared-storage file-system
    """
    SharedStorageHelper(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        kms_key_id=kms_key_id,
    ).add_file_system(use_existing_fs=False)


@shared_storage.command('attach-file-system', context_settings=CLICK_SETTINGS)
@click.option('--cluster-name', help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--kms-key-id', help='KMS Key ID')
def attach_file_system(
    cluster_name: str, aws_region: str, aws_profile: str, kms_key_id: str
):
    """
    attach existing shared-storage file-system
    """
    SharedStorageHelper(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        kms_key_id=kms_key_id,
    ).add_file_system(use_existing_fs=True)


def fancy_title(context, title, emoji=None):
    """
    Display a fancy colored title with optional emoji
    """
    title_text = f'{"  " + emoji + "  " if emoji else ""}{title}'
    border = '=' * (len(title_text) + 4)

    context.print('')
    context.print(border, style='bold cyan')
    context.print(f'  {title_text}  ', style='bold cyan')
    context.print(border, style='bold cyan')
    context.print('')


def _scan_module_table(db: ClusterConfigDB, table_name: str) -> List[Dict]:
    """
    Every item of a module table.
    A table that does not exist (module not deployed) yields no items.
    """
    items = []
    try:
        table = db.aws.dynamodb_table().Table(table_name)
        last_evaluated_key = None
        while True:
            if last_evaluated_key is not None:
                result = table.scan(ExclusiveStartKey=last_evaluated_key)
            else:
                result = table.scan()
            items += Utils.get_value_as_list('Items', result, [])
            last_evaluated_key = Utils.get_value_as_dict('LastEvaluatedKey', result)
            if last_evaluated_key is None:
                break
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            raise
    return items


def _find_eol_base_os_references(db: ClusterConfigDB) -> List[str]:
    """
    Cluster settings (including scheduler.compute_node_os) and HPC queue profiles that still
    reference an end-of-life base_os. These are edited rather than deleted, so they stay a hard
    stop for the upgrade.
    """
    findings = []

    for entry in db.get_config_entries():
        key = Utils.get_value_as_string('key', entry, '')
        value = Utils.get_value_as_string('value', entry)
        if value in constants.EOL_BASEOS and (
            key.endswith('base_os') or key.endswith('compute_node_os')
        ):
            findings.append(f'cluster setting {key} = {value}')

    for module in db.get_cluster_modules():
        module_id = Utils.get_value_as_string('module_id', module)
        if Utils.is_empty(module_id):
            continue
        if Utils.get_value_as_string('name', module) != constants.MODULE_SCHEDULER:
            continue
        for item in _scan_module_table(
            db, f'{db.cluster_name}.{module_id}.queue-profiles'
        ):
            base_os = Utils.get_value_as_string('param_base_os', item)
            if base_os in constants.EOL_BASEOS:
                name = Utils.get_value_as_string(
                    'queue_profile_name', item, '<unnamed>'
                )
                findings.append(f'HPC queue profile {name} = {base_os}')

    return findings


def _describe_stack(stack: Dict) -> str:
    return (
        f'{Utils.get_value_as_string("stack_id", stack)} '
        f'({Utils.get_value_as_string("name", stack, "<unnamed>")}, '
        f'{Utils.get_value_as_string("architecture", stack, "<unknown>")})'
    )


def _plan_eol_software_stacks(db: ClusterConfigDB) -> List[Dict]:
    """
    Read-only: what the upgrade would do to eVDI software stacks on an end-of-life base_os.
    They can no longer launch and their base_os cannot be edited, so they are deleted; a stack
    a live session still references is disabled instead, leaving running desktops untouched.
    One entry per eVDI module.
    """
    plans = []
    for module in db.get_cluster_modules():
        module_id = Utils.get_value_as_string('module_id', module)
        module_name = Utils.get_value_as_string('name', module)
        if (
            Utils.is_empty(module_id)
            or module_name != constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER
        ):
            continue

        stacks_table = f'{db.cluster_name}.{module_id}.controller.software-stacks'
        eol_stacks = [
            stack
            for stack in _scan_module_table(db, stacks_table)
            if Utils.get_value_as_string('base_os', stack) in constants.EOL_BASEOS
        ]
        if Utils.is_empty(eol_stacks):
            continue

        eol_stack_ids = {
            Utils.get_value_as_string('stack_id', stack) for stack in eol_stacks
        }
        sessions: List[Dict] = []
        for session in _scan_module_table(
            db, f'{db.cluster_name}.{module_id}.controller.user-sessions'
        ):
            if Utils.get_value_as_string('state', session, '').upper() == 'DELETED':
                continue
            software_stack = Utils.get_value_as_dict('software_stack', session, {})
            stack_id = Utils.get_value_as_string('stack_id', software_stack)
            base_os = Utils.get_value_as_string(
                'base_os', software_stack
            ) or Utils.get_value_as_string('base_os', session)
            if stack_id in eol_stack_ids or base_os in constants.EOL_BASEOS:
                sessions.append(
                    {
                        'stack_id': stack_id,
                        'base_os': base_os,
                        'session_id': Utils.get_value_as_string(
                            'idea_session_id', session, '<unknown>'
                        ),
                        'owner': Utils.get_value_as_string(
                            'owner', session, '<unknown>'
                        ),
                        'name': Utils.get_value_as_string('name', session, '<unnamed>'),
                    }
                )

        # a session that carries no stack_id can only be matched to a stack by base_os; keying on
        # the stack reference alone deletes a stack a running desktop is still on.
        stack_ids_in_use = {
            session['stack_id'] for session in sessions if session['stack_id']
        }
        base_os_in_use = {
            session['base_os'] for session in sessions if not session['stack_id']
        }

        to_delete, to_disable = [], []
        for stack in eol_stacks:
            in_use = (
                Utils.get_value_as_string('stack_id', stack) in stack_ids_in_use
                or Utils.get_value_as_string('base_os', stack) in base_os_in_use
            )
            (to_disable if in_use else to_delete).append(stack)

        plans.append(
            {
                'stacks_table': stacks_table,
                'sessions': sessions,
                'to_delete': to_delete,
                'to_disable': to_disable,
            }
        )
    return plans


def _stack_sessions(plan: Dict, stack: Dict) -> List[Dict]:
    stack_id = Utils.get_value_as_string('stack_id', stack)
    stack_base_os = Utils.get_value_as_string('base_os', stack)
    return [
        session
        for session in plan['sessions']
        if session['stack_id'] == stack_id
        or (not session['stack_id'] and session['base_os'] == stack_base_os)
    ]


def _apply_eol_software_stacks(
    context, cluster_name: str, aws_region: str, aws_profile: str, plans: List[Dict]
) -> None:
    """
    Delete or disable the software stacks _plan_eol_software_stacks found. Called only once the
    upgrade is confirmed: this is the first change the upgrade makes to the cluster.
    """
    if not plans:
        return
    db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    disabled = 0
    for plan in plans:
        table = db.aws.dynamodb_table().Table(plan['stacks_table'])
        for stack in plan['to_disable']:
            # enabled is a DynamoDB reserved word, hence the expression attribute name
            table.update_item(
                Key={
                    'base_os': Utils.get_value_as_string('base_os', stack),
                    'stack_id': Utils.get_value_as_string('stack_id', stack),
                },
                UpdateExpression='SET #enabled = :enabled',
                ExpressionAttributeNames={'#enabled': 'enabled'},
                ExpressionAttributeValues={':enabled': False},
            )
            disabled += 1
            still_in_use = ', '.join(
                f'{session["owner"]} ({session["name"]})'
                for session in _stack_sessions(plan, stack)
            )
            context.info(
                f'disabled end-of-life eVDI software stack {_describe_stack(stack)}, '
                f'still in use by {still_in_use}'
            )
        for stack in plan['to_delete']:
            table.delete_item(
                Key={
                    'base_os': Utils.get_value_as_string('base_os', stack),
                    'stack_id': Utils.get_value_as_string('stack_id', stack),
                }
            )
            context.info(
                f'deleted end-of-life eVDI software stack {_describe_stack(stack)}'
            )
        if plan['to_delete']:
            context.info(
                f'deleted {len(plan["to_delete"])} end-of-life eVDI software stack(s) from '
                f'{plan["stacks_table"]}'
            )

    if disabled:
        # the DynamoDB row is the source of truth, but the portal lists stacks from the search
        # index, so a disabled stack keeps showing as enabled until the index catches up.
        context.warning(
            f'{disabled} software stack(s) are disabled in DynamoDB but still read as enabled '
            f'in the eVDI search index until it is reindexed. The virtual-desktop-controller '
            f'redeploy later in this upgrade reconciles the index against DynamoDB at startup. '
            f'To reindex sooner, run "ideactl reindex-software-stacks --reset" on the '
            f'virtual-desktop-controller host.'
        )


def _check_eol_base_os(
    context,
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    disable_stacks_in_use: bool = False,
) -> List[Dict]:
    """
    Report every end-of-life base_os reference before the upgrade, changing nothing. Cluster
    settings and queue profiles are migrated by the admin and stay a hard stop, as does a live
    session on an end-of-life software stack unless disable_stacks_in_use is set. Returns the
    software stack plan for _apply_eol_software_stacks to run once the upgrade is confirmed.
    """
    db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    findings = _find_eol_base_os_references(db)
    if Utils.is_not_empty(findings):
        replacements = ', '.join(
            f'{eol} -> {replacement}'
            for eol, replacement in constants.EOL_BASEOS.items()
        )
        context.error(
            'This cluster still references a Base OS that has reached end-of-life and is no '
            'longer supported by IDEA. The upgrade cannot continue until every reference is '
            f'migrated ({replacements}).'
        )
        for finding in findings:
            context.error(f'  - {finding}')
        context.error(
            'Migrate the cluster settings and HPC queue profiles above to the replacement Base '
            'OS, then re-run upgrade-cluster.'
        )
        raise SystemExit(1)

    plans = _plan_eol_software_stacks(db)
    sessions = [session for plan in plans for session in plan['sessions']]
    if sessions and not disable_stacks_in_use:
        context.error(
            f'{len(sessions)} virtual desktop session(s) still use a Base OS that has '
            f'reached end-of-life. Nothing has been changed.'
        )
        for session in sessions:
            context.error(
                f'  - session {session["session_id"]} owned by {session["owner"]} on software '
                f'stack {session["stack_id"] or session["base_os"]}'
            )
        context.error('Delete these virtual desktops, then re-run upgrade-cluster.')
        context.error(
            'Alternatively, re-run upgrade-cluster with --disable-eol-stacks-in-use to '
            'disable these software stacks and continue.'
        )
        raise SystemExit(1)

    for plan in plans:
        for stack in plan['to_disable']:
            context.info(
                f'will disable end-of-life eVDI software stack {_describe_stack(stack)}'
            )
        for stack in plan['to_delete']:
            context.info(
                f'will delete end-of-life eVDI software stack {_describe_stack(stack)}'
            )
    return plans


# module name -> the (base_os key, ami key) pairs that module templates, relative to its module
# id. Every module that templates base_os / instance_ami must appear here, or the module keeps its
# pre-upgrade OS while the rest of the cluster moves.
AMI_UPDATE_KEYS = {
    constants.MODULE_BASTION_HOST: [('base_os', 'instance_ami')],
    constants.MODULE_CLUSTER_MANAGER: [
        ('ec2.autoscaling.base_os', 'ec2.autoscaling.instance_ami')
    ],
    constants.MODULE_DIRECTORYSERVICE: [('base_os', 'instance_ami')],
    constants.MODULE_SCHEDULER: [
        ('base_os', 'instance_ami'),
        ('compute_node_os', 'compute_node_ami'),
    ],
    constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER: [
        ('controller.autoscaling.base_os', 'controller.autoscaling.instance_ami'),
        ('dcv_broker.autoscaling.base_os', 'dcv_broker.autoscaling.instance_ami'),
        (
            'dcv_connection_gateway.autoscaling.base_os',
            'dcv_connection_gateway.autoscaling.instance_ami',
        ),
    ],
}


def build_ami_update_entries(
    ami_id: str, base_os: str, modules: List[Dict], keep_keys: Optional[Set[str]] = None
) -> List[str]:
    """
    The `config set` entries applied by upgrade_cluster Phase 3, for the modules this cluster
    actually deploys. Module ids come from the cluster config: config keys are prefixed with
    the module id, so a hardcoded id writes nothing on a cluster that renamed its modules.

    A key named in keep_keys keeps its current value, and so does the other half of its pair:
    writing one without the other leaves the module's base OS and AMI disagreeing.
    """
    keep_keys = keep_keys or set()
    entries = []
    for module in modules:
        module_id = Utils.get_value_as_string('module_id', module)
        key_pairs = AMI_UPDATE_KEYS.get(Utils.get_value_as_string('name', module))
        if Utils.is_empty(module_id) or not key_pairs:
            continue
        for base_os_key, ami_key in key_pairs:
            keys = (f'{module_id}.{base_os_key}', f'{module_id}.{ami_key}')
            if keep_keys.intersection(keys):
                continue
            entries.append(f'Key={keys[0]},Type=string,Value={base_os}')
            entries.append(f'Key={keys[1]},Type=string,Value={ami_id}')
    return entries


def keep_built_compute_image(current: Optional[Dict], stock: Optional[Dict]) -> bool:
    """
    Whether an upgrade must leave the scheduler's compute image alone. True only for an image IDEA
    built that is newer than the release's stock image: an operator who adopted a build from the
    Custom AMIs page should not lose it, but a build older than the release is stale. Creation dates
    are the ISO 8601 UTC strings EC2 returns, so they order as strings, and equal is not newer.
    """
    if not current or not stock:
        return False
    name = Utils.get_value_as_string('Name', current)
    if image_state(name, COMPUTE_IMAGE_PREFIX) != 'built':
        return False
    created = Utils.get_value_as_string('CreationDate', current, '')
    release_created = Utils.get_value_as_string('CreationDate', stock, '')
    if Utils.is_empty(created) or Utils.is_empty(release_created):
        return False
    return created > release_created


def resolve_compute_ami_keep_keys(
    context, db: ClusterConfigDB, modules: List[Dict], ami_id: str, ec2
) -> Set[str]:
    """
    The scheduler compute keys Phase 3 must not write. Compute moves onto the release image like
    every other module, unless the cluster already runs a newer image built from the Custom AMIs
    page, which an unconditional rewrite would silently discard. Anything it cannot determine
    leaves the keys writable, so an unreadable image never blocks an upgrade.
    """
    keep: Set[str] = set()
    for module in modules:
        if Utils.get_value_as_string('name', module) != constants.MODULE_SCHEDULER:
            continue
        module_id = Utils.get_value_as_string('module_id', module)
        if Utils.is_empty(module_id):
            continue
        entry = db.get_config_entry(f'{module_id}.compute_node_ami')
        current_ami = Utils.get_value_as_string('value', entry) if entry else None
        if Utils.is_empty(current_ami) or current_ami == ami_id:
            continue

        try:
            described = ec2.describe_images(ImageIds=[current_ami, ami_id])
        except Exception as e:
            context.warning(
                f'could not describe compute image {current_ami} or release image {ami_id}: {e}. '
                f'Compute moves to {ami_id}.'
            )
            continue

        images = {
            image['ImageId']: image
            for image in Utils.get_value_as_list('Images', described, [])
        }
        current = images.get(current_ami)
        stock = images.get(ami_id)
        if keep_built_compute_image(current, stock):
            keep.update(
                {f'{module_id}.compute_node_os', f'{module_id}.compute_node_ami'}
            )
            context.info(
                f'keeping built compute image {current_ami} '
                f'({Utils.get_value_as_string("Name", current)}, created '
                f'{Utils.get_value_as_string("CreationDate", current)}), newer than the release '
                f'image {ami_id} (created {Utils.get_value_as_string("CreationDate", stock)})'
            )
        elif (
            current
            and image_state(
                Utils.get_value_as_string('Name', current), COMPUTE_IMAGE_PREFIX
            )
            == 'built'
        ):
            context.info(
                f'built compute image {current_ami} is older than the release image {ami_id} and '
                f'was replaced; rebuild it from the Custom AMIs page to run a built image again'
            )
    return keep


def boto3_session(aws_profile: str):
    """a boto3 session for the profile the command was given, or the ambient credentials"""
    import boto3

    if Utils.is_not_empty(aws_profile):
        return boto3.Session(profile_name=aws_profile)
    return boto3.Session()


def get_cluster_base_os(db: ClusterConfigDB) -> List[str]:
    """
    The distinct Base OS values the cluster settings already carry, read from the same keys the
    upgrade writes. More than one means the cluster is part-migrated and the upgrade cannot pick
    for the admin.
    """
    values = {
        Utils.get_value_as_string('value', entry)
        for entry in db.get_config_entries()
        if Utils.get_value_as_string('key', entry, '').endswith('.base_os')
    }
    return sorted(value for value in values if Utils.is_not_empty(value))


def resolve_upgrade_base_os(
    context, cluster_name: str, aws_region: str, aws_profile: str, base_os: str
) -> str:
    """
    The Base OS this upgrade applies. Without an explicit --base-os the cluster keeps the Base OS
    its settings already carry, so an unattended --force run cannot silently move every module
    onto a different OS. Refuses rather than guessing when that value cannot be read.
    """
    try:
        current = get_cluster_base_os(
            ClusterConfigDB(
                cluster_name=cluster_name,
                aws_region=aws_region,
                aws_profile=aws_profile,
            )
        )
    except Exception as e:
        if Utils.is_empty(base_os):
            context.error(
                f'Could not read the cluster settings to determine the current Base OS: {e}. '
                f'Re-run with an explicit --base-os.'
            )
            raise SystemExit(1)
        current = []

    if Utils.is_empty(base_os):
        if len(current) != 1:
            found = ', '.join(current) if current else 'no base_os setting found'
            context.error(
                f'Could not determine the Base OS this cluster runs from its settings ({found}). '
                f'Re-run with an explicit --base-os to say which Base OS every module should use.'
            )
            raise SystemExit(1)
        context.info(
            f'No --base-os given: keeping the Base OS this cluster runs: {current[0]}'
        )
        return current[0]

    if current and base_os not in current:
        context.warning(
            f'--base-os {base_os} changes this cluster from {", ".join(current)}: every module '
            f'is redeployed onto {base_os}.'
        )
    return base_os


def get_module_instance_ids(
    cluster_name: str, aws_region: str, aws_profile: str, cfn
) -> List[Tuple[str, str]]:
    """
    (stack_name, instance_id) for every AWS::EC2::Instance the cluster's module stacks own. Module
    ids come from the cluster config because stack names are {cluster_name}-{module_id}: a hardcoded
    list omits modules outright and is wrong for any cluster with non-default module ids.
    """
    db = ClusterConfigDB(
        cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
    )
    found = []
    for module in db.get_cluster_modules():
        module_id = Utils.get_value_as_string('module_id', module)
        stack_name = (
            Utils.get_value_as_string('stack_name', module)
            or f'{cluster_name}-{module_id}'
        )
        try:
            for page in cfn.get_paginator('list_stack_resources').paginate(
                StackName=stack_name
            ):
                for resource in page['StackResourceSummaries']:
                    if resource['ResourceType'] == 'AWS::EC2::Instance':
                        found.append((stack_name, resource['PhysicalResourceId']))
        except botocore.exceptions.ClientError as e:
            error = e.response['Error']
            if error.get('Code') == 'ValidationError' and 'does not exist' in error.get(
                'Message', ''
            ):
                continue  # module not deployed
            # AccessDenied, throttling and friends are not "not deployed": swallowing them
            # silently skips instances that are protected, and cfn deletes them silently too.
            raise
    return found


def clear_termination_protection(
    instances: List[Tuple[str, str]], ec2, context
) -> List[Tuple[str, str]]:
    """
    Clear protection so cfn can delete an instance it replaces, and return what was cleared. Only
    instances that had it on, so one that was never protected does not come back protected.
    """
    cleared = []
    for stack_name, instance_id in instances:
        try:
            attribute = ec2.describe_instance_attribute(
                InstanceId=instance_id, Attribute='disableApiTermination'
            )
            if not attribute['DisableApiTermination']['Value']:
                continue
            ec2.modify_instance_attribute(
                InstanceId=instance_id, DisableApiTermination={'Value': False}
            )
            cleared.append((stack_name, instance_id))
            context.info(
                f'cleared instance termination protection on {instance_id} ({stack_name})'
            )
        except Exception as e:
            context.warning(
                f'could not clear termination protection on {instance_id} ({stack_name}): {e}. '
                f'If this upgrade replaces it, verify the old host is terminated afterwards.'
            )
    return cleared


def restore_termination_protection(cleared: List[Tuple[str, str]], ec2, context):
    """
    Re-protect what the sweep cleared. Filters by instance-id rather than passing ids directly,
    because describe_instances fails the whole call when one id has already been replaced.
    """
    if not cleared:
        return
    alive = {
        instance['InstanceId']
        for reservation in ec2.describe_instances(
            Filters=[
                {'Name': 'instance-id', 'Values': [i for _, i in cleared]},
                {
                    'Name': 'instance-state-name',
                    'Values': ['pending', 'running', 'stopping', 'stopped'],
                },
            ]
        )['Reservations']
        for instance in reservation['Instances']
    }
    for stack_name, instance_id in cleared:
        if instance_id not in alive:
            context.info(
                f'{instance_id} ({stack_name}) was replaced by the upgrade, so it has no '
                f'termination protection to restore'
            )
            continue
        try:
            ec2.modify_instance_attribute(
                InstanceId=instance_id, DisableApiTermination={'Value': True}
            )
            context.info(
                f'restored instance termination protection on {instance_id} ({stack_name})'
            )
        except Exception as e:
            context.warning(
                f'could not restore termination protection on {instance_id} '
                f'({stack_name}): {e}. Re-enable it by hand.'
            )


def _warn_termination_protection_cleared(context, cleared: List[Tuple[str, str]]):
    """
    Name the instances an aborted upgrade left unprotected. Deliberately not restored: a rollback
    may still need to delete them, and re-protecting mid-rollback strands one outside its stack.
    """
    if not cleared:
        return
    context.warning(
        f'termination protection is still cleared on '
        f'{", ".join(instance_id for _, instance_id in cleared)}. '
        f'Re-enable it by hand once the cluster is stable.'
    )


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option(
    '--termination-protection',
    default=True,
    help='Set termination protection to true or false. Default: true',
)
@click.option('--deployment-id', help='A UUID to identify the deployment.')
@click.option(
    '--base-os',
    help='Base OS to upgrade to (e.g., amazonlinux2023, rhel8, rhel9, rhel10, rocky8, rocky9, '
    'rocky10). Defaults to the Base OS the cluster already runs; pass this only to change it.',
)
@click.option(
    '--force-build-bootstrap',
    is_flag=True,
    help='If the bootstrap package directory for a given DeploymentId already exists, '
    'the directory will be deleted and rendered again.',
)
@click.option(
    '--rollback/--no-rollback',
    default=True,
    help='Rollback stack to stable state on failure. Defaults to "true".',
)
@click.option(
    '--optimize-deployment',
    is_flag=True,
    help='If flag is provided, deployment will be optimized and applicable stacks will be deployed in parallel.',
)
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
@click.option('--force', is_flag=True, help='Skip all confirmation prompts.')
@click.option(
    '--skip-global-settings-update',
    is_flag=True,
    help='Skip updating global settings. Use if you have already updated global settings.',
)
@click.option(
    '--disable-eol-stacks-in-use',
    is_flag=True,
    help='Disable end-of-life eVDI software stacks that a live virtual desktop session still '
    'uses, instead of aborting the upgrade. Running desktops are left untouched, but no new '
    'session can launch from a disabled stack.',
)
@click.argument('MODULES', required=False, nargs=-1)
def upgrade_cluster(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    termination_protection: bool,
    deployment_id: str,
    base_os: str,
    force_build_bootstrap: bool,
    rollback: bool,
    optimize_deployment: bool,
    module_set: str,
    force: bool,
    skip_global_settings_update: bool,
    disable_eol_stacks_in_use: bool,
    modules=None,
):
    """
    Upgrade IDEA cluster with pre-upgrade steps and module deployment

    This command combines pre-upgrade configuration steps and module deployment into a single operation.
    It handles updating the base OS, AMI IDs, global settings, and then deploys all specified modules.

    If no modules are specified, all modules will be upgraded.

    Example usage:
    ./idea-admin.sh upgrade-cluster --base-os amazonlinux2023 --aws-region us-east-2 --cluster-name idea-test1 --aws-profile default

    To upgrade specific modules:
    ./idea-admin.sh upgrade-cluster cluster metrics scheduler --base-os amazonlinux2023 --aws-region us-east-2 --cluster-name idea-test1
    """

    context = SocaCliContext(
        options=SocaContextOptions(
            enable_aws_client_provider=True,
            aws_region=aws_region,
            aws_profile=aws_profile,
        )
    )

    # An unattended --force run must not move every module onto a different Base OS by default.
    base_os = resolve_upgrade_base_os(
        context=context,
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        base_os=base_os,
    )

    # If no modules are specified, use 'all'
    if not modules:
        context.info('No modules specified, upgrading all modules')
        all_modules = True
    else:
        all_modules = False

    # Validate base OS
    if base_os in constants.EOL_BASEOS:
        context.error(
            f'Base OS {base_os} has reached end-of-life and is no longer supported by IDEA. '
            f'Upgrade to {constants.EOL_BASEOS[base_os]} instead.'
        )
        raise SystemExit(1)

    valid_os = [
        'amazonlinux2023',
        'rhel8',
        'rhel9',
        'rhel10',
        'rocky8',
        'rocky9',
        'rocky10',
    ]
    if base_os not in valid_os:
        context.error(
            f'Invalid base_os: {base_os}. Must be one of: {", ".join(valid_os)}'
        )
        raise SystemExit(1)

    # EL10 ships no Amazon DCV packages, so upgrading the eVDI control plane
    # (broker/gateway/controller) breaks every session; refuse here rather than rely on menu text.
    if base_os in ('rhel10', 'rocky10'):
        try:
            db_check = ClusterConfigDB(
                cluster_name=cluster_name,
                aws_region=aws_region,
                aws_profile=aws_profile,
            )
            deployed_modules = db_check.get_cluster_modules()
        except Exception as e:
            context.error(
                f'Could not read the cluster modules table to validate {base_os} '
                f'eVDI compatibility: {e}'
            )
            raise SystemExit(1)
        for cluster_module in deployed_modules:
            if (
                cluster_module.get('name')
                == constants.MODULE_VIRTUAL_DESKTOP_CONTROLLER
                and cluster_module.get('status') == 'deployed'
            ):
                context.error(
                    f'base_os {base_os} is not supported on clusters with the '
                    f'virtual-desktop-controller module deployed: Amazon DCV publishes '
                    f'no EL10 packages, so the eVDI broker/gateway/controller would '
                    f'redeploy without DCV installed. Use rhel9/rocky9 (or remove the '
                    f'eVDI module) instead.'
                )
                raise SystemExit(1)

    # Report end-of-life Base OS references before the upgrade is confirmed. This changes nothing:
    # the software stack plan it returns is applied further down, once the admin has said yes.
    eol_stack_plans = _check_eol_base_os(
        context=context,
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        disable_stacks_in_use=disable_eol_stacks_in_use,
    )

    # Display upgrade context information
    fancy_title(context, 'IDEA Cluster Upgrade', '🚀')
    context.info(f'🔹 Target cluster: {cluster_name} in region {aws_region}')
    context.info(f'🔹 Target base OS: {base_os}')
    if all_modules:
        context.info('🔹 Upgrade scope: All modules')
    else:
        context.info(f'🔹 Upgrade scope: Specific modules - {", ".join(modules)}')

    fancy_title(context, 'Upgrade Process Overview', '📋')
    context.info('The upgrade will proceed in the following phases:')
    context.info('1️⃣  Update base OS in values.yml')
    context.info('2️⃣  Configuration backup and global settings update')
    context.info('3️⃣  Sync full configuration to add any new values')
    context.info('4️⃣  Update AMI IDs and module-specific base OS settings')
    context.info('5️⃣  Module upgrade deployment')
    context.info(
        '⏱️  This process may take 1 hour or more to complete depending on the number of modules.'
    )

    if not force:
        confirm = context.prompt('Continue with the cluster upgrade?', default=True)
        if not confirm:
            context.info('Upgrade aborted by user')
            raise SystemExit(0)

    _apply_eol_software_stacks(
        context=context,
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        plans=eol_stack_plans,
    )

    # bound before the try so the failure path can always report what the sweep cleared
    cleared_protection: List[Tuple[str, str]] = []

    try:
        # PHASE 1: Update base_os in values.yml
        fancy_title(context, 'Phase 1: Update Base OS in values.yml', '📝')
        context.info(f'🔄 Updating base_os in values.yml to {base_os}...')

        # Get AMI ID for the selected OS and region for later use
        props = AdministratorProps()
        ami_config_path = props.region_ami_config_file()

        with open(ami_config_path, 'r') as f:
            ami_config = Utils.from_yaml(f.read())

        try:
            ami_id = resolve_region_ami(
                regions_config=ami_config, aws_region=aws_region, base_os=base_os
            )
        except exceptions.SocaException as e:
            context.error(
                f'Could not find AMI ID for base OS {base_os} in region {aws_region}: {e}'
            )
            raise SystemExit(1)

        # Update base_os in the cluster values file first
        _update_values_base_os(
            context=context,
            cluster_name=cluster_name,
            aws_region=aws_region,
            aws_profile=aws_profile,
            base_os=base_os,
        )

        # PHASE 2: Perform pre-upgrade configuration if not skipped
        config_generator = None
        export_dir = None

        if not skip_global_settings_update:
            fancy_title(context, 'Phase 2: Global Settings Backup and Update', '💾')

            # Now call the helper function to backup and update global settings
            # This will use the updated values.yml with the new base_os
            config_generator, export_dir = _perform_backup_update_global_settings(
                cluster_name=cluster_name,
                aws_region=aws_region,
                aws_profile=aws_profile,
                force=force,
                module_set=module_set,
                context=context,
            )

        # Optionally sync full configuration to add any new values that don't exist in DynamoDB
        sync_full_config = force
        if not force:
            context.info('')
            context.info('🔄  Optional: Sync full configuration to add any new values')
            context.info(
                "📋  This step will add any new configuration values that don't exist in DynamoDB"
            )
            context.info(
                '📋  without overwriting existing values. This is generally recommended.'
            )
            sync_full_config = context.prompt(
                'Sync full configuration to add new values?', default=True
            )

        if sync_full_config:
            # If global settings were skipped, we need to create the config_generator and export_dir
            if config_generator is None or export_dir is None:
                context.info('🔧  Setting up configuration objects for full sync...')
                props = AdministratorProps()
                cluster_dir = props.cluster_dir(cluster_name)
                cluster_region_dir = props.cluster_region_dir(cluster_dir, aws_region)
                export_dir = os.path.join(cluster_region_dir, 'config')
                values_file_path = os.path.join(cluster_region_dir, 'values.yml')

                # Read the existing values.yml file
                with open(values_file_path, 'r') as f:
                    values_dict = Utils.from_yaml(f.read())

                config_generator = ConfigGenerator(values_dict)

                # We need to regenerate config since global settings step was skipped
                context.info('🔄  Regenerating configuration from values.yml...')
                config_generator.generate_config_from_templates(
                    True, cluster_region_dir
                )

            _sync_full_config_without_overwrite(
                cluster_name=cluster_name,
                aws_region=aws_region,
                aws_profile=aws_profile,
                config_generator=config_generator,
                export_dir=export_dir,
                context=context,
            )
        else:
            context.info('⏭️  Skipping full configuration sync')

        # PHASE 3: Update AMI IDs and base_os values in DynamoDB
        # This phase now always runs to ensure infrastructure AMIs are updated
        fancy_title(context, 'Phase 3: Update AMI IDs and Settings', '🖥️')
        context.info('🔍 Using AMI ID for selected OS and region...')
        context.info(f'📌 AMI ID: {ami_id}')
        context.info(f'📌 Base OS: {base_os}')

        proceed_with_ami_updates = True
        if not force:
            confirm = context.prompt(
                'Continue with AMI and settings updates?', default=True
            )
            if not confirm:
                context.info('⏭️  Skipping AMI updates per user request')
                context.info(
                    '📋  Module deployment will proceed with existing AMI configurations'
                )
                proceed_with_ami_updates = False

        if proceed_with_ami_updates:
            # Update AMIs and base_os in DynamoDB
            db = ClusterConfigDB(
                cluster_name=cluster_name,
                aws_region=aws_region,
                aws_profile=aws_profile,
            )

            context.info('🔄 Updating AMI IDs and base_os values in DynamoDB...')
            cluster_modules = db.get_cluster_modules()
            ami_updates = build_ami_update_entries(
                ami_id=ami_id,
                base_os=base_os,
                modules=cluster_modules,
                keep_keys=resolve_compute_ami_keep_keys(
                    context=context,
                    db=db,
                    modules=cluster_modules,
                    ami_id=ami_id,
                    ec2=boto3_session(aws_profile).client(
                        'ec2', region_name=aws_region
                    ),
                ),
            )

            for entry in ami_updates:
                tokens = entry.split(',', 2)
                key = tokens[0].split('Key=')[1].strip()
                data_type = tokens[1].split('Type=')[1].strip()
                value = tokens[2].split('Value=')[1].strip()

                if data_type == 'string':
                    db.set_config_entry(key, value)

            context.success('✅ AMI and base_os settings updated successfully')

        # PHASE 4: Begin module deployment
        fancy_title(context, 'Phase 4: Module Deployment', '🏗️')
        context.info('🚀 Beginning module deployment...')
        context.info(
            '⏱️ This is the final phase of the upgrade process and may take 60+ minutes to complete.'
        )
        context.info(
            f'🔄 Deployment will use {"parallel execution ⚡" if optimize_deployment else "sequential execution 📊"}.'
        )

        # Add a warning about termination protection if scheduler or bastion-host modules are selected
        if modules and not all_modules:
            critical_modules = set(modules).intersection({'scheduler', 'bastion-host'})
            if critical_modules:
                context.info(
                    '⚠️ IMPORTANT: You are upgrading the following instances that may have termination protection: '
                    + ', '.join(critical_modules)
                )
                context.info(
                    '⚠️ Please ensure termination protection is disabled in the AWS console for these instances before proceeding.'
                )
        elif all_modules:
            context.info(
                '⚠️ IMPORTANT: This upgrade includes scheduler and bastion-host which may have termination protection enabled.'
            )
            context.info(
                '⚠️ Please ensure termination protection is disabled in the AWS console for these instances before proceeding.'
            )

        if not force and all_modules:
            confirm = context.prompt(
                'Proceed with deploying all modules?', default=True
            )
            if not confirm:
                context.info(
                    'Module deployment skipped. Pre-upgrade configuration is still in effect.'
                )
                context.info(
                    'You can deploy specific modules later with the deploy command.'
                )
                raise SystemExit(0)

        # an app-module upgrade can replace the module's ec2 instance; termination protection makes
        # cfn's delete of the original fail silently, leaving a running host no stack references.
        ec2 = None
        try:
            session = boto3_session(aws_profile)
            ec2 = session.client('ec2', region_name=aws_region)
            cleared_protection = clear_termination_protection(
                get_module_instance_ids(
                    cluster_name,
                    aws_region,
                    aws_profile,
                    session.client('cloudformation', region_name=aws_region),
                ),
                ec2,
                context,
            )
        except Exception as e:
            context.warning(
                f'pre-upgrade termination-protection sweep failed: {e}. '
                f'Verify replaced instances are terminated after the upgrade.'
            )

        # Deploy all modules with upgrade flag
        DeploymentHelper(
            cluster_name=cluster_name,
            aws_region=aws_region,
            aws_profile=aws_profile,
            termination_protection=termination_protection,
            deployment_id=deployment_id,
            upgrade=True,  # Always use upgrade=True for this command
            module_set=module_set,
            all_modules=all_modules,
            force_build_bootstrap=force_build_bootstrap,
            optimize_deployment=optimize_deployment,
            module_ids=modules if not all_modules else None,
            rollback=rollback,
        ).invoke()

        try:
            restore_termination_protection(cleared_protection, ec2, context)
        except Exception as e:
            context.warning(
                f'could not restore termination protection: {e}. Re-enable it by hand.'
            )

        _save_values_file_to_bucket(
            context=context,
            cluster_name=cluster_name,
            aws_region=aws_region,
            aws_profile=aws_profile,
        )

        fancy_title(context, 'Cluster Upgrade Completed', '🎉')
        context.success('✅ All upgrade phases completed successfully')
        context.info(
            '🔍 Run "idea-admin check-cluster-status" to verify cluster health.'
        )
    except Exception as e:
        context.error(f'❌ Failed to complete upgrade: {str(e)}')
        _warn_termination_protection_cleared(context, cleared_protection)
        raise SystemExit(1)
    except BaseException:
        # SystemExit and KeyboardInterrupt leave the instances unprotected just the same, so the
        # report has to run before the interpreter goes away.
        _warn_termination_protection_cleared(context, cleared_protection)
        raise


def _update_values_base_os(
    context: SocaCliContext,
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    base_os: str,
) -> str:
    """
    set base_os in the cluster's values.yml, restoring the file from the cluster's s3 bucket
    first when the local copy is missing. returns the values file path.
    """
    values_file_path = ValuesDiff(cluster_name, aws_region).get_values_file_path()

    if os.path.exists(values_file_path):
        _warn_values_file_drift(
            context, cluster_name, aws_region, aws_profile, values_file_path
        )
    else:
        context.warning(
            f'values.yml not found at {values_file_path}, '
            f'restoring it from the cluster s3 bucket ...'
        )
        download_values_file(cluster_name, aws_region, aws_profile, context)

    with open(values_file_path, 'r') as f:
        values = f.read()

    # a values.yml old enough to predate the key would otherwise be reported as updated.
    updated_values, replaced = re.subn(
        r'^base_os:.*$', f'base_os: {base_os}', values, flags=re.MULTILINE
    )
    if not replaced:
        context.error(
            f'{values_file_path} has no base_os key, so the upgrade cannot set it to {base_os}. '
            f'Add "base_os: {base_os}" to the file and re-run upgrade-cluster.'
        )
        raise SystemExit(1)

    with open(values_file_path, 'w') as f:
        f.write(updated_values)

    context.success(f'✅ Successfully updated base_os to {base_os} in values.yml')
    return values_file_path


def _save_values_file_to_bucket(
    context: SocaCliContext, cluster_name: str, aws_region: str, aws_profile: str
) -> None:
    """
    save values.yml back to the cluster's s3 bucket after a successful upgrade, so the next
    upgrade can restore it. a failed upload warns rather than failing the upgrade.
    """
    try:
        upload_values_file(cluster_name, aws_region, aws_profile, context)
    except Exception as e:
        profile = (
            f' --aws-profile {aws_profile}' if Utils.is_not_empty(aws_profile) else ''
        )
        context.warning(
            f'could not save values.yml to the cluster s3 bucket: {e}. The upgrade itself '
            f'succeeded; save the file with: idea-admin.sh config save-values '
            f'--cluster-name {cluster_name} --aws-region {aws_region}{profile}'
        )


# Helper function to sync full configuration without overwrite
def _sync_full_config_without_overwrite(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    config_generator: ConfigGenerator,
    export_dir: str,
    context=None,
):
    """
    Helper function to sync full configuration without overwrite.
    This adds any new configuration values that don't exist in DynamoDB.
    Uses the already-created config_generator and export_dir to avoid redundant work.
    """
    if context is None:
        context = SocaCliContext()

    try:
        db = ClusterConfigDB(
            cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
        )

        # Sync full config without overwrite to add any new values
        context.info('🔄  Syncing full configuration to add any new values...')
        all_config_entries = config_generator.convert_config_to_key_value_pairs(
            key_prefix='', path=export_dir
        )
        context.info(
            f'📊  Adding {len(all_config_entries)} configuration entries (new values only)...'
        )
        db.sync_cluster_settings_in_db(all_config_entries, overwrite=False)

        context.success('✅  Full configuration sync completed successfully')
        return True
    except Exception as e:
        context.error(f'❌  Failed to sync full configuration: {str(e)}')
        raise


# Helper function to perform backup and update global settings
def _perform_backup_update_global_settings(
    cluster_name: str,
    aws_region: str,
    aws_profile: str,
    force: bool,
    module_set: str,
    context=None,
):
    """
    Helper function to perform backup and update of global settings
    """
    if context is None:
        context = SocaCliContext()

    if not force:
        fancy_title(context, 'Global Settings Backup and Update', '💾')
        context.info('The following actions will be performed:')
        context.info('📥  Export current configuration')
        context.info('🔒  Create a backup with date stamp')
        context.info('🔄  Regenerate configuration files')
        context.info('📤  Update global settings in DynamoDB')
        confirm = context.prompt(
            'Continue with global settings backup and update?', default=True
        )
        if not confirm:
            context.info('Operation aborted by user')
            raise SystemExit(0)

    # Start the backup and update process
    try:
        # Setup config access
        props = AdministratorProps()
        cluster_dir = props.cluster_dir(cluster_name)
        cluster_region_dir = props.cluster_region_dir(cluster_dir, aws_region)
        export_dir = os.path.join(cluster_region_dir, 'config')
        values_file_path = os.path.join(cluster_region_dir, 'values.yml')

        db = ClusterConfigDB(
            cluster_name=cluster_name, aws_region=aws_region, aws_profile=aws_profile
        )

        # Export config with more detailed output
        context.info(f'📥  Exporting config from DB to {export_dir}...')
        os.makedirs(export_dir, exist_ok=True)
        cluster_config = db.build_config_from_db()
        config_dict = cluster_config.as_dict()

        context.info('🔍  Fetching module information...')
        modules = db.get_cluster_modules()

        idea_config = {'modules': []}

        context.info('⚙️  Processing module configurations...')
        for module in modules:
            module_id = module['module_id']
            module_name = module['name']
            module_type = module['type']
            module_export_dir = os.path.join(export_dir, module_id)
            os.makedirs(module_export_dir, exist_ok=True)
            module_settings = Utils.get_value_as_dict(module_id, config_dict)
            module_settings_file = os.path.join(module_export_dir, 'settings.yml')
            with open(module_settings_file, 'w') as f:
                f.write(Utils.to_yaml(module_settings))
            idea_config['modules'].append(
                {
                    'name': module_name,
                    'id': module_id,
                    'type': module_type,
                    'config_files': ['settings.yml'],
                }
            )

        context.info('📝  Creating main configuration file...')
        with open(os.path.join(export_dir, 'idea.yml'), 'w') as f:
            f.write(Utils.to_yaml(idea_config))

        # Make a backup with more detailed output
        backup_dir = f'{export_dir}.golden.{time.strftime("%m%d%Y_%H%M%S")}'
        context.info(f'🔒  Creating backup of configuration at {backup_dir}')
        if os.path.exists(backup_dir):
            import shutil

            shutil.rmtree(backup_dir)
        import shutil

        shutil.copytree(export_dir, backup_dir)
        context.info(
            '✅  Backup created successfully. This can be used for restoration if needed.'
        )

        # Regenerate config from values
        context.info('🔄  Regenerating configuration from values.yml...')

        # Read the existing values.yml file
        with open(values_file_path, 'r') as f:
            values_dict = Utils.from_yaml(f.read())

        # Now use these values for config generation
        config_generator = ConfigGenerator(values_dict)
        context.info('⚙️  Applying configuration templates...')
        config_generator.generate_config_from_templates(True, cluster_region_dir)

        # Update global settings in DDB
        context.info('📤  Updating global settings in DynamoDB...')

        context.info('🗑️  Deleting existing global settings to ensure clean state...')
        db.delete_config_entries('global-settings.')

        context.info('🔍  Preparing global settings for synchronization...')
        config_entries = config_generator.convert_config_to_key_value_pairs(
            key_prefix='global-settings', path=export_dir
        )
        context.info(
            f'📊  Synchronizing {len(config_entries)} configuration entries to database...'
        )
        db.sync_cluster_settings_in_db(config_entries, overwrite=True)

        context.success('✅  Global settings backup and update completed successfully')
        return config_generator, export_dir
    except Exception as e:
        context.error(f'❌  Failed to backup and update global settings: {str(e)}')
        raise


@click.command()
@click.option('--cluster-name', required=True, help='Cluster Name')
@click.option('--aws-region', required=True, help='AWS Region')
@click.option('--aws-profile', help='AWS Profile Name')
@click.option('--force', is_flag=True, help='Skip all confirmation prompts.')
@click.option('--module-set', help='Name of the ModuleSet. Default: default')
def backup_update_global_settings(
    cluster_name: str, aws_region: str, aws_profile: str, force: bool, module_set: str
):
    """
    Backup and update global settings for an IDEA cluster

    This command will:
    1. Export current configuration to a directory
    2. Create a backup of the configuration with date stamp
    3. Regenerate configuration files
    4. Update global settings in DynamoDB

    This is useful when you want to update global settings without performing a full upgrade.
    """

    # Use the helper function to perform the actual work
    config_generator, export_dir = _perform_backup_update_global_settings(
        cluster_name=cluster_name,
        aws_region=aws_region,
        aws_profile=aws_profile,
        force=force,
        module_set=module_set,
    )


main.add_command(deploy)
main.add_command(cdk)
main.add_command(config)
main.add_command(bootstrap_cluster)
main.add_command(upload_packages)
main.add_command(list_modules)
main.add_command(build_bootstrap_package)
main.add_command(show_connection_info)
main.add_command(quick_setup_help)
main.add_command(quick_setup)
main.add_command(delete_cluster)
main.add_command(delete_backups)
main.add_command(patch_module)
main.add_command(check_cluster_status)
main.add_command(about)
main.add_command(run_integration_tests)
main.add_command(sso)
main.add_command(utils)
main.add_command(support)
main.add_command(directoryservice)
main.add_command(shared_storage)
main.add_command(upgrade_cluster)
main.add_command(backup_update_global_settings)


def main_wrapper():
    success = True
    exit_code = 0

    try:
        args = sys.argv[1:]

        has_params = False
        if len(args) > 0:
            if len(args) == 1:
                param = args[0]
                has_params = Utils.is_not_empty(param)
            elif len(args) > 1:
                has_params = True

        if has_params:
            main(args)
        else:
            main(['-h'])

    except exceptions.SocaException as e:
        if e.error_code == errorcodes.USER_INPUT_FLOW_INTERRUPT:
            pass
        elif e.error_code == errorcodes.CLUSTER_CONFIG_NOT_INITIALIZED:
            click.secho(
                f'{e}. Is the cluster configuration synced?', fg='red', bold=True
            )
            success = False
        elif e.error_code in (
            errorcodes.CONFIG_ERROR,
            errorcodes.USER_INPUT_FLOW_ERROR,
            errorcodes.INTEGRATION_TEST_FAILED,
        ):
            click.secho(f'{e}', fg='red', bold=True)
            success = False
        else:
            raise e
    except botocore.exceptions.ProfileNotFound as e:
        click.secho(f'{e}', fg='red', bold=True)
        success = False
    except SystemExit as e:
        success = e.code == 0
        exit_code = e.code
    except Exception as e:
        click.secho(f'Command failed with error: {e}', fg='red', bold=True)
        raise e

    if not success:
        if exit_code == 0:
            exit_code = 1
        sys.exit(exit_code)


# used only for local testing
if __name__ == '__main__':
    main_wrapper()
