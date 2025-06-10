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
    SocaAnyPayload,
)

from ideasdk.context import SocaContextOptions
from ideasdk.utils import Utils
from ideasdk.aws import AWSUtil, EC2InstanceTypesDB, AwsClientProvider
from ideasdk.auth import TokenService, TokenServiceOptions
from ideasdk.client.evdi_client import EvdiClient

from ideaclustermanager import AppContext
from ideaclustermanager.app.accounts.accounts_service import AccountsService
from ideaclustermanager.app.projects.projects_service import ProjectsService
from ideaclustermanager.app.accounts.cognito_user_pool import (
    CognitoUserPool,
    CognitoUserPoolOptions,
)
from ideaclustermanager.app.tasks.task_manager import TaskManager

from ideaclustermanagertests.mock_ldap_client import MockLdapClient

from ideatestutils import MockInstanceTypes, MockConfig
from ideatestutils import IdeaTestProps
from ideatestutils.dynamodb.dynamodb_local import DynamoDBLocal

import pytest
import boto3
import time
import requests

from _pytest.monkeypatch import MonkeyPatch

# initialize monkey patch globally, so that it can be used inside session scoped context fixtures
# this allows session scoped monkey patches to be applicable across all unit tests
# monkeypatch.undo() is called at the end of context fixture
monkeypatch = MonkeyPatch()


def wait_for_dynamodb_local(endpoint_url: str, timeout: int = 30):
    """Wait for DynamoDB Local to be ready to accept connections"""
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            # Try to make a simple request to check if server is up
            response = requests.get(endpoint_url, timeout=1)
            # DynamoDB Local returns a 400 for GET requests, but that means it's running
            if response.status_code == 400:
                print(f'DynamoDB Local is ready at {endpoint_url}')
                return True
        except requests.exceptions.ConnectionError:
            # Server not ready yet, continue waiting
            pass
        except requests.exceptions.Timeout:
            # Server not ready yet, continue waiting
            pass

        print(f'Waiting for DynamoDB Local at {endpoint_url}...')
        time.sleep(1)

    print(f'DynamoDB Local failed to start within {timeout} seconds')
    return False


@pytest.fixture(scope='session')
def ddb_local():
    ddb_local = DynamoDBLocal(db_name='cluster-manager', reset=True)

    # Check and install DynamoDB Local if not already installed
    install_success = ddb_local.check_and_install()
    if not install_success:
        pytest.skip('DynamoDB Local installation failed')

    ddb_local.start()

    # Wait for DynamoDB Local to actually be ready
    endpoint_url = f'http://localhost:{ddb_local.port}/'
    if not wait_for_dynamodb_local(endpoint_url, timeout=30):
        ddb_local.stop()
        pytest.skip('DynamoDB Local failed to start')

    yield ddb_local

    ddb_local.stop()


