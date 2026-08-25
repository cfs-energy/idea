#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the 'License'). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

from idea_lambda_commons import HttpClient, CfnResponse, CfnResponseStatus
import boto3
import logging

logging.getLogger().setLevel(logging.INFO)

PHYSICAL_RESOURCE_ID = 'project-role-boundaries'


def clear_boundaries(iam_client, role_path: str, boundary_arn: str) -> int:
    """
    Drop the boundary reference from every project role still carrying it.

    ListRoles does not return PermissionsBoundary - only GetRole does - so the
    boundary has to be read per role. Filtering on the ListRoles payload silently
    matches nothing and clears nothing.

    Roles that disappear mid-run are ignored: the reference is gone either way.
    """
    cleared = 0
    paginator = iam_client.get_paginator('list_roles')
    for page in paginator.paginate(PathPrefix=role_path):
        for listed in page.get('Roles', []):
            role_name = listed.get('RoleName')
            try:
                role = iam_client.get_role(RoleName=role_name).get('Role', {})
            except iam_client.exceptions.NoSuchEntityException:
                logging.info(f'role no longer present, nothing to clear: {role_name}')
                continue
            boundary = role.get('PermissionsBoundary') or {}
            if boundary.get('PermissionsBoundaryArn') != boundary_arn:
                continue
            try:
                iam_client.delete_role_permissions_boundary(RoleName=role_name)
                cleared += 1
                logging.info(f'cleared project boundary from role: {role_name}')
            except iam_client.exceptions.NoSuchEntityException:
                logging.info(f'role no longer present, nothing to clear: {role_name}')
    return cleared


def handler(event, context):
    """
    Clear the per-project permissions boundary before CloudFormation deletes it.

    The boundary policy is a stack resource, but the project roles that reference it
    are created at runtime. IAM refuses to delete a policy that is still in use as a
    boundary, so the references have to go first or the stack cannot be torn down.
    """
    http_client = HttpClient()
    request_type = event.get('RequestType')
    data = {}

    if request_type == 'Delete':
        # Never fail this one. Reporting failure on delete is what makes a stack
        # undeletable, which is the problem this resource exists to prevent. If the
        # sweep is incomplete the policy delete fails afterwards with its own error.
        try:
            logging.info(f'ReceivedEvent: {event}')
            properties = event['ResourceProperties']
            cleared = clear_boundaries(
                boto3.client('iam'),
                properties['RolePath'],
                properties['BoundaryPolicyArn'],
            )
            logging.info(f'cleared project boundary from {cleared} role(s)')
            data = {'ClearedCount': str(cleared)}
        except Exception as e:
            logging.exception(f'Failed to clear project role boundaries: {e}')
            data = {'error': str(e)}

    http_client.send_cfn_response(
        CfnResponse(
            context=context,
            event=event,
            status=CfnResponseStatus.SUCCESS,
            data=data,
            physical_resource_id=PHYSICAL_RESOURCE_ID,
        )
    )
