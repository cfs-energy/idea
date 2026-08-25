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
Test Cases for project-role-boundary.yml

A per-project instance role carries this boundary, so its effective access is the
intersection of the boundary and the policies attached to it. An action one of those
policies grants but the boundary omits is dead: attached in the console, AccessDenied
on the instance, and nothing reports it. Rendering both sides and comparing them is
what keeps the boundary correct as those policies change, instead of a list someone
has to remember to update.
"""

import fnmatch

import pytest

from ideasdk.config.soca_config import SocaConfig

from ideaadministrator.app_utils import AdministratorUtils

BOUNDARY_TEMPLATE = 'project-role-boundary.yml'

# the policies the bedrock project provisioner attaches to a project role, other than
# the project's own bedrock policy and any administrator supplied policy.
ATTACHED_POLICY_TEMPLATES = [
    'virtual-desktop-dcv-host.yml',
    'amazon-ssm-managed-instance-core.yml',
    'cloud-watch-agent-server-policy.yml',
    'amazon-prometheus-remote-write-access.yml',
]

# the host policy grants different actions per directory service provider, so every
# provider is rendered rather than only the default one.
DIRECTORY_SERVICE_PROVIDERS = [
    'aws_managed_activedirectory',
    'activedirectory',
    'openldap',
]

CLUSTER_NAME = 'idea-test'
MODULE_ID = 'virtual-desktop-controller'


class RenderConfig(SocaConfig):
    """the config surface the policy templates read, without a cluster config table."""

    def get_module_id(self, module_name: str) -> str:
        return module_name


def build_config(directory_service_provider: str) -> RenderConfig:
    return RenderConfig(
        config={
            'cluster': {
                'cluster_name': CLUSTER_NAME,
                'cluster_s3_bucket': f'{CLUSTER_NAME}-cluster',
                'aws': {
                    'region': 'us-east-2',
                    'account_id': '123456789012',
                    'partition': 'aws',
                    'dns_suffix': 'amazonaws.com',
                },
                # the widest render: both optional kms blocks of the host policy
                'kms': {'key_type': 'customer-managed'},
                'dynamodb': {'kms_key_id': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'},
            },
            'directoryservice': {'provider': directory_service_provider},
        }
    )


@pytest.fixture(autouse=True)
def dev_mode(monkeypatch):
    # policy templates are read from the source tree, not from an installed package
    monkeypatch.setenv('IDEA_DEV_MODE', 'true')


def render(template_name: str, directory_service_provider: str):
    return AdministratorUtils.render_policy(
        policy_template_name=template_name,
        cluster_name=CLUSTER_NAME,
        module_id=MODULE_ID,
        config=build_config(directory_service_provider),
    )


def allowed_actions(policy) -> list:
    actions = set()
    for statement in policy.get('Statement', []):
        if statement.get('Effect', 'Allow') != 'Allow':
            continue
        if 'NotAction' in statement:
            # an allow by exclusion has no enumerable action list, so it stands for
            # every action and is only covered by a boundary that allows every action
            actions.add('*')
            continue
        action = statement.get('Action', [])
        if isinstance(action, str):
            action = [action]
        actions.update(action)
    return sorted(actions)


def uncovered_actions(actions, boundary_actions) -> list:
    # iam matches an action name case insensitively, so the comparison does too
    patterns = [pattern.lower() for pattern in boundary_actions]
    return sorted(
        action
        for action in actions
        if not any(fnmatch.fnmatchcase(action.lower(), pattern) for pattern in patterns)
    )


@pytest.mark.parametrize('provider', DIRECTORY_SERVICE_PROVIDERS)
@pytest.mark.parametrize('template_name', ATTACHED_POLICY_TEMPLATES)
def test_the_boundary_allows_every_action_an_attached_policy_grants(
    template_name, provider
):
    boundary_actions = allowed_actions(render(BOUNDARY_TEMPLATE, provider))
    actions = allowed_actions(render(template_name, provider))
    assert len(actions) > 0, f'{template_name} rendered no allowed actions'

    uncovered = uncovered_actions(actions, boundary_actions)
    assert uncovered == [], (
        f'{template_name} grants actions {BOUNDARY_TEMPLATE} does not allow, so a '
        f'project role gets them attached and denied: {uncovered}'
    )


def test_the_comparison_reports_an_action_the_boundary_omits():
    """
    the check above only means something if it can fail, so both answers are asserted
    against the rendered boundary
    """
    boundary_actions = allowed_actions(render(BOUNDARY_TEMPLATE, 'openldap'))
    assert uncovered_actions(['ec2:DescribeTags'], boundary_actions) == []
    assert uncovered_actions(['logs:DescribeLogGroups'], boundary_actions) == []
    assert uncovered_actions(['logs:DescribeLogStreams'], boundary_actions) == []
    assert uncovered_actions(['ecr:GetAuthorizationToken'], boundary_actions) == [
        'ecr:GetAuthorizationToken'
    ]


def test_a_boundary_wildcard_covers_the_actions_below_it():
    assert uncovered_actions(['ssm:GetParameter'], ['ssm:*']) == []
    # the other direction is not covered: a grant wider than the ceiling is voided
    # in part, so it is reported rather than passed
    assert uncovered_actions(['ssm:*'], ['ssm:GetParameter']) == ['ssm:*']
    assert uncovered_actions(['*'], ['ssm:*', 'logs:*']) == ['*']
