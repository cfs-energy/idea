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
Test Cases for the scheduler integration test reporting

the status table formats every job id with '{:10s}'. a submission that returned no job id
used to reach that as None and take the whole suite down mid-report, so the run ended with a
failure verdict beside counters nothing had filled in.
"""

from typing import List, Optional

import pytest

from ideadatamodel import SocaJob, SubmitJobResult
from ideaadministrator.integration_tests.scheduler.job_test_case import (
    JobTestCase,
    JobSubmissionHelper,
)

TEST_CASE_CONFIG = {
    'name': 'hook_iam_role',
    'resource': 'instance_profile=FakeProfile',
    'command': '/bin/echo Test',
    'expected_output': 'Instance profile not found',
    'queue': 'normal',
    'error_code': 'JOB_SUBMISSION_FAILED',
}


class MockClusterConfig:
    @staticmethod
    def get_list(_key, required=False):
        return ['subnet-mock1']


class MockIdeaContext:
    def __init__(self):
        self.titles: List[str] = []

    def print_title(self, title):
        self.titles.append(title)


class MockTestContext:
    def __init__(self):
        self.admin_username = 'mockadmin'
        self.test_run_id = 'mock-run'
        self.cluster_config = MockClusterConfig()
        self.idea_context = MockIdeaContext()
        self.errors: List[str] = []
        self.scheduler_calls = 0

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        self.errors.append(message)

    def success(self, message):
        pass

    def get_scheduler_client(self):
        self.scheduler_calls += 1
        raise AssertionError('the scheduler must not be polled without a job id')


@pytest.fixture()
def helper():
    # __init__ reads job_test_cases.yml off the packaged resources dir; the reporting under
    # test only needs the context and the test case map
    instance = object.__new__(JobSubmissionHelper)
    instance.context = MockTestContext()
    instance.job_test_case_config = {}
    instance.job_test_cases = {}
    return instance


def build_test_case(helper, test_case_id: str, job_id: Optional[str]) -> JobTestCase:
    test_case = JobTestCase(
        helper=helper,
        test_case_id=test_case_id,
        base_os='amazonlinux2023',
        ami_id='ami-mock',
        test_case_config=dict(TEST_CASE_CONFIG),
    )
    test_case.job_submit_result = SubmitJobResult(
        accepted=job_id is not None, job=SocaJob(job_id=job_id)
    )
    test_case.status = 'IN_PROGRESS'
    helper.job_test_cases[test_case_id] = test_case
    return test_case


def test_get_job_id_reports_unknown_when_the_submission_carried_no_job_id(helper):
    test_case = build_test_case(helper, 'amazonlinux2023_hook_iam_role', None)
    assert test_case.get_job_id() == 'UNKNOWN'


def test_get_job_id_returns_the_job_id_when_the_submission_carried_one(helper):
    test_case = build_test_case(helper, 'amazonlinux2023_hello_world', '42')
    assert test_case.get_job_id() == '42'


def test_print_summary_renders_a_case_that_has_no_job_id(helper, capsys):
    build_test_case(helper, 'amazonlinux2023_hello_world', '42')
    build_test_case(helper, 'amazonlinux2023_hook_iam_role', None)

    helper.print_summary('Job Test Case Progress')

    rows = capsys.readouterr().out.splitlines()
    assert any('amazonlinux2023_hello_world' in row and '42' in row for row in rows)
    assert any(
        'amazonlinux2023_hook_iam_role' in row and 'UNKNOWN' in row for row in rows
    )


def test_check_progress_fails_a_case_that_has_no_job_id(helper):
    test_case = build_test_case(helper, 'amazonlinux2023_hook_iam_role', None)

    test_case.check_progress()

    # a case with no job id can never complete; polling it would spin until the loop expires
    assert test_case.status == 'FAIL'
    assert helper.context.scheduler_calls == 0
    assert any('hook_iam_role' in message for message in helper.context.errors)


def test_counts_report_a_case_with_no_job_id_as_failed(helper):
    build_test_case(helper, 'amazonlinux2023_hello_world', '42')
    no_job_id = build_test_case(helper, 'amazonlinux2023_hook_iam_role', None)

    no_job_id.check_progress()

    assert helper.get_test_case_count() == 2
    assert helper.get_failed_count() == 1
    assert helper.get_success_count() == 0
    assert helper.get_completed_count() == 1
