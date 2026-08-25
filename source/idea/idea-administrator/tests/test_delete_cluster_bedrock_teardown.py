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

"""
delete-cluster removal of the per project bedrock resources the cluster-manager
provisions at runtime. cloudformation does not own them, so the stack delete
leaves them behind unless this sweep runs first.
"""

import botocore.exceptions

from ideaadministrator.app.delete_cluster import delete_bedrock_project_resources

CLUSTER_NAME = 'idea-test'
ACCOUNT_ID = '123456789012'
PATH = f'/idea/{CLUSTER_NAME}/projects/'
ROLE_NAME = f'{CLUSTER_NAME}-p1-project'
ROLE_ARN = f'arn:aws:iam::{ACCOUNT_ID}:role{PATH}{ROLE_NAME}'
PROFILE_NAME = f'{CLUSTER_NAME}-p1-project'
PROFILE_ARN = f'arn:aws:iam::{ACCOUNT_ID}:instance-profile{PATH}{PROFILE_NAME}'
PROJECT_POLICY_ARN = f'arn:aws:iam::{ACCOUNT_ID}:policy{PATH}{CLUSTER_NAME}-p1-bedrock'
OTHER_POLICY_ARN = f'arn:aws:iam::{ACCOUNT_ID}:policy/other/not-ours'
SSM_POLICY_ARN = 'arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'
INFERENCE_PROFILE_ARN = (
    f'arn:aws:bedrock:us-east-2:{ACCOUNT_ID}:application-inference-profile/aip-1'
)


def not_found(operation):
    return botocore.exceptions.ClientError(
        {'Error': {'Code': 'NoSuchEntity', 'Message': 'gone'}}, operation
    )


class FakeIam:
    def __init__(self, roles=None, instance_profiles=None, policies=None):
        self.calls = []
        self.roles = dict(roles or {})
        self.instance_profiles = dict(instance_profiles or {})
        self.policies = dict(policies or {})

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def list_attached_role_policies(self, RoleName):
        self._record('list_attached_role_policies', RoleName=RoleName)
        if RoleName not in self.roles:
            raise not_found('ListAttachedRolePolicies')
        return {
            'AttachedPolicies': [{'PolicyArn': arn} for arn in self.roles[RoleName]]
        }

    def detach_role_policy(self, RoleName, PolicyArn):
        self._record('detach_role_policy', RoleName=RoleName, PolicyArn=PolicyArn)
        self.roles[RoleName].remove(PolicyArn)

    def get_instance_profile(self, InstanceProfileName):
        self._record('get_instance_profile', InstanceProfileName=InstanceProfileName)
        if InstanceProfileName not in self.instance_profiles:
            raise not_found('GetInstanceProfile')
        return {
            'InstanceProfile': {
                'Roles': [
                    {'RoleName': name}
                    for name in self.instance_profiles[InstanceProfileName]
                ]
            }
        }

    def remove_role_from_instance_profile(self, InstanceProfileName, RoleName):
        self._record(
            'remove_role_from_instance_profile',
            InstanceProfileName=InstanceProfileName,
            RoleName=RoleName,
        )
        self.instance_profiles[InstanceProfileName].remove(RoleName)

    def delete_instance_profile(self, InstanceProfileName):
        self._record('delete_instance_profile', InstanceProfileName=InstanceProfileName)
        if InstanceProfileName not in self.instance_profiles:
            raise not_found('DeleteInstanceProfile')
        del self.instance_profiles[InstanceProfileName]

    def delete_role(self, RoleName):
        self._record('delete_role', RoleName=RoleName)
        if RoleName not in self.roles:
            raise not_found('DeleteRole')
        del self.roles[RoleName]

    def get_paginator(self, name):
        assert name == 'list_policies'
        client = self

        class Paginator:
            def paginate(self, PathPrefix, Scope):
                client._record('list_policies', PathPrefix=PathPrefix, Scope=Scope)
                return [
                    {
                        'Policies': [
                            {'Arn': arn}
                            for arn in sorted(client.policies)
                            if arn.split(':policy')[-1].startswith(PathPrefix)
                        ]
                    }
                ]

        return Paginator()

    def list_policy_versions(self, PolicyArn):
        self._record('list_policy_versions', PolicyArn=PolicyArn)
        return {
            'Versions': [
                {'VersionId': version_id, 'IsDefaultVersion': version_id == 'v1'}
                for version_id in self.policies[PolicyArn]
            ]
        }

    def delete_policy_version(self, PolicyArn, VersionId):
        self._record('delete_policy_version', PolicyArn=PolicyArn, VersionId=VersionId)
        self.policies[PolicyArn].remove(VersionId)

    def delete_policy(self, PolicyArn):
        self._record('delete_policy', PolicyArn=PolicyArn)
        del self.policies[PolicyArn]


