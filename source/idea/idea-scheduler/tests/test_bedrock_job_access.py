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
Test Cases for selecting the instance profile a job's compute nodes run under

a job in a project with bedrock enabled must run under that project's instance profile,
which is what carries the project's model allowlist. anything unresolved must leave the
job on the standard compute node profile without pretending the access is there.
"""

import re
from typing import Dict, Optional

import pytest

from ideadatamodel import (
    constants,
    GetProjectResult,
    HpcQueueProfile,
    Project,
    ProjectBedrockConfig,
    SocaJobParams,
    SocaQueueManagementParams,
)
from ideasdk.aws import AWSUtil
from ideasdk.client import ProjectsClient
from ideascheduler import SchedulerAppContext
from ideascheduler.app.bedrock_job_access import (
    BedrockJobAccessResolver,
    BedrockJobAccessState,
)
from ideascheduler.app.scheduler import SocaJobBuilder

COMPUTE_NODE_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::123456789012:instance-profile/idea-test-compute-node-instance-profile'
)
PROJECT_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::123456789012:instance-profile/idea/idea-mock/projects/'
    'idea-mock-p1-project'
)
PROJECT_INSTANCE_PROFILE_NAME = 'idea-mock-p1-project'
OTHER_INSTANCE_PROFILE_ARN = (
    'arn:aws:iam::123456789012:instance-profile/some-other-instance-profile'
)
PASS_ROLE_ARN = 'arn:aws:iam::123456789012:role/idea/idea-mock/projects/*'
PROJECT_NAME = 'default'
MODEL_ID = 'vendor.model-v1:0'
INFERENCE_PROFILE_ARN = (
    'arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abcd1234'
)


def bedrock_project(
    instance_profile_arn: Optional[str] = PROJECT_INSTANCE_PROFILE_ARN,
    model_ids: Optional[list] = None,
    inference_profile_arns: Optional[Dict[str, str]] = None,
    model_errors: Optional[Dict[str, str]] = None,
    enabled: bool = True,
) -> Project:
    if model_ids is None:
        model_ids = [MODEL_ID]
    if inference_profile_arns is None and instance_profile_arn is not None:
        inference_profile_arns = {MODEL_ID: INFERENCE_PROFILE_ARN}
    return Project(
        project_id='f28879f4-9b47-45b8-beb4-9d42dde2257e',
        name=PROJECT_NAME,
        enabled=True,
        bedrock=ProjectBedrockConfig(
            enabled=enabled,
            model_ids=model_ids,
            instance_profile_arn=instance_profile_arn,
            inference_profile_arns=inference_profile_arns,
            model_errors=model_errors,
        ),
    )


def serve_project(monkeypatch, project: Optional[Project]):
    monkeypatch.setattr(
        ProjectsClient,
        'get_project',
        lambda _self, _request: GetProjectResult(project=project),
    )


def fail_project_reads(monkeypatch):
    def unavailable(_self, _request):
        raise Exception('projects client unavailable')

    monkeypatch.setattr(ProjectsClient, 'get_project', unavailable)


def resolve_instance_profiles(monkeypatch):
    """a submitted instance_profile is looked up in IAM, which the tests do not reach"""
    monkeypatch.setattr(
        AWSUtil,
        'get_instance_profile_arn',
        lambda _self, instance_profile_name: {
            'success': True,
            'arn': f'arn:aws:iam::123456789012:instance-profile/{instance_profile_name}',
        },
    )


def enable_bedrock_for_jobs(
    context: SchedulerAppContext, deployed: bool = True
) -> None:
    context.config().put('scheduler.bedrock.enabled', True)
    if deployed:
        context.config().put('scheduler.bedrock.project_pass_role_arn', PASS_ROLE_ARN)


def build(
    context: SchedulerAppContext,
    params: Optional[Dict] = None,
    queue_profile: Optional[HpcQueueProfile] = None,
    project: Optional[str] = PROJECT_NAME,
):
    if params is None:
        params = {'nodes': 1, 'cpus': 1, 'instance_type': 't3.micro'}
    builder = SocaJobBuilder(
        context=context,
        params=params,
        queue_profile=queue_profile,
        project=project,
    )
    validation_result = builder.validate()
    if not validation_result.is_valid():
        return validation_result, None
    job_params, _ = builder.build()
    return validation_result, job_params


def validation_messages(validation_result) -> str:
    return ' '.join(
        entry.message
        for entry in validation_result.results
        if entry.message is not None
    )


def validation_error_codes(validation_result) -> list:
    return [entry.error_code for entry in validation_result.results]


def test_bedrock_for_jobs_is_off_by_default(context, monkeypatch):
    """
    the project is fully provisioned for bedrock, but nothing was enabled for jobs, so
    the job keeps the standard compute node profile.
    breaks if: the scheduler.bedrock.enabled gate is dropped or defaults to true
    """
    serve_project(monkeypatch, bedrock_project())

    validation_result, job_params = build(context)

    assert validation_result.is_valid() is True
    assert job_params.instance_profile == COMPUTE_NODE_INSTANCE_PROFILE_ARN


def test_bedrock_project_profile_is_applied_to_the_job(context, monkeypatch):
    """
    breaks if: the project is not passed to the builder, or the instance profile default
    no longer consults the project's bedrock access
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())

    validation_result, job_params = build(context)

    assert validation_result.is_valid() is True
    assert job_params.instance_profile == PROJECT_INSTANCE_PROFILE_ARN


