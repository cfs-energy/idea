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
Test Cases for the bedrock project provisioner

The provisioner is driven against recording iam and bedrock clients and a stub
projects dao, so policy shape, subset enforcement, idempotency and teardown are
exercised without DynamoDB or AWS.
"""

from ideaclustermanager.app.projects.bedrock_provisioner import (
    BedrockProvisioner,
    build_policy_document,
    build_resource_name,
    get_actions_outside_boundary,
    get_allowed_actions,
    is_model_supported_in_partition,
    is_model_supported_in_region,
    validate_no_global_profiles,
)
from ideaclustermanager.app.projects.db.projects_dao import ProjectsDAO

from ideadatamodel import exceptions

import botocore.exceptions
import pytest

CLUSTER_NAME = 'idea-test'
MODULE_ID = 'cluster-manager'
REGION = 'us-east-2'
ACCOUNT_ID = '123456789012'
PARTITION = 'aws'
PROJECT_ID = 'a1b2c3d4-0000-4000-8000-000000000001'

MODEL_A = 'us.vendor-a.model-1'
MODEL_B = 'vendor-b.model-9'
CATALOG = [MODEL_A, MODEL_B]

BOUNDARY_ARN = (
    f'arn:{PARTITION}:iam::{ACCOUNT_ID}:policy/'
    f'{CLUSTER_NAME}-{REGION}-{MODULE_ID}-project-boundary'
)
BASE_POLICY_ARN = (
    f'arn:{PARTITION}:iam::{ACCOUNT_ID}:policy/{CLUSTER_NAME}-{REGION}-vdc-host'
)
SSM_POLICY_ARN = f'arn:{PARTITION}:iam::aws:policy/AmazonSSMManagedInstanceCore'
CW_POLICY_ARN = f'arn:{PARTITION}:iam::aws:policy/CloudWatchAgentServerPolicy'

CONFIG_VALUES = {
    'cluster.cluster_name': CLUSTER_NAME,
    'cluster.aws.region': REGION,
    'cluster.aws.account_id': ACCOUNT_ID,
    'cluster.aws.partition': PARTITION,
    'cluster.aws.dns_suffix': 'amazonaws.com',
    'cluster.iam.policies.amazon_ssm_managed_instance_core_arn': SSM_POLICY_ARN,
    'cluster.iam.policies.cloud_watch_agent_server_arn': CW_POLICY_ARN,
    'cluster.iam.ec2_managed_policy_arns': [],
    'virtual-desktop-controller.dcv_host_policy_arn': BASE_POLICY_ARN,
    'global-settings.custom_tags': [],
    f'{MODULE_ID}.bedrock.enabled': True,
    f'{MODULE_ID}.bedrock.model_ids': CATALOG,
}


def no_such_entity(operation: str):
    return botocore.exceptions.ClientError(
        {'Error': {'Code': 'NoSuchEntity', 'Message': 'not found'}}, operation
    )


def access_denied(operation: str):
    return botocore.exceptions.ClientError(
        {'Error': {'Code': 'AccessDenied', 'Message': 'denied'}}, operation
    )


def validation_error(operation: str, message: str = 'model is not available'):
    return botocore.exceptions.ClientError(
        {'Error': {'Code': 'ValidationException', 'Message': message}}, operation
    )


class FakeConfig:
    def __init__(self, values):
        self.values = dict(values)

    def _get(self, key, default, required):
        value = self.values.get(key, default)
        if required and value is None:
            raise KeyError(key)
        return value

    def get_string(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_bool(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_list(self, key, default=None, required=False, module_id=None):
        return self._get(key, default, required)

    def get_module_id(self, module_name):
        return {'virtual-desktop-controller': 'virtual-desktop-controller'}.get(
            module_name, module_name
        )


class FakeLogger:
    def info(self, *args, **kwargs): ...

    def debug(self, *args, **kwargs): ...

    def warning(self, *args, **kwargs): ...

    def error(self, *args, **kwargs): ...


class RecordingLogger(FakeLogger):
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message, *args, **kwargs):
        self.errors.append(message)

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)


class FakeIam:
    def __init__(self):
        self.calls = []
        self.log = []
        self.roles = {}
        self.policies = {}
        self.instance_profiles = {}
        self.attached = {}

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        self.log.append((name, kwargs))

    def get_policy(self, PolicyArn):
        self._record('get_policy', PolicyArn=PolicyArn)
        if PolicyArn not in self.policies:
            raise no_such_entity('GetPolicy')
        return {'Policy': {'Arn': PolicyArn}}

    def create_policy(self, PolicyName, Path, PolicyDocument, Tags=None):
        self._record(
            'create_policy',
            PolicyName=PolicyName,
            Path=Path,
            Tags=Tags,
            PolicyDocument=PolicyDocument,
        )
        arn = f'arn:{PARTITION}:iam::{ACCOUNT_ID}:policy{Path}{PolicyName}'
        self.policies[arn] = {'versions': {'v1': PolicyDocument}, 'default': 'v1'}
        return {'Policy': {'Arn': arn}}

    def list_policy_versions(self, PolicyArn):
        self._record('list_policy_versions', PolicyArn=PolicyArn)
        if PolicyArn not in self.policies:
            raise no_such_entity('ListPolicyVersions')
        policy = self.policies[PolicyArn]
        return {
            'Versions': [
                {
                    'VersionId': version_id,
                    'IsDefaultVersion': version_id == policy['default'],
                }
                for version_id in policy['versions']
            ]
        }

    def get_policy_version(self, PolicyArn, VersionId):
        self._record('get_policy_version', PolicyArn=PolicyArn, VersionId=VersionId)
        return {
            'PolicyVersion': {
                'Document': self.policies[PolicyArn]['versions'][VersionId]
            }
        }

    def create_policy_version(self, PolicyArn, PolicyDocument, SetAsDefault):
        self._record(
            'create_policy_version', PolicyArn=PolicyArn, SetAsDefault=SetAsDefault
        )
        policy = self.policies[PolicyArn]
        version_id = f'v{len(policy["versions"]) + 1}'
        policy['versions'][version_id] = PolicyDocument
        if SetAsDefault:
            policy['default'] = version_id
        return {'PolicyVersion': {'VersionId': version_id}}

    def delete_policy_version(self, PolicyArn, VersionId):
        self._record('delete_policy_version', PolicyArn=PolicyArn, VersionId=VersionId)
        self.policies[PolicyArn]['versions'].pop(VersionId, None)

    def delete_policy(self, PolicyArn):
        self._record('delete_policy', PolicyArn=PolicyArn)
        if PolicyArn not in self.policies:
            raise no_such_entity('DeletePolicy')
        del self.policies[PolicyArn]

    def get_role(self, RoleName):
        self._record('get_role', RoleName=RoleName)
        if RoleName not in self.roles:
            raise no_such_entity('GetRole')
        return {'Role': self.roles[RoleName]}

    def create_role(
        self,
        Path,
        RoleName,
        Description,
        AssumeRolePolicyDocument,
        PermissionsBoundary,
        Tags,
    ):
        self._record(
            'create_role',
            Path=Path,
            RoleName=RoleName,
            PermissionsBoundary=PermissionsBoundary,
            Tags=Tags,
            AssumeRolePolicyDocument=AssumeRolePolicyDocument,
        )
        arn = f'arn:{PARTITION}:iam::{ACCOUNT_ID}:role{Path}{RoleName}'
        self.roles[RoleName] = {
            'Arn': arn,
            'PermissionsBoundary': {'PermissionsBoundaryArn': PermissionsBoundary},
        }
        self.attached[RoleName] = set()
        return {'Role': self.roles[RoleName]}

    def put_role_permissions_boundary(self, RoleName, PermissionsBoundary):
        self._record(
            'put_role_permissions_boundary',
            RoleName=RoleName,
            PermissionsBoundary=PermissionsBoundary,
        )
        self.roles[RoleName]['PermissionsBoundary'] = {
            'PermissionsBoundaryArn': PermissionsBoundary
        }

    def tag_role(self, RoleName, Tags):
        self._record('tag_role', RoleName=RoleName, Tags=Tags)
        if RoleName not in self.roles:
            raise no_such_entity('TagRole')
        self.roles[RoleName]['Tags'] = Tags

    def delete_role(self, RoleName):
        self._record('delete_role', RoleName=RoleName)
        if RoleName not in self.roles:
            raise no_such_entity('DeleteRole')
        del self.roles[RoleName]
        self.attached.pop(RoleName, None)

    def list_attached_role_policies(self, RoleName):
        self._record('list_attached_role_policies', RoleName=RoleName)
        if RoleName not in self.roles:
            raise no_such_entity('ListAttachedRolePolicies')
        return {
            'AttachedPolicies': [
                {'PolicyArn': arn} for arn in sorted(self.attached[RoleName])
            ]
        }

    def attach_role_policy(self, RoleName, PolicyArn):
        self._record('attach_role_policy', RoleName=RoleName, PolicyArn=PolicyArn)
        self.attached[RoleName].add(PolicyArn)

    def detach_role_policy(self, RoleName, PolicyArn):
        self._record('detach_role_policy', RoleName=RoleName, PolicyArn=PolicyArn)
        self.attached[RoleName].discard(PolicyArn)

    def get_instance_profile(self, InstanceProfileName):
        self._record('get_instance_profile', InstanceProfileName=InstanceProfileName)
        if InstanceProfileName not in self.instance_profiles:
            raise no_such_entity('GetInstanceProfile')
        return {'InstanceProfile': self.instance_profiles[InstanceProfileName]}

    def create_instance_profile(self, InstanceProfileName, Path, Tags=None):
        self._record(
            'create_instance_profile',
            InstanceProfileName=InstanceProfileName,
            Path=Path,
            Tags=Tags,
        )
        self.instance_profiles[InstanceProfileName] = {
            'Arn': f'arn:{PARTITION}:iam::{ACCOUNT_ID}:instance-profile{Path}{InstanceProfileName}',
            'Roles': [],
        }
        return {'InstanceProfile': self.instance_profiles[InstanceProfileName]}

    def add_role_to_instance_profile(self, InstanceProfileName, RoleName):
        self._record(
            'add_role_to_instance_profile',
            InstanceProfileName=InstanceProfileName,
            RoleName=RoleName,
        )
        self.instance_profiles[InstanceProfileName]['Roles'].append(
            {'RoleName': RoleName}
        )

    def remove_role_from_instance_profile(self, InstanceProfileName, RoleName):
        self._record(
            'remove_role_from_instance_profile',
            InstanceProfileName=InstanceProfileName,
            RoleName=RoleName,
        )
        roles = self.instance_profiles[InstanceProfileName]['Roles']
        self.instance_profiles[InstanceProfileName]['Roles'] = [
            role for role in roles if role['RoleName'] != RoleName
        ]

    def delete_instance_profile(self, InstanceProfileName):
        self._record('delete_instance_profile', InstanceProfileName=InstanceProfileName)
        if InstanceProfileName not in self.instance_profiles:
            raise no_such_entity('DeleteInstanceProfile')
        del self.instance_profiles[InstanceProfileName]


class PathScopedIam(FakeIam):
    """
    the deployed grant is scoped to the project role path. iam authorizes a name
    addressed read of an absent entity against the root path arn, so the read is
    denied rather than answered with NoSuchEntity.
    """

    def get_role(self, RoleName):
        if RoleName not in self.roles:
            self._record('get_role', RoleName=RoleName)
            raise access_denied('GetRole')
        return super().get_role(RoleName)

    def get_instance_profile(self, InstanceProfileName):
        if InstanceProfileName not in self.instance_profiles:
            self._record(
                'get_instance_profile', InstanceProfileName=InstanceProfileName
            )
            raise access_denied('GetInstanceProfile')
        return super().get_instance_profile(InstanceProfileName)


class FakeBedrock:
    def __init__(
        self,
        routed_regions=None,
        on_demand_models=None,
        system_profiles=None,
        fail_models=None,
    ):
        self.fail_models = set(fail_models or [])
        self.calls = []
        self.log = []
        self.profiles = {}
        self.routed_regions = routed_regions or [REGION, 'us-east-1']
        # base ids invocable directly. anything else must resolve to a system profile.
        self.on_demand_models = (
            {MODEL_B} if on_demand_models is None else set(on_demand_models)
        )
        self.system_profiles = system_profiles or {}

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        self.log.append((name, kwargs))

    def list_inference_profiles(self, typeEquals=None, nextToken=None):
        self._record('list_inference_profiles', typeEquals=typeEquals)
        if typeEquals == 'SYSTEM_DEFINED':
            return {
                'inferenceProfileSummaries': [
                    {
                        'inferenceProfileId': profile_id,
                        'inferenceProfileArn': (
                            f'arn:{PARTITION}:bedrock:{REGION}:{ACCOUNT_ID}:'
                            f'inference-profile/{profile_id}'
                        ),
                        'models': [{'modelArn': arn} for arn in model_arns],
                    }
                    for profile_id, model_arns in self.system_profiles.items()
                ]
            }
        return {
            'inferenceProfileSummaries': [
                {
                    'inferenceProfileName': name,
                    'inferenceProfileArn': profile['arn'],
                    'inferenceProfileId': profile['id'],
                }
                for name, profile in self.profiles.items()
            ]
        }

    def get_foundation_model(self, modelIdentifier):
        self._record('get_foundation_model', modelIdentifier=modelIdentifier)
        if modelIdentifier in self.on_demand_models:
            return {'modelDetails': {'inferenceTypesSupported': ['ON_DEMAND']}}
        return {'modelDetails': {'inferenceTypesSupported': ['INFERENCE_PROFILE']}}

    def tag_resource(self, resourceARN, tags):
        self._record('tag_resource', resourceARN=resourceARN, tags=tags)
        return {}

    def create_inference_profile(self, inferenceProfileName, modelSource, tags):
        self._record(
            'create_inference_profile',
            inferenceProfileName=inferenceProfileName,
            modelSource=modelSource,
            tags=tags,
        )
        source_model = modelSource['copyFrom'].split('/')[-1]
        if source_model in self.fail_models:
            raise validation_error(
                'create_inference_profile', f'{source_model} is not available'
            )
        profile_id = f'aip-{len(self.profiles) + 1}'
        arn = (
            f'arn:{PARTITION}:bedrock:{REGION}:{ACCOUNT_ID}:'
            f'application-inference-profile/{profile_id}'
        )
        model_id = modelSource['copyFrom'].split('/')[-1]
        if model_id.startswith('us.'):
            model_id = model_id[len('us.') :]
        self.profiles[inferenceProfileName] = {
            'arn': arn,
            'id': profile_id,
            'models': [
                {
                    'modelArn': f'arn:{PARTITION}:bedrock:{region}::foundation-model/{model_id}'
                }
                for region in self.routed_regions
            ],
        }
        return {'inferenceProfileArn': arn, 'inferenceProfileId': profile_id}

    def get_inference_profile(self, inferenceProfileIdentifier):
        self._record(
            'get_inference_profile',
            inferenceProfileIdentifier=inferenceProfileIdentifier,
        )
        for profile in self.profiles.values():
            if inferenceProfileIdentifier in (profile['arn'], profile['id']):
                return {'models': profile['models']}
        raise no_such_entity('GetInferenceProfile')

    def delete_inference_profile(self, inferenceProfileIdentifier):
        self._record(
            'delete_inference_profile',
            inferenceProfileIdentifier=inferenceProfileIdentifier,
        )
        for name, profile in list(self.profiles.items()):
            if inferenceProfileIdentifier in (profile['arn'], profile['id']):
                del self.profiles[name]
                return {}
        raise no_such_entity('DeleteInferenceProfile')


class FakeProjectsDAO:
    def __init__(self, db_project):
        self.db_project = db_project
        self.updates = []

    def get_project_by_id(self, project_id):
        if self.db_project is None:
            return None
        return dict(self.db_project)

    @staticmethod
    def convert_from_db(project):
        return ProjectsDAO.convert_from_db(project)

    def update_project(self, project):
        self.updates.append(project)
        self.db_project = {**self.db_project, **project}
        return self.db_project


class FakeEc2:
    """describe_instances through a paginator, answering with the instances given."""

    def __init__(self, instance_ids=None, error=None):
        self.instance_ids = list(instance_ids or [])
        self.error = error
        self.filters = []

    def get_paginator(self, operation_name):
        assert operation_name == 'describe_instances'
        return self

    def paginate(self, Filters=None):
        self.filters.append(Filters)
        if self.error is not None:
            raise self.error
        return [
            {
                'Reservations': [
                    {'Instances': [{'InstanceId': i} for i in self.instance_ids]}
                ]
            }
        ]


class FakeAws:
    def __init__(self, iam, bedrock, ec2=None):
        self._iam = iam
        self._bedrock = bedrock
        self._ec2 = ec2 or FakeEc2()

    def iam(self):
        return self._iam

    def bedrock(self):
        return self._bedrock

    def ec2(self):
        return self._ec2


class FakeContext:
    def __init__(self, config, aws):
        self._config = config
        self._aws = aws

    def config(self):
        return self._config

    def aws(self):
        return self._aws

    def cluster_name(self):
        return CLUSTER_NAME

    def module_id(self):
        return MODULE_ID

    def logger(self, name=None):
        return FakeLogger()


def db_project(enabled=True, bedrock_enabled=True, model_ids=None, bedrock=None):
    if bedrock is None:
        bedrock = {
            'enabled': bedrock_enabled,
            'model_ids': CATALOG if model_ids is None else model_ids,
        }
    return {
        'project_id': PROJECT_ID,
        'name': 'research',
        'title': 'Research',
        'enabled': enabled,
        'bedrock': bedrock,
        'tags': {'idea:CostCenter': 'cc-1'},
        'created_on': 1,
        'updated_on': 1,
    }


def build_provisioner(
    project=None, config_values=None, boundary=True, bedrock=None, iam=None, ec2=None
):
    config = FakeConfig({**CONFIG_VALUES, **(config_values or {})})
    iam = iam or FakeIam()
    if boundary:
        iam.policies[BOUNDARY_ARN] = {'versions': {'v1': {}}, 'default': 'v1'}
    bedrock_client = bedrock or FakeBedrock()
    dao = FakeProjectsDAO(db_project() if project is None else project)
    shared_log = []
    iam.log = shared_log
    bedrock_client.log = shared_log
    dao.log = shared_log
    context = FakeContext(config, FakeAws(iam, bedrock_client, ec2))
    return (
        BedrockProvisioner(context=context, projects_dao=dao),
        iam,
        bedrock_client,
        dao,
    )


def call_names(client):
    return [name for name, _ in client.calls]


def kwargs_for(client, name):
    return [kwargs for called, kwargs in client.calls if called == name]


# policy document shape


def test_policy_document_statement_pair():
    document = build_policy_document(
        ['arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/aip-1'],
        [
            'arn:aws:bedrock:us-east-2::foundation-model/m',
            'arn:aws:bedrock:us-east-1::foundation-model/m',
        ],
    )
    assert document['Version'] == '2012-10-17'

    by_sid = {statement['Sid']: statement for statement in document['Statement']}
    profile_statement = by_sid['InvokeProjectInferenceProfiles']
    model_statement = by_sid['InvokeFoundationModelsThroughProjectProfiles']
    assert profile_statement['Effect'] == 'Allow'
    assert profile_statement['Action'] == [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
    ]
    assert profile_statement['Resource'] == [
        'arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/aip-1'
    ]
    assert 'Condition' not in profile_statement

    assert model_statement['Action'] == profile_statement['Action']
    assert model_statement['Resource'] == [
        'arn:aws:bedrock:us-east-1::foundation-model/m',
        'arn:aws:bedrock:us-east-2::foundation-model/m',
    ]
    assert model_statement['Condition'] == {
        'StringEquals': {
            'bedrock:InferenceProfileArn': [
                'arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/aip-1'
            ]
        }
    }


def test_policy_document_has_no_source_system_profile_resource():
    """
    invoke has to go through the application profile or the profile's cost allocation tags are
    bypassed. Read-only discovery is exempt: ListInferenceProfiles is a collection action.
    """
    document = build_policy_document(
        ['arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/aip-1'],
        ['arn:aws:bedrock:us-east-2::foundation-model/m'],
    )
    invoke_resources = [
        resource
        for statement in document['Statement']
        if statement['Sid'].startswith('Invoke')
        for resource in statement['Resource']
    ]
    assert len(invoke_resources) > 0
    assert not any(':inference-profile/' in resource for resource in invoke_resources)


def test_policy_document_omits_model_statement_without_models():
    document = build_policy_document(['arn:aws:bedrock:r:a:x/1'], [])
    sids = {statement['Sid'] for statement in document['Statement']}
    assert 'InvokeFoundationModelsThroughProjectProfiles' not in sids
    assert 'InvokeProjectInferenceProfiles' in sids


# partition rules


@pytest.mark.parametrize(
    'partition, model_id, supported',
    [
        ('aws', 'vendor-a.model-1', True),
        ('aws', 'us.vendor-a.model-1', True),
        ('aws', 'global.vendor-a.model-1', False),
        ('aws', 'us-gov.vendor-a.model-1', False),
        ('aws-us-gov', 'vendor-a.model-1', True),
        ('aws-us-gov', 'us-gov.vendor-a.model-1', True),
        ('aws-us-gov', 'us.vendor-a.model-1', False),
        ('aws-us-gov', 'eu.vendor-a.model-1', False),
        ('aws-us-gov', 'global.vendor-a.model-1', False),
    ],
)
def test_partition_model_support(partition, model_id, supported):
    assert is_model_supported_in_partition(partition, model_id) is supported


def test_a_globally_routing_profile_is_dropped_in_a_commercial_partition():
    # the regionless arn is what a global profile routes to. granting it would
    # allow an invoke routed outside the geography.
    bedrock = FakeBedrock()
    provisioner, _, _, _ = build_provisioner(bedrock=bedrock)
    profile_arn = f'arn:{PARTITION}:bedrock:{REGION}:1:application-inference-profile/1'
    bedrock.profiles['p'] = {
        'arn': profile_arn,
        'id': '1',
        'models': [{'modelArn': f'arn:{PARTITION}:bedrock:::foundation-model/m'}],
    }
    kept, model_arns, rejected = provisioner._resolve_foundation_model_arns(
        PARTITION, {MODEL_B: profile_arn}
    )
    assert kept == {}
    assert model_arns == []
    assert rejected == [profile_arn]


def test_gov_partition_drops_a_globally_routing_profile():
    # dropped rather than raised: raising after the profile was created left an
    # unrecorded profile behind and failed the same way on every later run.
    bedrock = FakeBedrock()
    provisioner, _, _, _ = build_provisioner(
        config_values={'cluster.aws.partition': 'aws-us-gov'}, bedrock=bedrock
    )
    profile_arn = (
        'arn:aws-us-gov:bedrock:us-gov-west-1:1:application-inference-profile/1'
    )
    bedrock.profiles['p'] = {
        'arn': profile_arn,
        'id': '1',
        'models': [{'modelArn': 'arn:aws-us-gov:bedrock:::foundation-model/m'}],
    }
    kept, model_arns, rejected = provisioner._resolve_foundation_model_arns(
        'aws-us-gov', {MODEL_B: profile_arn}
    )
    assert kept == {}
    assert model_arns == []
    assert rejected == [profile_arn]


def test_a_profile_routing_to_no_models_is_dropped():
    # both the profile arn and the foundation model arns are required for an
    # invoke, so a profile-only statement would deny every call.
    bedrock = FakeBedrock()
    provisioner, _, _, _ = build_provisioner(bedrock=bedrock)
    profile_arn = f'arn:{PARTITION}:bedrock:{REGION}:1:application-inference-profile/1'
    bedrock.profiles['p'] = {'arn': profile_arn, 'id': '1', 'models': []}
    kept, model_arns, rejected = provisioner._resolve_foundation_model_arns(
        PARTITION, {MODEL_B: profile_arn}
    )
    assert kept == {}
    assert rejected == [profile_arn]


def policy_documents(iam):
    return [
        document
        for policy in iam.policies.values()
        for document in policy['versions'].values()
        if isinstance(document, str)
    ]


def test_the_policy_never_grants_the_regionless_foundation_model_arn():
    bedrock = FakeBedrock(routed_regions=[''])
    provisioner, iam, _, _ = build_provisioner(
        project=db_project(model_ids=[MODEL_B]), bedrock=bedrock
    )
    provisioner.reconcile_project(PROJECT_ID)

    assert all(
        'bedrock:::foundation-model' not in document
        for document in policy_documents(iam)
    )
    assert 'create_policy' not in call_names(iam)
    assert 'delete_inference_profile' in call_names(bedrock)


def test_a_regionally_routed_profile_still_reaches_the_policy():
    # the control for the regionless assertion above: the same path with a
    # regional arn does write a policy, and it names that arn.
    provisioner, iam, _, _ = build_provisioner(project=db_project(model_ids=[MODEL_B]))
    provisioner.reconcile_project(PROJECT_ID)

    documents = policy_documents(iam)
    assert len(documents) == 1
    assert f'bedrock:{REGION}::foundation-model' in documents[0]
    assert 'bedrock:::foundation-model' not in documents[0]


# model id validation


def test_a_global_model_id_is_rejected_and_names_the_geographic_id():
    with pytest.raises(exceptions.SocaException) as exc_info:
        validate_no_global_profiles(
            [MODEL_B, 'global.vendor-a.model-1'], PARTITION, REGION
        )

    message = str(exc_info.value)
    assert 'global.vendor-a.model-1' in message
    assert 'us.vendor-a.model-1' in message


def test_a_global_model_id_is_rejected_without_a_region():
    with pytest.raises(exceptions.SocaException):
        validate_no_global_profiles(['global.vendor-a.model-1'])


def test_cross_region_and_bare_model_ids_are_accepted():
    validate_no_global_profiles([MODEL_A, MODEL_B], PARTITION, REGION)


def test_a_gov_model_id_is_accepted_in_a_gov_partition():
    validate_no_global_profiles(
        ['us-gov.vendor-a.model-1', MODEL_B], 'aws-us-gov', 'us-gov-west-1'
    )


def test_a_gov_global_model_id_names_the_gov_geographic_id():
    with pytest.raises(exceptions.SocaException) as exc_info:
        validate_no_global_profiles(
            ['global.vendor-a.model-1'], 'aws-us-gov', 'us-gov-west-1'
        )

    assert 'us-gov.vendor-a.model-1' in str(exc_info.value)


# naming


def test_build_resource_name_truncates_deterministically():
    parts = ['idea-cluster', 'a' * 80, 'project']
    first = build_resource_name(parts, 64)
    assert len(first) == 64
    assert first == build_resource_name(parts, 64)
    assert first != build_resource_name(['idea-cluster', 'b' * 80, 'project'], 64)


def test_build_resource_name_has_no_consecutive_separators():
    name = build_resource_name(['idea-test', 'x  y', 'vendor.model-1:0'], 64)
    assert '--' not in name
    assert name == 'idea-test-x-y-vendor.model-1:0'


# provisioning


def test_provision_creates_role_instance_profile_and_inference_profiles():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)

    create_role = kwargs_for(iam, 'create_role')[0]
    assert create_role['Path'] == f'/idea/{CLUSTER_NAME}/projects/'
    assert create_role['PermissionsBoundary'] == BOUNDARY_ARN
    tags = {tag['Key']: tag['Value'] for tag in create_role['Tags']}
    assert tags['idea:Project'] == 'research'
    assert tags['idea:ClusterName'] == CLUSTER_NAME
    assert tags['idea:CostCenter'] == 'cc-1'

    created = kwargs_for(bedrock, 'create_inference_profile')
    assert len(created) == 2
    profile_tags = {tag['key']: tag['value'] for tag in created[0]['tags']}
    assert profile_tags['idea:Project'] == 'research'
    assert created[0]['modelSource']['copyFrom'].endswith(
        f'inference-profile/{MODEL_A}'
    )
    assert created[1]['modelSource']['copyFrom'].endswith(f'foundation-model/{MODEL_B}')

    attached = kwargs_for(iam, 'attach_role_policy')
    attached_arns = {item['PolicyArn'] for item in attached}
    assert BASE_POLICY_ARN in attached_arns
    assert SSM_POLICY_ARN in attached_arns
    assert any('/projects/' in arn for arn in attached_arns)

    add_role = kwargs_for(iam, 'add_role_to_instance_profile')[0]
    assert add_role['RoleName'] == provisioner.role_name(
        ProjectsDAO.convert_from_db(db_project())
    )

    saved = dao.updates[-1]['bedrock']
    assert saved['role_arn'].startswith(f'arn:aws:iam::{ACCOUNT_ID}:role/idea/')
    assert set(saved['inference_profile_arns'].keys()) == {MODEL_A, MODEL_B}


def test_provision_proceeds_when_the_absent_role_read_is_denied():
    provisioner, iam, bedrock, dao = build_provisioner(iam=PathScopedIam())
    logger = RecordingLogger()
    provisioner.logger = logger
    provisioner.reconcile_project(PROJECT_ID)

    assert logger.errors == []
    path = f'/idea/{CLUSTER_NAME}/projects/'
    assert kwargs_for(iam, 'create_role')[0]['Path'] == path
    assert kwargs_for(iam, 'create_instance_profile')[0]['Path'] == path

    saved = dao.updates[-1]['bedrock']
    assert saved['role_arn'].startswith(f'arn:aws:iam::{ACCOUNT_ID}:role{path}')
    assert saved['instance_profile_arn'].startswith(
        f'arn:aws:iam::{ACCOUNT_ID}:instance-profile{path}'
    )
    assert set(saved['inference_profile_arns'].keys()) == {MODEL_A, MODEL_B}


def test_teardown_treats_a_denied_read_of_an_absent_role_as_absent():
    provisioner, iam, _, dao = build_provisioner(
        project=db_project(enabled=False), iam=PathScopedIam()
    )
    logger = RecordingLogger()
    provisioner.logger = logger
    provisioner.reconcile_project(PROJECT_ID)

    assert logger.errors == []
    assert call_names(iam) == ['get_role']
    assert dao.updates == []


def test_a_denied_write_is_still_reported():
    class DeniedCreateIam(PathScopedIam):
        def create_role(self, **kwargs):
            self._record('create_role', **kwargs)
            raise access_denied('CreateRole')

    provisioner, iam, _, dao = build_provisioner(iam=DeniedCreateIam())
    logger = RecordingLogger()
    provisioner.logger = logger
    provisioner.reconcile_project(PROJECT_ID)

    assert len(logger.errors) == 1
    assert 'denied' in logger.errors[0]
    assert 'create_instance_profile' not in call_names(iam)
    # the denial is recorded on the project so the administrator sees it; no role fields are invented
    saved = dao.updates[-1]['bedrock']
    assert 'denied' in saved['policy_errors']['CreateRole']
    assert saved.get('role_arn') is None


def test_provision_only_covers_models_in_the_cluster_catalog():
    project = db_project(model_ids=[MODEL_B, 'vendor-x.not-approved'])
    provisioner, iam, bedrock, dao = build_provisioner(project=project)
    provisioner.reconcile_project(PROJECT_ID)

    created = kwargs_for(bedrock, 'create_inference_profile')
    assert len(created) == 1
    assert MODEL_B in created[0]['inferenceProfileName']

    saved = dao.updates[-1]['bedrock']
    assert list(saved['inference_profile_arns'].keys()) == [MODEL_B]

    policy_document = kwargs_for(iam, 'create_policy')[0]['PolicyDocument']
    assert 'not-approved' not in policy_document


def test_provision_is_idempotent():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    iam.calls.clear()
    bedrock.calls.clear()

    provisioner.reconcile_project(PROJECT_ID)

    for mutating in (
        'create_role',
        'create_policy',
        'create_policy_version',
        'create_instance_profile',
        'attach_role_policy',
        'detach_role_policy',
        'add_role_to_instance_profile',
        'put_role_permissions_boundary',
    ):
        assert mutating not in call_names(iam), mutating
    assert 'create_inference_profile' not in call_names(bedrock)
    assert 'delete_inference_profile' not in call_names(bedrock)


def test_provision_restores_a_drifted_permissions_boundary():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    role_name = list(iam.roles.keys())[0]
    iam.roles[role_name]['PermissionsBoundary'] = {
        'PermissionsBoundaryArn': 'arn:aws:iam::123456789012:policy/other'
    }
    iam.calls.clear()

    provisioner.reconcile_project(PROJECT_ID)

    put = kwargs_for(iam, 'put_role_permissions_boundary')
    assert len(put) == 1
    assert put[0]['PermissionsBoundary'] == BOUNDARY_ARN


def test_revoked_model_loses_policy_access_before_the_profile_is_deleted():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    revoked_arn = dao.db_project['bedrock']['inference_profile_arns'][MODEL_A]

    dao.db_project = {
        **dao.db_project,
        'bedrock': {**dao.db_project['bedrock'], 'model_ids': [MODEL_B]},
    }
    iam.calls.clear()
    bedrock.calls.clear()
    dao.log.clear()

    provisioner.reconcile_project(PROJECT_ID)

    policy_version = kwargs_for(iam, 'create_policy_version')
    assert len(policy_version) == 1
    policy_arn = policy_version[0]['PolicyArn']
    document = iam.policies[policy_arn]['versions'][iam.policies[policy_arn]['default']]
    assert revoked_arn not in document

    ordered = [name for name, _ in dao.log]
    assert ordered.index('create_policy_version') < ordered.index(
        'delete_inference_profile'
    )
    assert (
        kwargs_for(bedrock, 'delete_inference_profile')[0]['inferenceProfileIdentifier']
        == revoked_arn.split('/')[-1]
    )
    assert dao.updates[-1]['bedrock']['inference_profile_arns'] == {
        MODEL_B: dao.db_project['bedrock']['inference_profile_arns'][MODEL_B]
    }


def test_empty_model_list_removes_the_bedrock_policy_but_keeps_the_role():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    dao.db_project = {
        **dao.db_project,
        'bedrock': {**dao.db_project['bedrock'], 'model_ids': []},
    }
    iam.calls.clear()

    provisioner.reconcile_project(PROJECT_ID)

    assert 'delete_policy' in call_names(iam)
    assert len(iam.roles) == 1
    assert not any('/projects/' in arn for arn in iam.policies)


# teardown


def test_disabled_project_is_torn_down():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    assert len(bedrock.profiles) == 2

    dao.db_project = {**dao.db_project, 'enabled': False}
    provisioner.reconcile_project(PROJECT_ID)

    assert iam.roles == {}
    assert iam.instance_profiles == {}
    assert iam.policies == {BOUNDARY_ARN: iam.policies[BOUNDARY_ARN]}
    assert bedrock.profiles == {}

    saved = dao.updates[-1]['bedrock']
    assert 'role_arn' not in saved
    assert 'instance_profile_arn' not in saved
    assert 'inference_profile_arns' not in saved


def test_bedrock_disabled_on_the_project_tears_down():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)

    dao.db_project = {
        **dao.db_project,
        'bedrock': {**dao.db_project['bedrock'], 'enabled': False},
    }
    provisioner.reconcile_project(PROJECT_ID)

    assert iam.roles == {}
    assert bedrock.profiles == {}


def test_teardown_of_an_unprovisioned_project_makes_no_writes():
    project = db_project(enabled=False)
    provisioner, iam, bedrock, dao = build_provisioner(project=project)

    provisioner.reconcile_project(PROJECT_ID)

    assert call_names(iam) == ['get_role']
    assert bedrock.calls == []
    assert dao.updates == []


def test_teardown_is_idempotent():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    dao.db_project = {**dao.db_project, 'enabled': False}
    provisioner.reconcile_project(PROJECT_ID)
    iam.calls.clear()
    bedrock.calls.clear()

    provisioner.reconcile_project(PROJECT_ID)

    assert 'delete_role' not in call_names(iam)
    assert bedrock.calls == []


# gating


def test_reconcile_is_a_noop_when_the_feature_is_disabled():
    provisioner, iam, bedrock, dao = build_provisioner(
        config_values={f'{MODULE_ID}.bedrock.enabled': False}
    )
    provisioner.reconcile_project(PROJECT_ID)
    assert iam.calls == []
    assert bedrock.calls == []
    assert dao.updates == []


def test_missing_permissions_boundary_stops_provisioning():
    provisioner, iam, bedrock, dao = build_provisioner(boundary=False)
    with pytest.raises(exceptions.SocaException):
        provisioner.reconcile_project(PROJECT_ID)
    assert 'create_role' not in call_names(iam)


def test_disabling_the_feature_tears_down_before_the_config_catches_up():
    # the real sequence: the setting is written, the reconcile runs about a second
    # later, and the in-memory config tree does not refresh for another 10-30s. the
    # intent has to travel with the task or the disable is a no-op and access stays.
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    assert 'create_role' in call_names(iam)

    iam.calls.clear()
    bedrock.calls.clear()
    assert provisioner.is_enabled() is True, 'the config is still pre-change here'
    provisioner.reconcile_project(
        PROJECT_ID, cluster_bedrock={'enabled': False, 'model_ids': CATALOG}
    )

    assert 'delete_role' in call_names(iam)
    assert 'delete_instance_profile' in call_names(iam)
    assert 'delete_inference_profile' in call_names(bedrock)


def test_removing_a_model_revokes_it_before_the_config_catches_up():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    assert set(dao.updates[-1]['bedrock']['inference_profile_arns']) == {
        MODEL_A,
        MODEL_B,
    }

    iam.calls.clear()
    bedrock.calls.clear()
    provisioner.reconcile_project(
        PROJECT_ID, cluster_bedrock={'enabled': True, 'model_ids': [MODEL_B]}
    )

    saved = dao.updates[-1]['bedrock']
    assert list(saved['inference_profile_arns'].keys()) == [MODEL_B]
    assert 'delete_inference_profile' in call_names(bedrock)
    assert saved['model_errors'][MODEL_A] == 'not in the cluster model catalog'


def test_disabling_the_feature_tears_down_a_provisioned_project():
    # the same outcome once the config has propagated, which is the path a reconcile
    # triggered by anything other than the settings write takes.
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    assert 'create_role' in call_names(iam)

    provisioner.context.config().values[f'{MODULE_ID}.bedrock.enabled'] = False
    iam.calls.clear()
    bedrock.calls.clear()
    provisioner.reconcile_project(PROJECT_ID)

    assert 'delete_role' in call_names(iam)
    assert 'delete_instance_profile' in call_names(iam)
    assert 'delete_inference_profile' in call_names(bedrock)


def test_disabling_the_feature_makes_no_calls_for_an_unprovisioned_project():
    provisioner, iam, bedrock, dao = build_provisioner(
        config_values={f'{MODULE_ID}.bedrock.enabled': False}
    )
    provisioner.reconcile_project(PROJECT_ID)
    assert iam.calls == []
    assert bedrock.calls == []


def test_access_denied_is_reported_not_raised():
    # the grants arrive with a module redeploy, which can trail the setting. a
    # raise here would fail the task on every retry.
    provisioner, iam, _, dao = build_provisioner()

    def denied(**kwargs):
        raise botocore.exceptions.ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'denied'}}, 'GetPolicy'
        )

    iam.get_policy = denied
    provisioner.reconcile_project(PROJECT_ID)

    # recorded on the project so the administrator sees it, not only in the log
    saved = dao.updates[-1]['bedrock']
    assert 'denied' in saved['policy_errors']['GetPolicy']
    assert saved['enabled'] is True
    assert saved['model_ids'] == CATALOG


def test_teardown_keeps_a_role_carrying_an_unmanaged_policy():
    """
    a policy an administrator attached by hand is outside the provisioner's iam
    grant. detaching it would be denied and misread as missing permissions, so the
    managed policies come off, the role stays and the condition is logged.
    """
    unmanaged_arn = f'arn:{PARTITION}:iam::{ACCOUNT_ID}:policy/hand-attached'
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    role_name = next(iter(iam.roles))
    iam.attached[role_name].add(unmanaged_arn)
    logger = RecordingLogger()
    provisioner.logger = logger

    dao.db_project = {**dao.db_project, 'enabled': False}
    provisioner.reconcile_project(PROJECT_ID)

    assert iam.attached[role_name] == {unmanaged_arn}
    assert role_name in iam.roles
    assert 'delete_role' not in call_names(iam)
    assert iam.instance_profiles == {}
    assert bedrock.profiles == {}
    assert logger.errors == []
    assert any(unmanaged_arn in message for message in logger.warnings)
    # the role is still recorded, so a later reconcile finds it
    assert dao.updates[-1]['bedrock']['role_arn'].endswith(role_name)


def test_a_base_model_without_on_demand_resolves_to_a_system_profile():
    system_profile_id = f'us.{MODEL_B}'
    bedrock = FakeBedrock(
        on_demand_models=set(),
        system_profiles={
            system_profile_id: [
                f'arn:{PARTITION}:bedrock:{REGION}::foundation-model/{MODEL_B}'
            ]
        },
    )
    provisioner, _, _, _ = build_provisioner(
        project=db_project(model_ids=[MODEL_B]), bedrock=bedrock
    )
    provisioner.reconcile_project(PROJECT_ID)

    created = kwargs_for(bedrock, 'create_inference_profile')
    assert len(created) == 1
    assert created[0]['modelSource']['copyFrom'].endswith(
        f'inference-profile/{system_profile_id}'
    )


def test_an_unresolvable_model_is_skipped_not_created():
    bedrock = FakeBedrock(on_demand_models=set(), system_profiles={})
    provisioner, iam, _, _ = build_provisioner(
        project=db_project(model_ids=[MODEL_B]), bedrock=bedrock
    )
    provisioner.reconcile_project(PROJECT_ID)

    assert 'create_inference_profile' not in call_names(bedrock)
    assert 'create_role' in call_names(iam)


def test_an_adopted_profile_and_an_existing_role_are_retagged():
    provisioner, iam, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)

    iam.calls.clear()
    bedrock.calls.clear()
    provisioner.reconcile_project(PROJECT_ID)

    assert 'tag_role' in call_names(iam)
    assert 'tag_resource' in call_names(bedrock)


def test_a_profile_that_fails_to_delete_stays_in_the_stored_map():
    # dropping it from the map orphaned the profile: nothing held a handle on it.
    provisioner, _, bedrock, dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)
    stored = dao.db_project['bedrock']['inference_profile_arns']
    assert MODEL_A in stored

    def denied(**kwargs):
        raise botocore.exceptions.ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'denied'}},
            'DeleteInferenceProfile',
        )

    bedrock.delete_inference_profile = denied
    dao.db_project['bedrock']['model_ids'] = [MODEL_B]
    provisioner.reconcile_project(PROJECT_ID)

    assert MODEL_A in dao.db_project['bedrock']['inference_profile_arns']


def test_write_back_keeps_a_concurrent_model_id_edit():
    # the write back used the copy read at reconcile start, reverting any edit
    # that landed while it ran. the edit here lands mid reconcile, after the
    # provisioner read the project and before it writes back.
    provisioner, _, bedrock, dao = build_provisioner()
    original_get = bedrock.get_inference_profile
    state = {'edited': False}

    def get_with_concurrent_edit(inferenceProfileIdentifier):
        if not state['edited']:
            state['edited'] = True
            dao.db_project['bedrock'] = {'enabled': True, 'model_ids': [MODEL_B]}
        return original_get(inferenceProfileIdentifier)

    bedrock.get_inference_profile = get_with_concurrent_edit
    provisioner.reconcile_project(PROJECT_ID)

    assert state['edited'] is True
    assert dao.db_project['bedrock']['model_ids'] == [MODEL_B]


@pytest.mark.parametrize(
    'partition,region,model_id,supported',
    [
        ('aws', 'us-east-2', 'us.vendor.model', True),
        ('aws', 'eu-west-1', 'us.vendor.model', False),
        ('aws', 'eu-west-1', 'eu.vendor.model', True),
        ('aws', 'us-east-2', 'global.vendor.model', False),
        ('aws', 'us-east-2', 'vendor.model', True),
        ('aws-us-gov', 'us-gov-west-1', 'us.vendor.model', False),
        ('aws-us-gov', 'us-gov-west-1', 'us-gov.vendor.model', True),
    ],
)
def test_region_model_support(partition, region, model_id, supported):
    assert is_model_supported_in_region(partition, region, model_id) is supported


# one unavailable model


def test_an_unavailable_model_does_not_cost_the_project_its_other_models():
    provisioner, iam, bedrock, dao = build_provisioner(
        bedrock=FakeBedrock(fail_models=[MODEL_A])
    )
    provisioner.reconcile_project(PROJECT_ID)

    # the role, the policy and the instance profile still exist
    assert len(kwargs_for(iam, 'create_role')) == 1
    assert len(kwargs_for(iam, 'create_policy')) == 1
    assert len(kwargs_for(iam, 'create_instance_profile')) == 1

    saved = dao.updates[-1]['bedrock']
    assert list(saved['inference_profile_arns'].keys()) == [MODEL_B]
    assert saved['role_arn']


def test_an_unavailable_model_is_recorded_against_the_project():
    provisioner, _iam, _bedrock, dao = build_provisioner(
        bedrock=FakeBedrock(fail_models=[MODEL_A])
    )
    provisioner.reconcile_project(PROJECT_ID)

    errors = dao.updates[-1]['bedrock']['model_errors']
    assert list(errors.keys()) == [MODEL_A]
    assert 'not available' in errors[MODEL_A]


def test_an_unavailable_model_does_not_raise_out_of_the_reconcile():
    # raising here means sqs redelivers 16 times and then dead-letters, failing the
    # same way every pass
    provisioner, _iam, _bedrock, _dao = build_provisioner(
        bedrock=FakeBedrock(fail_models=[MODEL_A])
    )
    provisioner.reconcile_project(PROJECT_ID)


def test_a_model_outside_the_catalog_is_recorded_with_a_reason():
    project = db_project(model_ids=[MODEL_B, 'vendor-x.not-approved'])
    provisioner, _iam, _bedrock, dao = build_provisioner(project=project)
    provisioner.reconcile_project(PROJECT_ID)

    errors = dao.updates[-1]['bedrock']['model_errors']
    assert errors['vendor-x.not-approved'] == 'not in the cluster model catalog'


def test_the_recorded_error_clears_once_the_model_becomes_available():
    provisioner, _iam, _bedrock, dao = build_provisioner(
        bedrock=FakeBedrock(fail_models=[MODEL_A])
    )
    provisioner.reconcile_project(PROJECT_ID)
    assert 'model_errors' in dao.updates[-1]['bedrock']

    # same project, a bedrock that now accepts every model
    provisioner, _iam, _bedrock2, dao2 = build_provisioner(
        project=dao.db_project, bedrock=FakeBedrock()
    )
    provisioner.reconcile_project(PROJECT_ID)

    saved = dao2.updates[-1]['bedrock']
    assert 'model_errors' not in saved
    assert set(saved['inference_profile_arns'].keys()) == {MODEL_A, MODEL_B}


# a live desktop shares the project role


def _project_and_policy_arn(provisioner):
    project = ProjectsDAO.convert_from_db(db_project())
    return project, provisioner.arns.get_project_policy_arn(
        provisioner.policy_name(project)
    )


def test_disabling_bedrock_keeps_the_role_while_a_desktop_still_uses_it():
    # deleting the role takes the dcv host, ssm and cloudwatch policies with it, so a
    # running desktop loses its sqs queue, its logs and its licence fetch.
    ec2 = FakeEc2(instance_ids=['i-0abc'])
    provisioner, iam, bedrock, dao = build_provisioner(ec2=ec2)
    provisioner.reconcile_project(PROJECT_ID)

    iam.calls.clear()
    bedrock.calls.clear()
    provisioner.reconcile_project(
        PROJECT_ID, cluster_bedrock={'enabled': False, 'model_ids': CATALOG}
    )

    assert 'delete_role' not in call_names(iam)
    assert 'delete_instance_profile' not in call_names(iam)
    assert 'remove_role_from_instance_profile' not in call_names(iam)


def test_disabling_bedrock_still_revokes_model_access_on_a_live_desktop():
    ec2 = FakeEc2(instance_ids=['i-0abc'])
    provisioner, iam, _bedrock, _dao = build_provisioner(ec2=ec2)
    provisioner.reconcile_project(PROJECT_ID)
    project, project_policy_arn = _project_and_policy_arn(provisioner)
    role_name = provisioner.role_name(project)

    provisioner.reconcile_project(
        PROJECT_ID, cluster_bedrock={'enabled': False, 'model_ids': CATALOG}
    )

    remaining = iam.attached[role_name]
    assert project_policy_arn not in remaining, 'bedrock access must be revoked'
    assert BASE_POLICY_ARN in remaining, 'the dcv host policy must survive'
    assert SSM_POLICY_ARN in remaining


def test_the_inference_profiles_go_even_when_the_role_is_kept():
    ec2 = FakeEc2(instance_ids=['i-0abc'])
    provisioner, _iam, bedrock, dao = build_provisioner(ec2=ec2)
    provisioner.reconcile_project(PROJECT_ID)

    bedrock.calls.clear()
    provisioner.reconcile_project(
        PROJECT_ID, cluster_bedrock={'enabled': False, 'model_ids': CATALOG}
    )

    assert 'delete_inference_profile' in call_names(bedrock)
    assert dao.updates[-1]['bedrock'].get('inference_profile_arns') in (None, {})


def test_a_failed_instance_lookup_keeps_the_role():
    # an unknown must not be read as 'nobody is using it'
    ec2 = FakeEc2(error=access_denied('DescribeInstances'))
    provisioner, iam, _bedrock, _dao = build_provisioner(ec2=ec2)
    provisioner.reconcile_project(PROJECT_ID)

    iam.calls.clear()
    provisioner.reconcile_project(
        PROJECT_ID, cluster_bedrock={'enabled': False, 'model_ids': CATALOG}
    )

    assert 'delete_role' not in call_names(iam)


def test_the_role_is_filtered_on_the_project_instance_profile_arn():
    ec2 = FakeEc2(instance_ids=[])
    provisioner, _iam, _bedrock, _dao = build_provisioner(ec2=ec2)
    provisioner.reconcile_project(PROJECT_ID)
    provisioner.reconcile_project(
        PROJECT_ID, cluster_bedrock={'enabled': False, 'model_ids': CATALOG}
    )

    filters = {f['Name']: f['Values'] for f in ec2.filters[-1]}
    assert filters['iam-instance-profile.arn'][0].endswith(
        provisioner.instance_profile_name(ProjectsDAO.convert_from_db(db_project()))
    )
    assert 'stopped' in filters['instance-state-name'], (
        'a stopped desktop is resumed later and still needs the role'
    )


def test_teardown_completes_once_no_instance_uses_the_profile():
    ec2 = FakeEc2(instance_ids=['i-0abc'])
    provisioner, iam, bedrock, dao = build_provisioner(ec2=ec2)
    provisioner.reconcile_project(PROJECT_ID)
    provisioner.reconcile_project(
        PROJECT_ID, cluster_bedrock={'enabled': False, 'model_ids': CATALOG}
    )
    assert 'delete_role' not in call_names(iam)

    # the desktop is gone, so the next reconcile finishes the job
    ec2.instance_ids = []
    iam.calls.clear()
    provisioner.reconcile_project(
        PROJECT_ID, cluster_bedrock={'enabled': False, 'model_ids': CATALOG}
    )

    assert 'delete_role' in call_names(iam)
    assert 'delete_instance_profile' in call_names(iam)


# administrator supplied managed policies


ADMIN_POLICY_ARN = f'arn:{PARTITION}:iam::{ACCOUNT_ID}:policy/customer-data-access'
LICENSE_POLICY_ARN = f'arn:{PARTITION}:iam::{ACCOUNT_ID}:policy/license-parameter'

# stands in for the rendered boundary. only its actions matter here, so the resources
# and the deny statements the real document carries are left out.
BOUNDARY_ACTIONS = ['logs:*', 's3:GetObject', 's3:ListBucket', 'ssm:*']


def policy_document(actions, effect='Allow'):
    return {
        'Version': '2012-10-17',
        'Statement': [{'Effect': effect, 'Action': actions, 'Resource': '*'}],
    }


def stored_policy(actions):
    return {'versions': {'v1': policy_document(actions)}, 'default': 'v1'}


def build_provisioner_with_admin_policies(
    documents, boundary_actions=None, iam=None, project=None
):
    """
    a cluster that sets cluster.iam.ec2_managed_policy_arns. every arn mapped to None
    has no readable document, which is how an unreadable policy is exercised.
    """
    iam = iam or FakeIam()
    iam.policies[BOUNDARY_ARN] = stored_policy(boundary_actions or BOUNDARY_ACTIONS)
    for arn, actions in documents.items():
        if actions is not None:
            iam.policies[arn] = stored_policy(actions)
    return build_provisioner(
        project=project,
        config_values={'cluster.iam.ec2_managed_policy_arns': list(documents)},
        iam=iam,
        boundary=False,
    )


def attached_arns(iam):
    return {kwargs['PolicyArn'] for kwargs in kwargs_for(iam, 'attach_role_policy')}


def test_get_allowed_actions_reads_a_single_action_and_skips_a_deny():
    document = {
        'Statement': [
            {'Effect': 'Allow', 'Action': 's3:PutObject'},
            {'Effect': 'Deny', 'Action': ['s3:DeleteObject']},
        ]
    }
    assert get_allowed_actions(document) == ['s3:PutObject']


def test_get_allowed_actions_reads_a_single_statement_object():
    # iam accepts a bare statement object in place of the list
    document = {'Statement': {'Effect': 'Allow', 'Action': ['s3:PutObject']}}
    assert get_allowed_actions(document) == ['s3:PutObject']


def test_an_allow_by_exclusion_is_not_covered_by_any_named_action():
    document = {'Statement': [{'Effect': 'Allow', 'NotAction': ['iam:*']}]}
    assert get_allowed_actions(document) == ['*']
    assert get_actions_outside_boundary(['*'], ['s3:GetObject', 'logs:*']) == ['*']


def test_a_service_wildcard_in_the_boundary_covers_the_actions_below_it():
    assert get_actions_outside_boundary(['ssm:GetParameter'], ['ssm:*']) == []
    # the reverse is reported: a grant wider than the ceiling is voided in part
    assert get_actions_outside_boundary(['ssm:*'], ['ssm:GetParameter']) == ['ssm:*']


def test_an_administrator_policy_the_boundary_covers_is_attached():
    provisioner, iam, _bedrock, dao = build_provisioner_with_admin_policies(
        {LICENSE_POLICY_ARN: ['ssm:GetParameter']}
    )
    provisioner.reconcile_project(PROJECT_ID)

    assert LICENSE_POLICY_ARN in attached_arns(iam)
    assert 'policy_errors' not in dao.updates[-1]['bedrock']


def test_an_administrator_policy_the_boundary_voids_is_not_attached():
    # attaching it reads as granted in the console and denies on the instance
    provisioner, iam, _bedrock, dao = build_provisioner_with_admin_policies(
        {ADMIN_POLICY_ARN: ['s3:PutObject', 'ecr:GetAuthorizationToken']}
    )
    logger = RecordingLogger()
    provisioner.logger = logger
    provisioner.reconcile_project(PROJECT_ID)

    assert ADMIN_POLICY_ARN not in attached_arns(iam)
    # the policies idea owns are still attached
    assert {BASE_POLICY_ARN, SSM_POLICY_ARN, CW_POLICY_ARN} <= attached_arns(iam)

    reason = dao.updates[-1]['bedrock']['policy_errors'][ADMIN_POLICY_ARN]
    assert 'ecr:GetAuthorizationToken' in reason
    assert 's3:PutObject' in reason
    assert len(logger.errors) == 1
    assert ADMIN_POLICY_ARN in logger.errors[0]


def test_a_partly_voided_administrator_policy_names_only_the_voided_actions():
    provisioner, iam, _bedrock, dao = build_provisioner_with_admin_policies(
        {ADMIN_POLICY_ARN: ['s3:GetObject', 's3:PutObject']}
    )
    provisioner.reconcile_project(PROJECT_ID)

    assert ADMIN_POLICY_ARN not in attached_arns(iam)
    reason = dao.updates[-1]['bedrock']['policy_errors'][ADMIN_POLICY_ARN]
    assert 's3:PutObject' in reason
    assert 's3:GetObject' not in reason


def test_a_refused_administrator_policy_is_detached_from_a_role_that_carries_it():
    # a role provisioned before the boundary was compared still has it attached
    provisioner, iam, _bedrock, _dao = build_provisioner_with_admin_policies(
        {ADMIN_POLICY_ARN: ['s3:PutObject']},
        boundary_actions=['logs:*', 's3:*', 'ssm:*'],
    )
    provisioner.reconcile_project(PROJECT_ID)
    assert ADMIN_POLICY_ARN in attached_arns(iam)

    iam.policies[BOUNDARY_ARN] = stored_policy(['logs:*', 's3:GetObject', 'ssm:*'])
    iam.calls.clear()
    provisioner.reconcile_project(PROJECT_ID)

    role_name = provisioner.role_name(ProjectsDAO.convert_from_db(db_project()))
    assert kwargs_for(iam, 'detach_role_policy') == [
        {'RoleName': role_name, 'PolicyArn': ADMIN_POLICY_ARN}
    ]


def test_an_unreadable_administrator_policy_is_attached_and_reported():
    # a policy that cannot be read is not evidence that the boundary voids it, so it
    # keeps the access it has today and the reason is recorded
    provisioner, iam, _bedrock, dao = build_provisioner_with_admin_policies(
        {ADMIN_POLICY_ARN: None}
    )
    provisioner.reconcile_project(PROJECT_ID)

    assert ADMIN_POLICY_ARN in attached_arns(iam)
    reason = dao.updates[-1]['bedrock']['policy_errors'][ADMIN_POLICY_ARN]
    assert 'could not be read' in reason


def test_an_unreadable_boundary_leaves_administrator_policies_attached():
    class DeniedBoundaryVersionRead(FakeIam):
        def list_policy_versions(self, PolicyArn):
            if PolicyArn == BOUNDARY_ARN:
                self._record('list_policy_versions', PolicyArn=PolicyArn)
                raise access_denied('ListPolicyVersions')
            return super().list_policy_versions(PolicyArn)

    provisioner, iam, _bedrock, _dao = build_provisioner_with_admin_policies(
        {ADMIN_POLICY_ARN: ['s3:PutObject']}, iam=DeniedBoundaryVersionRead()
    )
    logger = RecordingLogger()
    provisioner.logger = logger
    provisioner.reconcile_project(PROJECT_ID)

    assert ADMIN_POLICY_ARN in attached_arns(iam)
    assert logger.errors == []
    assert any('boundary could not be read' in message for message in logger.warnings)


def test_a_cluster_without_administrator_policies_reads_no_policy_documents():
    provisioner, iam, _bedrock, _dao = build_provisioner()
    provisioner.reconcile_project(PROJECT_ID)

    boundary_reads = [
        kwargs
        for kwargs in kwargs_for(iam, 'list_policy_versions')
        if kwargs['PolicyArn'] == BOUNDARY_ARN
    ]
    assert boundary_reads == []


def test_the_boundary_check_never_refuses_a_policy_idea_owns():
    # refusing one costs a live desktop its dcv, ssm and cloudwatch access. a boundary
    # that has drifted behind idea's own policies is a deploy time problem, caught by
    # the administrator tests, not one to answer at runtime by dropping that access.
    provisioner, iam, _bedrock, dao = build_provisioner_with_admin_policies(
        {LICENSE_POLICY_ARN: ['ssm:GetParameter']}, boundary_actions=['ssm:*']
    )
    iam.policies[BASE_POLICY_ARN] = stored_policy(['s3:GetObject'])
    provisioner.reconcile_project(PROJECT_ID)

    assert {BASE_POLICY_ARN, SSM_POLICY_ARN, CW_POLICY_ARN} <= attached_arns(iam)
    assert 'policy_errors' not in dao.updates[-1]['bedrock']


def test_policy_lets_a_client_discover_its_own_profiles():
    """invoke alone is not usable: every sdk and cli finds the profile by listing first"""
    document = build_policy_document(
        ['arn:aws:bedrock:us-east-2:1234:application-inference-profile/aaa'], []
    )
    by_sid = {statement['Sid']: statement for statement in document['Statement']}

    assert by_sid['ReadProjectInferenceProfiles']['Action'] == [
        'bedrock:GetInferenceProfile'
    ]
    assert by_sid['ReadProjectInferenceProfiles']['Resource'] == [
        'arn:aws:bedrock:us-east-2:1234:application-inference-profile/aaa'
    ]
    assert by_sid['ListInferenceProfiles']['Action'] == [
        'bedrock:ListInferenceProfiles'
    ]
    # both forms: the api authorises against inference-profile/* when the caller does not filter by
    # type, which is what the claude code setup wizard does, and application-* when it does.
    assert by_sid['ListInferenceProfiles']['Resource'] == [
        'arn:aws:bedrock:us-east-2:1234:application-inference-profile/*',
        'arn:aws:bedrock:us-east-2:1234:inference-profile/*',
    ]


def test_a_project_with_no_profiles_gets_no_discovery_grant():
    document = build_policy_document([], [])
    sids = {statement['Sid'] for statement in document['Statement']}

    assert 'ReadProjectInferenceProfiles' not in sids
    assert 'ListInferenceProfiles' not in sids


def test_discovery_actions_stay_inside_the_project_role_boundary():
    """the boundary is the ceiling; a grant it does not cover is silently void"""
    document = build_policy_document(
        ['arn:aws:bedrock:us-east-2:1234:application-inference-profile/aaa'], []
    )
    boundary = {
        'Version': '2012-10-17',
        'Statement': [
            {
                'Effect': 'Allow',
                'Action': [
                    'bedrock:GetInferenceProfile',
                    'bedrock:InvokeModel',
                    'bedrock:InvokeModelWithResponseStream',
                    'bedrock:ListInferenceProfiles',
                ],
                'Resource': '*',
            }
        ],
    }

    assert get_actions_outside_boundary(document, boundary) == []