class FakeBedrock:
    def __init__(self, profiles=None):
        self.calls = []
        self.profiles = set(profiles or [])

    def delete_inference_profile(self, inferenceProfileIdentifier):
        self.calls.append(('delete_inference_profile', inferenceProfileIdentifier))
        if inferenceProfileIdentifier not in self.profiles:
            raise botocore.exceptions.ClientError(
                {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'gone'}},
                'DeleteInferenceProfile',
            )
        self.profiles.remove(inferenceProfileIdentifier)


def project(bedrock=None):
    return {
        'project_id': 'p1',
        'name': 'research',
        'bedrock': {
            'enabled': True,
            'model_ids': ['us.vendor-a.model-1'],
            'role_arn': ROLE_ARN,
            'instance_profile_arn': PROFILE_ARN,
            'inference_profile_arns': {'us.vendor-a.model-1': INFERENCE_PROFILE_ARN},
        }
        if bedrock is None
        else bedrock,
    }


def provisioned_iam():
    return FakeIam(
        roles={ROLE_NAME: [PROJECT_POLICY_ARN, SSM_POLICY_ARN]},
        instance_profiles={PROFILE_NAME: [ROLE_NAME]},
        policies={PROJECT_POLICY_ARN: ['v1', 'v2'], OTHER_POLICY_ARN: ['v1']},
    )


def call_names(client):
    return [name for name, _ in client.calls]


def test_deletes_in_dependency_order():
    iam = provisioned_iam()
    bedrock = FakeBedrock(profiles=['aip-1'])
    logged = []

    delete_bedrock_project_resources(
        iam, bedrock, CLUSTER_NAME, [project()], log=logged.append
    )

    assert call_names(iam) == [
        'list_attached_role_policies',
        'detach_role_policy',
        'detach_role_policy',
        'get_instance_profile',
        'remove_role_from_instance_profile',
        'delete_instance_profile',
        'delete_role',
        'list_policies',
        'list_policy_versions',
        'delete_policy_version',
        'delete_policy',
    ]
    assert iam.roles == {}
    assert iam.instance_profiles == {}
    # the policy outside the cluster path is left alone
    assert iam.policies == {OTHER_POLICY_ARN: ['v1']}
    assert bedrock.calls == [('delete_inference_profile', 'aip-1')]
    assert bedrock.profiles == set()
    assert any(f'deleted role {ROLE_NAME}' == line for line in logged)


def test_resources_already_gone_are_skipped_not_raised():
    iam = FakeIam(policies={PROJECT_POLICY_ARN: ['v1']})
    bedrock = FakeBedrock(profiles=[])
    logged = []

    delete_bedrock_project_resources(
        iam, bedrock, CLUSTER_NAME, [project()], log=logged.append
    )

    # the policy under the cluster path is still removed after the missing role
    assert iam.policies == {}
    assert 'delete_policy' in call_names(iam)
    assert bedrock.calls == [('delete_inference_profile', 'aip-1')]
    assert any('not found, skipped' in line for line in logged)


def test_a_project_without_provisioned_resources_touches_no_iam_entities():
    iam = FakeIam()
    bedrock = FakeBedrock()

    delete_bedrock_project_resources(
        iam,
        bedrock,
        CLUSTER_NAME,
        [project(bedrock={'enabled': True, 'model_ids': []})],
        log=lambda *_: None,
    )

    assert call_names(iam) == ['list_policies']
    assert bedrock.calls == []


def test_the_policy_sweep_is_scoped_to_the_cluster_path():
    iam = FakeIam()

    delete_bedrock_project_resources(
        iam, FakeBedrock(), CLUSTER_NAME, [], log=lambda *_: None
    )

    assert iam.calls == [('list_policies', {'PathPrefix': PATH, 'Scope': 'Local'})]
