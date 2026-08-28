"""
Test Cases for the bedrock check on the job provisioning path

a job whose project has bedrock enabled must not be given capacity that cannot reach the
project's models: it would consume compute and fail its first model call while the web
portal lists those models as available. the check is re-run at provisioning because the
project can change after submission.
"""

from typing import Optional

import pytest

from ideadatamodel import (
    errorcodes,
    exceptions,
    HpcQueueProfile,
    SocaJob,
    SocaJobParams,
    SocaQueueManagementParams,
)
from ideascheduler.app.provisioning import JobProvisioningUtil
from ideascheduler.app.provisioning.job_provisioner.job_provisioner import (
    PROVISIONING_WAIT_ERROR_CODES,
)

from test_bedrock_job_access import (
    bedrock_project,
    COMPUTE_NODE_INSTANCE_PROFILE_ARN,
    enable_bedrock_for_jobs,
    fail_project_reads,
    PROJECT_INSTANCE_PROFILE_ARN,
    PROJECT_NAME,
    serve_project,
)


class MockQueueProfiles:
    def __init__(self, queue_profile: HpcQueueProfile):
        self.queue_profile = queue_profile

    def get_queue_profile(self, **_kwargs) -> HpcQueueProfile:
        return self.queue_profile


def build_job(instance_profile: str, project: Optional[str] = PROJECT_NAME) -> SocaJob:
    return SocaJob(
        name='mock-job',
        job_id='1',
        job_uid='mock-job-uid',
        project=project,
        queue='normal',
        queue_type='mock-queue-profile',
        params=SocaJobParams(
            nodes=1,
            base_os='amazonlinux2023',
            instance_ami='ami-mock',
            instance_types=['c5.large'],
            instance_profile=instance_profile,
        ),
    )


def build_util(
    context,
    job: SocaJob,
    queue_management: Optional[SocaQueueManagementParams] = None,
) -> JobProvisioningUtil:
    context.queue_profiles = MockQueueProfiles(
        HpcQueueProfile(
            name='mock-queue-profile',
            queue_management_params=queue_management,
        )
    )
    return JobProvisioningUtil(context=context, jobs=[job])


def test_a_job_carrying_the_project_profile_passes(context, monkeypatch):
    """
    breaks if: the check rejects the job it is supposed to let through
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())

    build_util(context, build_job(PROJECT_INSTANCE_PROFILE_ARN)).check_bedrock()


def test_a_job_without_the_project_profile_is_held(context, monkeypatch):
    """
    the project has provisioned bedrock access but the job would launch under the standard
    compute node profile. this is the silent failure the check exists for: the job would
    run with no model access while the portal lists the models as available.
    breaks if: the provisioning check stops comparing the job's profile with the project's
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())

    util = build_util(context, build_job(COMPUTE_NODE_INSTANCE_PROFILE_ARN))
    with pytest.raises(exceptions.SocaException) as exc_info:
        util.check_bedrock()

    assert exc_info.value.error_code == errorcodes.BEDROCK_ACCESS_NOT_READY
    assert PROJECT_NAME in exc_info.value.message


def test_an_unprovisioned_project_holds_the_job_at_provisioning(context, monkeypatch):
    """
    the reconcile has not created the project's instance profile yet, so no capacity is
    created for the job. it is retried, not failed.
    breaks if: an unprovisioned project is allowed to provision under the compute node
    profile
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project(instance_profile_arn=None))

    util = build_util(context, build_job(COMPUTE_NODE_INSTANCE_PROFILE_ARN))
    with pytest.raises(exceptions.SocaException) as exc_info:
        util.check_bedrock()

    assert exc_info.value.error_code == errorcodes.BEDROCK_ACCESS_NOT_READY


def test_an_unreadable_project_holds_the_job_at_provisioning(context, monkeypatch):
    """
    the project cannot be read, so whether the job needs a project role is unknown.
    provisioning waits rather than creating capacity on an unknown.
    breaks if: a read failure is treated as the project having no bedrock access
    """
    enable_bedrock_for_jobs(context)
    fail_project_reads(monkeypatch)

    util = build_util(context, build_job(COMPUTE_NODE_INSTANCE_PROFILE_ARN))
    with pytest.raises(exceptions.SocaException) as exc_info:
        util.check_bedrock()

    assert exc_info.value.error_code == errorcodes.BEDROCK_ACCESS_NOT_READY


def test_an_unreadable_project_does_not_reject_a_submission(context, monkeypatch):
    """
    the same unknown at submission accepts the job: a projects api blip must not reject
    every job of a bedrock project, and the provisioning check runs again before capacity.
    breaks if: the submission and provisioning behaviours are collapsed into one
    """
    enable_bedrock_for_jobs(context)
    fail_project_reads(monkeypatch)

    util = build_util(context, build_job(COMPUTE_NODE_INSTANCE_PROFILE_ARN))
    util.check_bedrock(reject_when_check_fails=False)


def test_a_project_needing_an_administrator_rejects_a_submission(context, monkeypatch):
    """
    a queue that does not authorize the project profile cannot resolve on its own, so it
    is raised even on the submission path.
    breaks if: reject_when_check_fails=False also swallows a configuration failure
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project())

    util = build_util(
        context,
        build_job(COMPUTE_NODE_INSTANCE_PROFILE_ARN),
        queue_management=SocaQueueManagementParams(
            allowed_instance_profiles=[COMPUTE_NODE_INSTANCE_PROFILE_ARN]
        ),
    )
    with pytest.raises(exceptions.SocaException) as exc_info:
        util.check_bedrock(reject_when_check_fails=False)

    assert exc_info.value.error_code == errorcodes.BEDROCK_ACCESS_NOT_AUTHORIZED


def test_the_check_is_inert_while_bedrock_for_jobs_is_off(context, monkeypatch):
    """
    the project is fully provisioned for bedrock, but jobs were not opted in, so an
    existing cluster's provisioning is untouched.
    breaks if: the check ignores scheduler.bedrock.enabled
    """
    serve_project(monkeypatch, bedrock_project())

    build_util(context, build_job(COMPUTE_NODE_INSTANCE_PROFILE_ARN)).check_bedrock()


def test_a_job_in_a_project_without_bedrock_passes(context, monkeypatch):
    """
    breaks if: the check applies to projects that never asked for bedrock
    """
    enable_bedrock_for_jobs(context)
    serve_project(monkeypatch, bedrock_project(enabled=False))

    build_util(context, build_job(COMPUTE_NODE_INSTANCE_PROFILE_ARN)).check_bedrock()


def test_wait_and_configuration_states_are_classified_differently():
    """
    a job waiting on a reconcile must not burn its provisioning retry budget, and one
    waiting on an administrator must reach the retry cap and be held with the reason.
    breaks if: either error code is moved into or out of the wait list
    """
    assert errorcodes.BEDROCK_ACCESS_NOT_READY in PROVISIONING_WAIT_ERROR_CODES
    assert errorcodes.BEDROCK_ACCESS_NOT_AUTHORIZED not in PROVISIONING_WAIT_ERROR_CODES
