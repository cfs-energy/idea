from unittest.mock import Mock

import boto3
from botocore.stub import Stubber

from ideadatamodel import Project
from ideadatamodel.scheduler import GetUserApplicationsRequest
from ideascheduler.app.applications.hpc_applications_service import (
    HpcApplicationsService,
)


def test_user_applications_scan_paginates_with_real_client():
    resource = boto3.resource(
        'dynamodb',
        region_name='us-east-1',
        aws_access_key_id='testing',
        aws_secret_access_key='testing',
    )
    context = Mock()
    context.cluster_name.return_value = 'test'
    context.module_id.return_value = 'scheduler'
    context.aws.return_value.dynamodb_table.return_value = resource
    project = Project(project_id='project', name='project')
    context.projects_client.get_user_projects.return_value = [project]
    context.projects_client.get_project_by_id.return_value = project
    service = HpcApplicationsService(context)
    table_name = service.applications_dao.table.name
    with Stubber(resource.meta.client) as stub:
        stub.add_response(
            'scan',
            {
                'Items': [],
                'LastEvaluatedKey': {'application_id': {'S': 'hidden'}},
            },
            {'TableName': table_name},
        )
        stub.add_response(
            'scan',
            {
                'Items': [
                    {
                        'application_id': {'S': 'visible'},
                        'project_ids': {'L': [{'S': 'project'}]},
                        'created_on': {'N': '0'},
                        'updated_on': {'N': '0'},
                    }
                ]
            },
            {
                'TableName': table_name,
                'ExclusiveStartKey': {'application_id': 'hidden'},
            },
        )
        result = service.get_user_applications(
            GetUserApplicationsRequest(username='user')
        )
        assert [app.application_id for app in result.applications] == ['visible']
        assert result.applications[0].projects == [project]
        stub.assert_no_pending_responses()
