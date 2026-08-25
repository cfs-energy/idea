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

"""
The custom resource that clears the per-project permissions boundary before
CloudFormation deletes the policy.

Two properties matter and are asserted separately:
  - it clears the boundary only from roles that actually carry this policy
  - a delete never reports failure, because a failed delete is what makes a stack
    undeletable, which is the situation this resource exists to prevent
"""

import importlib.util
import os
import sys
import types

import pytest

BOUNDARY = 'arn:aws:iam::123456789012:policy/idea-test-us-east-2-cluster-manager-project-boundary'
OTHER_BOUNDARY = 'arn:aws:iam::123456789012:policy/some-other-boundary'
ROLE_PATH = '/idea/idea-test/projects/'


def _load_handler_module():
    """
    Load the lambda handler without pulling in the real idea_lambda_commons, which
    only exists inside the packaged lambda.
    """
    if 'idea_lambda_commons' not in sys.modules:
        commons = types.ModuleType('idea_lambda_commons')

        class CfnResponseStatus:
            SUCCESS = 'SUCCESS'
            FAILED = 'FAILED'

        class CfnResponse:
            def __init__(self, context, event, status, data, physical_resource_id):
                self.status = status
                self.data = data
                self.physical_resource_id = physical_resource_id

        class HttpClient:
            sent = []

            def send_cfn_response(self, response):
                HttpClient.sent.append(response)

        commons.CfnResponseStatus = CfnResponseStatus
        commons.CfnResponse = CfnResponse
        commons.HttpClient = HttpClient
        sys.modules['idea_lambda_commons'] = commons

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'resources',
        'lambda_functions',
        'idea_custom_resource_detach_project_boundaries',
        'handler.py',
    )
    spec = importlib.util.spec_from_file_location('detach_boundaries_handler', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handler_module = _load_handler_module()


class FakeNoSuchEntity(Exception):
    pass


class FakeIamClient:
    """
    Models the part of IAM that matters here: ListRoles does NOT return
    PermissionsBoundary, GetRole does. A fake that returns it from ListRoles is
    more generous than production and hides a handler that clears nothing.
    """

    class exceptions:
        NoSuchEntityException = FakeNoSuchEntity

    def __init__(self, roles, vanishing=()):
        self._roles = roles
        self._vanishing = set(vanishing)
        self.cleared = []
        self.path_prefixes = []
        self.got = []

    def get_paginator(self, name):
        assert name == 'list_roles'
        client = self

        class Paginator:
            def paginate(self, PathPrefix):  # noqa: N803 - boto3 kwarg name
                client.path_prefixes.append(PathPrefix)
                # exactly what the real ListRoles gives back: no boundary field
                return [{'Roles': [{'RoleName': r['RoleName']} for r in client._roles]}]

        return Paginator()

    def get_role(self, RoleName):  # noqa: N803
        self.got.append(RoleName)
        for role in self._roles:
            if role['RoleName'] == RoleName:
                if RoleName in self._vanishing:
                    raise FakeNoSuchEntity(RoleName)
                return {'Role': role}
        raise FakeNoSuchEntity(RoleName)

    def delete_role_permissions_boundary(self, RoleName):  # noqa: N803
        if RoleName in self._vanishing:
            raise FakeNoSuchEntity(RoleName)
        self.cleared.append(RoleName)


def role(name, boundary_arn=None):
    entry = {'RoleName': name}
    if boundary_arn is not None:
        entry['PermissionsBoundary'] = {'PermissionsBoundaryArn': boundary_arn}
    return entry


def test_clears_only_roles_carrying_this_boundary():
    client = FakeIamClient(
        [
            role('project-a', BOUNDARY),
            role('project-b', OTHER_BOUNDARY),
            role('project-c'),
            role('project-d', BOUNDARY),
        ]
    )
    cleared = handler_module.clear_boundaries(client, ROLE_PATH, BOUNDARY)
    assert cleared == 2
    assert client.cleared == ['project-a', 'project-d']


def test_scopes_the_listing_to_the_project_role_path():
    client = FakeIamClient([role('project-a', BOUNDARY)])
    handler_module.clear_boundaries(client, ROLE_PATH, BOUNDARY)
    assert client.path_prefixes == [ROLE_PATH]


def test_a_role_deleted_mid_sweep_is_not_an_error():
    client = FakeIamClient(
        [role('project-a', BOUNDARY), role('project-gone', BOUNDARY)],
        vanishing=['project-gone'],
    )
    cleared = handler_module.clear_boundaries(client, ROLE_PATH, BOUNDARY)
    assert cleared == 1
    assert client.cleared == ['project-a']


def test_the_boundary_is_read_with_get_role_not_list_roles():
    """
    Regression guard. ListRoles omits PermissionsBoundary, so a handler that reads
    it off the listing matches nothing and silently clears nothing - which is what
    shipped to dev27 before a live teardown test caught it.
    """
    client = FakeIamClient([role('project-a', BOUNDARY)])
    handler_module.clear_boundaries(client, ROLE_PATH, BOUNDARY)
    assert client.got == ['project-a'], 'boundary must be read via GetRole'
    assert client.cleared == ['project-a']


def test_nothing_to_clear_is_not_an_error():
    client = FakeIamClient([role('project-b', OTHER_BOUNDARY)])
    assert handler_module.clear_boundaries(client, ROLE_PATH, BOUNDARY) == 0
    assert client.cleared == []


@pytest.mark.parametrize('request_type', ['Create', 'Update'])
def test_create_and_update_do_not_touch_iam(request_type, monkeypatch):
    called = []
    monkeypatch.setattr(
        handler_module, 'clear_boundaries', lambda *a, **k: called.append(a)
    )
    sent = _run_handler(request_type)
    assert called == []
    assert sent.status == 'SUCCESS'


def test_delete_reports_success_even_when_the_sweep_fails(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError('iam is having a day')

    monkeypatch.setattr(handler_module, 'clear_boundaries', explode)
    sent = _run_handler('Delete')
    # a failed delete is what makes a stack undeletable; the policy delete that
    # follows will surface the real problem with its own error
    assert sent.status == 'SUCCESS'
    assert 'iam is having a day' in sent.data['error']


def test_delete_reports_how_many_were_cleared(monkeypatch):
    monkeypatch.setattr(handler_module, 'clear_boundaries', lambda *a, **k: 3)
    sent = _run_handler('Delete')
    assert sent.status == 'SUCCESS'
    assert sent.data == {'ClearedCount': '3'}


def _run_handler(request_type):
    http_client_cls = sys.modules['idea_lambda_commons'].HttpClient
    http_client_cls.sent = []
    handler_module.handler(
        {
            'RequestType': request_type,
            'ResourceProperties': {
                'RolePath': ROLE_PATH,
                'BoundaryPolicyArn': BOUNDARY,
            },
        },
        object(),
    )
    assert len(http_client_cls.sent) == 1
    return http_client_cls.sent[0]