@pytest.fixture(scope='session')
def context(ddb_local):
    """
    fixture to initialize context with mock config and aws clients
    goal is to ensure no network request are executed while executing unit tests
    """

    print('initializing cluster-manager context ...')

    def mock_function(*_, **__):
        return {}

    mock_boto_session = SocaAnyPayload()
    mock_boto_session.region_name = 'us-east-1'
    mock_boto_session.client = mock_function

    mock_ec2_client = SocaAnyPayload()
    mock_ec2_client.describe_security_groups = mock_function

    # Create a mock paginator for EC2 instance types
    def mock_get_paginator(operation_name):
        mock_paginator = SocaAnyPayload()

        def mock_paginate(**kwargs):
            # Return a generator that yields mock instance type data
            mock_page = {
                'InstanceTypes': [
                    {
                        'InstanceType': 't3.micro',
                        'CurrentGeneration': True,
                        'VCpuInfo': {'DefaultVCpus': 2},
                        'MemoryInfo': {'SizeInMiB': 1024},
                        'ProcessorInfo': {'SupportedArchitectures': ['x86_64']},
                    },
                    {
                        'InstanceType': 't3.small',
                        'CurrentGeneration': True,
                        'VCpuInfo': {'DefaultVCpus': 2},
                        'MemoryInfo': {'SizeInMiB': 2048},
                        'ProcessorInfo': {'SupportedArchitectures': ['x86_64']},
                    },
                ]
            }
            yield mock_page

        mock_paginator.paginate = mock_paginate
        return mock_paginator

    mock_ec2_client.get_paginator = mock_get_paginator

    mock_s3_client = SocaAnyPayload()
    mock_s3_client.upload_file = mock_function
    mock_s3_client.get_bucket_acl = mock_function

    # Create a mock DynamoDB client that connects to DynamoDB Local
    mock_dynamodb_client = SocaAnyPayload()

    # Create a real DynamoDB client that connects to the local instance
    real_dynamodb_client = boto3.client(
        'dynamodb',
        endpoint_url=f'http://localhost:{ddb_local.port}',
        region_name='us-east-1',
        aws_access_key_id='dummy',
        aws_secret_access_key='dummy',
    )

    # Copy all the real DynamoDB client methods to our mock
    mock_dynamodb_client.describe_table = real_dynamodb_client.describe_table
    mock_dynamodb_client.create_table = real_dynamodb_client.create_table
    mock_dynamodb_client.put_item = real_dynamodb_client.put_item
    mock_dynamodb_client.get_item = real_dynamodb_client.get_item
    mock_dynamodb_client.update_item = real_dynamodb_client.update_item
    mock_dynamodb_client.delete_item = real_dynamodb_client.delete_item
    mock_dynamodb_client.scan = real_dynamodb_client.scan
    mock_dynamodb_client.query = real_dynamodb_client.query
    mock_dynamodb_client.update_time_to_live = real_dynamodb_client.update_time_to_live
    mock_dynamodb_client.batch_get_item = real_dynamodb_client.batch_get_item
    mock_dynamodb_client.batch_write_item = real_dynamodb_client.batch_write_item

    # Create a mock DynamoDB table resource that connects to DynamoDB Local
    mock_dynamodb_table_resource = SocaAnyPayload()

    # Create a real DynamoDB table resource that connects to the local instance
    real_dynamodb_table_resource = boto3.resource(
        'dynamodb',
        endpoint_url=f'http://localhost:{ddb_local.port}',
        region_name='us-east-1',
        aws_access_key_id='dummy',
        aws_secret_access_key='dummy',
    )

    # Copy the Table method to our mock
    mock_dynamodb_table_resource.Table = real_dynamodb_table_resource.Table

    mock_cognito_idp = SocaAnyPayload()
    mock_cognito_idp.admin_create_user = mock_function
    mock_cognito_idp.admin_add_user_to_group = mock_function
    mock_cognito_idp.admin_get_user = mock_function
    mock_cognito_idp.admin_set_user_password = mock_function
    mock_cognito_idp.admin_update_user_attributes = mock_function
    mock_cognito_idp.admin_enable_user = mock_function
    mock_cognito_idp.admin_disable_user = mock_function

    # Add the describe_user_pool method that returns a mock password policy
    def mock_describe_user_pool(*args, **kwargs):
        return {
            'UserPool': {
                'Policies': {
                    'PasswordPolicy': {
                        'MinimumLength': 8,
                        'RequireUppercase': True,
                        'RequireLowercase': True,
                        'RequireNumbers': True,
                        'RequireSymbols': True,
                        'TemporaryPasswordValidityDays': 7,
                    }
                }
            }
        }

    mock_cognito_idp.describe_user_pool = mock_describe_user_pool

    monkeypatch.setattr(
        EC2InstanceTypesDB,
        '_instance_type_names_from_botocore',
        MockInstanceTypes.get_instance_type_names,
    )
    monkeypatch.setattr(EvdiClient, 'publish_user_disabled_event', mock_function)

    def create_mock_boto_session(**_):
        return boto3.Session(
            aws_access_key_id='mock_access_key',
            aws_secret_access_key='mock_secret_access_key',
            region_name='us-east-1',
        )

    monkeypatch.setattr(Utils, 'create_boto_session', create_mock_boto_session)
    monkeypatch.setattr(
        AWSUtil, 'get_ec2_instance_type', MockInstanceTypes.get_instance_type
    )
    monkeypatch.setattr(AwsClientProvider, 's3', lambda *_: mock_s3_client)
    monkeypatch.setattr(AwsClientProvider, 'ec2', lambda *_: mock_ec2_client)
    monkeypatch.setattr(AwsClientProvider, 'dynamodb', lambda *_: mock_dynamodb_client)
    monkeypatch.setattr(
        AwsClientProvider, 'dynamodb_table', lambda *_: mock_dynamodb_table_resource
    )
    monkeypatch.setattr(AwsClientProvider, 'cognito_idp', lambda *_: mock_cognito_idp)

    mock_config = MockConfig()

    test_props = IdeaTestProps()

    monkeypatch.setenv('IDEA_DEV_MODE', 'true')

    test_dir = test_props.get_test_dir('cluster-manager-tests')
    monkeypatch.setenv('IDEA_APP_DEPLOY_DIR', test_dir)

    context = AppContext(
        options=SocaContextOptions(
            cluster_name='idea-mock',
            module_id='cluster-manager',
            module_name='cluster-manager',
            module_set='default',
            enable_aws_client_provider=True,
            enable_aws_util=True,
            use_vpc_endpoints=True,
            config=mock_config.get_config(),
        )
    )
    context.task_manager = TaskManager(context=context, tasks=[])
    monkeypatch.setattr(
        context.task_manager,
        'send',
        lambda *args, **kwargs: print(
            f'[TaskManager.send()] args: {args}, kwargs: {kwargs}'
        ),
    )

    context.ldap_client = MockLdapClient(context=context)
    user_pool = CognitoUserPool(
        context=context,
        options=CognitoUserPoolOptions(
            user_pool_id=context.config().get_string(
                'identity-provider.cognito.user_pool_id', required=True
            ),
            admin_group_name=context.config().get_string(
                'identity-provider.cognito.administrators_group_name', required=True
            ),
            client_id='mock-client-id',
            client_secret='mock-client-secret',
        ),
    )

    context.accounts = AccountsService(
        context=context,
        ldap_client=context.ldap_client,
        user_pool=user_pool,
        evdi_client=EvdiClient(context=context),
        task_manager=context.task_manager,
        token_service=None,
    )
    context.accounts.create_defaults()

    context.token_service = TokenService(
        context=context,
        options=TokenServiceOptions(
            cognito_user_pool_provider_url=context.config().get_string(
                'identity-provider.cognito.provider_url', required=True
            ),
            cognito_user_pool_domain_url=context.config().get_string(
                'identity-provider.cognito.domain_url', required=True
            ),
            client_id='mock-client-id',
            client_secret='mock-client-secret',
            client_credentials_scope=[],
        ),
    )

    context.projects = ProjectsService(
        context=context,
        accounts_service=context.accounts,
        task_manager=context.task_manager,
    )

    yield context

    print('cluster manager context clean-up ...')
    monkeypatch.undo()