def test_bedrock_project_profile_outranks_the_queue_default(context, monkeypatch):
    """
    the queue profile names its own default instance profile. the project identity wins,
    otherwise the job silently loses the model access its project advertises.
    breaks if: the queue default is checked before the project's bedrock access
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())

    validation_result, job_params = build(
        context,
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            default_job_params=SocaJobParams(
                instance_profile=OTHER_INSTANCE_PROFILE_ARN
            ),
        ),
    )

    assert validation_result.is_valid() is True
    assert job_params.instance_profile == PROJECT_INSTANCE_PROFILE_ARN


def test_a_project_without_bedrock_keeps_the_compute_node_profile(context, monkeypatch):
    """
    breaks if: any project is given a project profile regardless of its bedrock flag
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, Project(name=PROJECT_NAME, enabled=True))

    validation_result, job_params = build(context)

    assert validation_result.is_valid() is True
    assert job_params.instance_profile == COMPUTE_NODE_INSTANCE_PROFILE_ARN


def test_an_unprovisioned_project_does_not_get_a_project_profile(context, monkeypatch):
    """
    bedrock is on for the project but the reconcile has not created its instance profile.
    the job keeps the compute node profile and is not rejected: the reconcile is expected
    to complete, and the provisioning gate holds the job until it does.
    breaks if: an empty instance_profile_arn is passed through as a profile, or if the
    submission is rejected for a state that resolves on its own
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project(instance_profile_arn=None))

    validation_result, job_params = build(context)

    assert validation_result.is_valid() is True
    assert job_params.instance_profile == COMPUTE_NODE_INSTANCE_PROFILE_ARN


def test_an_unreadable_project_does_not_get_a_project_profile(context, monkeypatch):
    """
    a projects api failure cannot be read as permission to run under a project role, and
    cannot reject the submission either.
    breaks if: a read failure resolves to any project profile, or fails validation
    """
    enable_bedrock_for_jobs(context)
    fail_project_reads(monkeypatch)

    validation_result, job_params = build(context)

    assert validation_result.is_valid() is True
    assert job_params.instance_profile == COMPUTE_NODE_INSTANCE_PROFILE_ARN


def test_a_job_cannot_replace_the_project_profile(context, monkeypatch):
    """
    a compute node holds one instance role, so a submitted instance_profile would void
    the project's model access.
    breaks if: a submitted instance_profile silently displaces the project profile
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())
    resolve_instance_profiles(monkeypatch)

    validation_result, job_params = build(
        context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'instance_profile': OTHER_INSTANCE_PROFILE_ARN,
        },
    )

    assert validation_result.is_valid() is False
    assert job_params is None
    assert constants.JOB_PARAM_INSTANCE_PROFILE in validation_error_codes(
        validation_result
    )
    assert OTHER_INSTANCE_PROFILE_ARN in validation_messages(validation_result)


def test_submitting_the_project_profile_itself_is_accepted(context, monkeypatch):
    """
    breaks if: the conflict check rejects the profile the job would have been given
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())
    resolve_instance_profiles(monkeypatch)

    validation_result, job_params = build(
        context,
        params={
            'nodes': 1,
            'cpus': 1,
            'instance_type': 't3.micro',
            'instance_profile': PROJECT_INSTANCE_PROFILE_ARN,
        },
    )

    assert validation_result.is_valid() is True
    assert job_params.instance_profile == PROJECT_INSTANCE_PROFILE_ARN


def test_a_queue_that_does_not_authorize_the_project_profile_rejects_the_job(
    context, monkeypatch
):
    """
    the project profile goes through the queue's allowed_instance_profiles like any
    other. an unauthorized one needs an administrator, so the job is rejected at
    submission instead of queued forever.
    breaks if: the project profile bypasses queue validation, or an unauthorized profile
    is accepted and the job left to wait
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())

    validation_result, job_params = build(
        context,
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                allowed_instance_profiles=[OTHER_INSTANCE_PROFILE_ARN]
            ),
        ),
    )

    assert validation_result.is_valid() is False
    assert job_params is None
    assert PROJECT_NAME in validation_messages(validation_result)


