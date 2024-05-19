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

import pytest

from ideadatamodel import (
    SocaAnyPayload,
)
from ideascheduler import SchedulerAppContext
from ideasdk.context import SocaContextOptions
from ideasdk.utils import Utils
from ideasdk.aws import AWSUtil, EC2InstanceTypesDB, AwsClientProvider
from ideasdk.client import ProjectsClient, SocaClientOptions
from ideasdk.auth import TokenService, TokenServiceOptions

from ideatestutils import MockInstanceTypes, MockConfig, MockProjects
from ideatestutils import IdeaTestProps


@pytest.fixture()
def context(monkeypatch):
    """
    fixture to initialize context with mock config and aws clients
    goal is to ensure no network request are executed while executing unit tests
    """

    mock_boto_session = SocaAnyPayload()
    mock_boto_session.region_name = 'us-east-1'
    mock_boto_session.client = lambda **_: {}

    mock_ec2_client = SocaAnyPayload()
    mock_ec2_client.describe_security_groups = lambda **_: {}

    mock_s3_client = SocaAnyPayload()
    mock_s3_client.upload_file = lambda **_: {}
    mock_s3_client.get_bucket_acl = lambda **_: {}

    monkeypatch.setattr(EC2InstanceTypesDB, '_instance_type_names_from_botocore', MockInstanceTypes.get_instance_type_names)
    monkeypatch.setattr(Utils, 'create_boto_session', lambda **_: mock_boto_session)
    monkeypatch.setattr(AWSUtil, 'get_ec2_instance_type', MockInstanceTypes.get_instance_type)
    monkeypatch.setattr(AwsClientProvider, 's3', lambda *_: mock_s3_client)
    monkeypatch.setattr(AwsClientProvider, 'ec2', lambda *_: mock_ec2_client)
    monkeypatch.setattr(ProjectsClient, 'get_project', MockProjects.get_project)
    monkeypatch.setattr(ProjectsClient, 'get_user_projects', MockProjects.get_user_projects)

    mock_config = MockConfig()

    test_props = IdeaTestProps()

    monkeypatch.setenv('IDEA_DEV_MODE', 'true')

    test_dir = test_props.get_test_dir('scheduler-tests')
    monkeypatch.setenv('IDEA_APP_DEPLOY_DIR', test_dir)

    context = SchedulerAppContext(
        options=SocaContextOptions(
            cluster_name='idea-mock',
            module_id='scheduler',
            module_name='scheduler',
            module_set='default',
            enable_aws_client_provider=True,
            enable_aws_util=True,
            config=mock_config.get_config()
        )
    )

    context.token_service = TokenService(
        context=context,
        options=TokenServiceOptions(
            cognito_user_pool_provider_url=context.config().get_string('identity-provider.cognito.provider_url', required=True),
            cognito_user_pool_domain_url=context.config().get_string('identity-provider.cognito.domain_url', required=True),
            client_id='mock-client-id',
            client_secret='mock-client-secret',
            client_credentials_scope=[]
        )
    )

    context.projects_client = ProjectsClient(
        context=context,
        options=SocaClientOptions(
            endpoint='http://localhost'
        ),
        token_service=context.token_service
    )

    return context
