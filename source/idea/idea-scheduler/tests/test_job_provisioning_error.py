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
Test Cases for Job Provisioning Errors
"""

from ideadatamodel import errorcodes, exceptions
from ideasdk.utils import Utils
from ideascheduler.app.provisioning.job_monitor.job_cache import (
    ERROR_MESSAGE_MAX_LENGTH,
    JOB_PROVISIONING_ERRORS,
    JobCache,
    build_job_error_message,
)

from typing import Any, Dict, List


class MockScheduler:
    def __init__(self, exc: BaseException = None):
        self.exc = exc
        self.invocations: List[Dict[str, Any]] = []

    def set_job_attributes(self, job_id: str, attributes: Dict[str, Any]) -> bool:
        self.invocations.append({'job_id': job_id, 'attributes': attributes})
        if self.exc is not None:
            raise self.exc
        return True


def test_build_job_error_message_redacts_resource_identifiers():
    error_message = build_job_error_message(
        error_code=errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED,
        message='[CLOUDFORMATION_STACK_BUILDER_FAILED] cloudformation stack builder failed, '
        'Err: ami-0abc1234def56789a is not supported in subnet-01234567890abcdef, '
        'arn:aws:cloudformation:us-east-1:123456789012:stack/mock-stack/mock-uuid',
    )
    assert 'ami-' not in error_message
    assert 'subnet-' not in error_message
    assert 'arn:aws' not in error_message
    assert '123456789012' not in error_message
    assert error_message.startswith(
        f'{errorcodes.CLOUDFORMATION_STACK_BUILDER_FAILED}: cloudformation_stack_builder_failed'
    )


def test_build_job_error_message_removes_unsupported_characters():
    error_message = build_job_error_message(
        error_code=errorcodes.SERVICE_QUOTA_NOT_AVAILABLE,
        message="service quota not available for instance_types: ['c5.large', 'c5.xlarge']",
    )
    # a comma or a quote in the value would be parsed as an additional resource by qalter
    assert error_message == (
        'SERVICE_QUOTA_NOT_AVAILABLE: '
        'service_quota_not_available_for_instance_types:_c5.large_c5.xlarge'
    )


def test_build_job_error_message_is_bounded():
    error_message = build_job_error_message(
        error_code=errorcodes.GENERAL_ERROR, message='error ' * 100
    )
    assert len(error_message) == ERROR_MESSAGE_MAX_LENGTH
    assert not error_message.endswith('_')


def test_build_job_error_message_without_reason():
    error_message = build_job_error_message(
        error_code=errorcodes.NOT_ENOUGH_LICENSES, message=None
    )
    assert error_message == errorcodes.NOT_ENOUGH_LICENSES

    error_message = build_job_error_message(
        error_code=errorcodes.NOT_ENOUGH_LICENSES,
        message=errorcodes.NOT_ENOUGH_LICENSES,
    )
    assert error_message == errorcodes.NOT_ENOUGH_LICENSES


def test_job_provisioning_error_is_published_to_scheduler(context):
    scheduler = MockScheduler()
    context.scheduler = scheduler
    job_cache = JobCache(context=context)
    job_id = Utils.short_uuid()

    job_cache.set_job_provisioning_error(
        job_id=job_id,
        error_code=errorcodes.SERVICE_QUOTA_NOT_AVAILABLE,
        message='service quota not available',
    )
    assert len(scheduler.invocations) == 1
    assert scheduler.invocations[0] == {
        'job_id': job_id,
        'attributes': {
            'error_message': 'SERVICE_QUOTA_NOT_AVAILABLE: service_quota_not_available'
        },
    }

    # an unchanged error must not be published on every provisioning cycle
    job_cache.set_job_provisioning_error(
        job_id=job_id,
        error_code=errorcodes.SERVICE_QUOTA_NOT_AVAILABLE,
        message='service quota not available',
    )
    assert len(scheduler.invocations) == 1

    # successful provisioning clears the error
    job_cache.clear_job_provisioning_error(job_id=job_id)
    assert len(scheduler.invocations) == 2
    assert scheduler.invocations[1] == {
        'job_id': job_id,
        'attributes': {'error_message': None},
    }

    # nothing to clear
    job_cache.clear_job_provisioning_error(job_id=job_id)
    assert len(scheduler.invocations) == 2


def test_job_provisioning_error_is_saved_when_scheduler_update_fails(context):
    scheduler = MockScheduler(
        exc=exceptions.soca_exception(
            error_code=errorcodes.SCHEDULER_ERROR, message='Unknown Job Id'
        )
    )
    context.scheduler = scheduler
    job_cache = JobCache(context=context)
    job_id = Utils.short_uuid()

    job_cache.set_job_provisioning_error(
        job_id=job_id,
        error_code=errorcodes.NOT_ENOUGH_LICENSES,
        message='Not enough licenses available for: mock_license',
    )

    assert len(scheduler.invocations) == 1
    error = job_cache.get_connection()[JOB_PROVISIONING_ERRORS].find_one(job_id=job_id)
    assert error is not None
    assert error['error_code'] == errorcodes.NOT_ENOUGH_LICENSES
