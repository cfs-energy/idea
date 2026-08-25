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
Test Cases for floating license availability

a license server that answers with zero seats and a license server that cannot be reached
are different outcomes: the first rejects a submission, the second must not.
"""

import subprocess

import pytest

from ideadatamodel import (
    exceptions,
    errorcodes,
    GetHpcLicenseResourceResult,
    HpcLicenseResource,
    SocaJob,
    SocaJobLicenseAsk,
    SocaJobParams,
)
from ideascheduler.app.app_protocols import LicenseAvailability
from ideascheduler.app.licenses.license_service import (
    DEFAULT_AVAIL_CHECK_TIMEOUT_SECONDS,
    LicenseService,
)
from ideascheduler.app.licenses.license_resources_dao import LicenseResourcesDAO
from ideascheduler.app.provisioning import JobProvisioningUtil

LICENSE_NAME = 'mock_lic_app'


class MockShellResult:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ''

    def __str__(self):
        return f'returncode: {self.returncode} >> stdout: {self.stdout}'


class MockShell:
    """
    stands in for the ShellInvoker that runs the availability check command.
    raises is what a hung or unreachable license server looks like to the caller.
    """

    def __init__(self, returncode: int = 0, stdout: str = '0', raises=None):
        self.returncode = returncode
        self.stdout = stdout
        self.raises = raises
        self.invocations = []

    def invoke(self, cmd=None, timeout=None, **_):
        self.invocations.append({'cmd': cmd, 'timeout': timeout})
        if self.raises is not None:
            raise self.raises
        return MockShellResult(returncode=self.returncode, stdout=self.stdout)


class MockLicenseService:
    def __init__(self, availability: LicenseAvailability):
        self.availability = availability

    def get_license_availability(self, license_resource_name: str):
        return self.availability


class MockJobCache:
    def __init__(self, active_count: int = 0):
        self.active_count = active_count

    def get_active_license_count(self, license_name: str) -> int:
        return self.active_count


def build_license_service(
    context, monkeypatch, shell: MockShell, reserved_count: int = 0
) -> LicenseService:
    monkeypatch.setattr(LicenseResourcesDAO, '__init__', lambda self, context: None)
    monkeypatch.setattr(LicenseResourcesDAO, 'initialize', lambda self: None)

    context.shell = shell
    service = LicenseService(context=context)

    license_resource = HpcLicenseResource(
        name=LICENSE_NAME,
        title='Mock App License',
        availability_check_cmd='/apps/soca/check_lic.py -s mock-license-server',
        reserved_count=reserved_count,
    )
    monkeypatch.setattr(
        service,
        'get_license_resource',
        lambda request: GetHpcLicenseResourceResult(license_resource=license_resource),
    )
    return service


def build_job(license_count: int = 2) -> SocaJob:
    return SocaJob(
        name='mock-job',
        job_id='1',
        job_uid='mock-job-uid',
        queue='normal',
        params=SocaJobParams(
            nodes=1,
            licenses=[SocaJobLicenseAsk(name=LICENSE_NAME, count=license_count)],
        ),
    )


def build_provisioning_util(
    context, availability: LicenseAvailability, active_count: int = 0
) -> JobProvisioningUtil:
    context.license_service = MockLicenseService(availability)
    context.job_cache = MockJobCache(active_count=active_count)
    return JobProvisioningUtil(context=context, jobs=[build_job()])


def test_license_server_reporting_zero_seats_is_an_answer(context, monkeypatch):
    """
    a reachable license server with no free seats reports zero, and that zero is real
    """
    service = build_license_service(
        context, monkeypatch, MockShell(returncode=0, stdout='0')
    )

    availability = service.get_license_availability(license_resource_name=LICENSE_NAME)

    assert availability.check_ok is True
    assert availability.available_count == 0
    assert availability.error is None


def test_license_server_reporting_seats_subtracts_reserved(context, monkeypatch):
    """
    happy path: the reported count minus the reserved count, with the check marked usable
    """
    service = build_license_service(
        context, monkeypatch, MockShell(returncode=0, stdout='12'), reserved_count=2
    )

    availability = service.get_license_availability(license_resource_name=LICENSE_NAME)

    assert availability.check_ok is True
    assert availability.available_count == 10


def test_unreachable_license_server_is_not_zero_seats(context, monkeypatch):
    """
    an unreachable server must not look like a server reporting no free seats
    """
    service = build_license_service(
        context,
        monkeypatch,
        MockShell(raises=OSError('mock-license-server: no route to host')),
    )

    availability = service.get_license_availability(license_resource_name=LICENSE_NAME)

    assert availability.check_ok is False
    assert LICENSE_NAME in availability.error


def test_timed_out_license_server_is_not_zero_seats(context, monkeypatch):
    """
    a hung license server is bounded by the invocation timeout and reported as unusable
    """
    service = build_license_service(
        context,
        monkeypatch,
        MockShell(
            raises=subprocess.TimeoutExpired(cmd='check_lic.py', timeout=10),
        ),
    )

    availability = service.get_license_availability(license_resource_name=LICENSE_NAME)

    assert availability.check_ok is False
    assert LICENSE_NAME in availability.error


def test_unparsable_check_output_is_not_zero_seats(context, monkeypatch):
    """
    output that is not a seat count means the check failed, not that seats are exhausted
    """
    service = build_license_service(
        context,
        monkeypatch,
        MockShell(returncode=0, stdout='lmgrd: cannot connect to license server'),
    )

    availability = service.get_license_availability(license_resource_name=LICENSE_NAME)

    assert availability.check_ok is False
    assert availability.available_count == 0


def test_failed_check_command_is_not_zero_seats(context, monkeypatch):
    """
    a non-zero exit from the check command is a failed check, not a zero seat count
    """
    service = build_license_service(
        context, monkeypatch, MockShell(returncode=1, stdout='')
    )

    availability = service.get_license_availability(license_resource_name=LICENSE_NAME)

    assert availability.check_ok is False


def test_availability_check_is_bounded_by_a_timeout(context, monkeypatch):
    """
    the check shells out on the submission path, so it must always carry a timeout
    """
    shell = MockShell(returncode=0, stdout='4')
    service = build_license_service(context, monkeypatch, shell)

    service.get_license_availability(license_resource_name=LICENSE_NAME)

    assert len(shell.invocations) == 1
    assert shell.invocations[0]['timeout'] == DEFAULT_AVAIL_CHECK_TIMEOUT_SECONDS
    assert shell.invocations[0]['timeout'] > 0


def test_submission_rejects_when_server_reports_not_enough_seats(context):
    """
    a server that answers still rejects the submission, naming the license resource
    """
    util = build_provisioning_util(
        context, LicenseAvailability(available_count=1, check_ok=True)
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        util.check_licenses(reject_when_check_fails=False)

    assert exc_info.value.error_code == errorcodes.NOT_ENOUGH_LICENSES
    assert LICENSE_NAME in exc_info.value.message


def test_submission_allowed_when_license_server_cannot_be_reached(context):
    """
    an unusable check must not reject the job at submission
    """
    util = build_provisioning_util(
        context,
        LicenseAvailability(
            available_count=0, check_ok=False, error='mock-license-server unreachable'
        ),
    )

    util.check_licenses(reject_when_check_fails=False)


def test_provisioning_still_fails_closed_when_license_server_cannot_be_reached(context):
    """
    provisioning keeps the old behaviour: an unusable check counts as zero and the job
    stays queued for retry
    """
    util = build_provisioning_util(
        context,
        LicenseAvailability(
            available_count=0, check_ok=False, error='mock-license-server unreachable'
        ),
    )

    with pytest.raises(exceptions.SocaException) as exc_info:
        util.check_licenses()

    assert exc_info.value.error_code == errorcodes.NOT_ENOUGH_LICENSES


def test_enough_seats_passes_both_call_sites(context):
    """
    with seats available neither submission nor provisioning rejects the job
    """
    util = build_provisioning_util(
        context, LicenseAvailability(available_count=8, check_ok=True), active_count=3
    )

    util.check_licenses(reject_when_check_fails=False)
    util.check_licenses()