def test_the_queue_rejection_carries_no_account_identifiers(context, monkeypatch):
    """
    the rejection reaches the job owner. it names the project; the instance profile arn
    and the account id in it stay with the administrator.
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())

    access = resolve(
        context,
        queue_management=SocaQueueManagementParams(
            allowed_instance_profiles=[OTHER_INSTANCE_PROFILE_ARN]
        ),
    )

    assert access.state == BedrockJobAccessState.NOT_AUTHORIZED
    assert PROJECT_NAME in access.message
    assert 'arn:' not in access.message
    assert re.search(r'\d{12}', access.message) is None


def test_a_queue_may_authorize_the_project_profile_by_name(context, monkeypatch):
    """
    an allowed list is written with names as often as arns, and both name the same
    instance profile.
    breaks if: only the arn form is matched against allowed_instance_profiles
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())

    validation_result, job_params = build(
        context,
        queue_profile=HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=SocaQueueManagementParams(
                allowed_instance_profiles=[PROJECT_INSTANCE_PROFILE_NAME]
            ),
        ),
    )

    assert validation_result.is_valid() is True
    assert job_params.instance_profile == PROJECT_INSTANCE_PROFILE_ARN


def test_a_module_without_the_pass_role_grant_rejects_the_job(context, monkeypatch):
    """
    the scheduler role gets iam:PassRole for project roles at deploy time. without it EC2
    refuses the launch template, so the submission is rejected with the redeploy named
    rather than the job failing later.
    breaks if: the deployment marker is not read, or its absence is treated as retryable
    """
    enable_bedrock_for_jobs(context, deployed=False)
    serve_project(monkeypatch, bedrock_project())

    validation_result, job_params = build(context)

    assert validation_result.is_valid() is False
    assert job_params is None
    assert 'redeploy the scheduler module' in validation_messages(validation_result)


# resolver states


def resolve(
    context: SchedulerAppContext,
    project_name: Optional[str] = PROJECT_NAME,
    queue_management: Optional[SocaQueueManagementParams] = None,
):
    return BedrockJobAccessResolver(
        context=context,
        project_name=project_name,
        queue_management=queue_management,
    ).resolve()


def test_bedrock_enabled_with_no_models_is_not_applicable(context, monkeypatch):
    """
    a project with bedrock on and no model allowed has no access to hand to a job, so its
    jobs must not be held waiting for one.
    breaks if: an empty model list is treated as access that is still reconciling
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project(model_ids=[]))

    assert resolve(context).state == BedrockJobAccessState.NOT_APPLICABLE


def test_models_that_all_failed_to_provision_need_an_administrator(
    context, monkeypatch
):
    """
    every model the project allows was rejected on the last reconcile, so waiting cannot
    fix it.
    breaks if: recorded model errors are ignored and the job waits indefinitely
    """
    enable_bedrock_for_jobs(context)
    serve_project(
        monkeypatch,
        bedrock_project(
            inference_profile_arns={},
            model_errors={MODEL_ID: 'not in the cluster model catalog'},
        ),
    )

    access = resolve(context)
    assert access.state == BedrockJobAccessState.NOT_AUTHORIZED
    assert 'not in the cluster model catalog' in access.message


def test_no_inference_profile_yet_is_not_ready(context, monkeypatch):
    """
    breaks if: a project with no provisioned model reads as available
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project(inference_profile_arns={}))

    access = resolve(context)
    assert access.state == BedrockJobAccessState.NOT_READY
    assert access.instance_profile_arn is None


def test_a_job_without_a_project_is_not_applicable(context, monkeypatch):
    """
    breaks if: an unknown project resolves to any project profile
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())

    assert resolve(context, project_name=None).state == (
        BedrockJobAccessState.NOT_APPLICABLE
    )


@pytest.mark.parametrize('enabled', [False, None])
def test_a_project_bedrock_block_that_is_off_is_not_applicable(
    context, monkeypatch, enabled
):
    """
    breaks if: the presence of a bedrock block is read as bedrock being enabled
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project(enabled=enabled))

    assert resolve(context).state == BedrockJobAccessState.NOT_APPLICABLE
