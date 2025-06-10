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

from ideascheduler import AppContext
from ideadatamodel import exceptions, errorcodes, constants
from ideadatamodel.scheduler import (
    CreateHpcLicenseResourceRequest,
    CreateHpcLicenseResourceResult,
    GetHpcLicenseResourceRequest,
    GetHpcLicenseResourceResult,
    UpdateHpcLicenseResourceRequest,
    UpdateHpcLicenseResourceResult,
    DeleteHpcLicenseResourceRequest,
    DeleteHpcLicenseResourceResult,
    ListHpcLicenseResourcesRequest,
    ListHpcLicenseResourcesResult,
    CheckHpcLicenseResourceAvailabilityRequest,
    CheckHpcLicenseResourceAvailabilityResult,
    HpcLicenseResource,
)
from ideasdk.utils import Utils
from ideasdk.shell.shell_invoker import ShellInvocationResult
from ideascheduler.app.app_protocols import LicenseServiceProtocol
from ideascheduler.app.licenses.license_resources_dao import LicenseResourcesDAO
from ideascheduler.app.scheduler.openpbs.openpbs_constants import (
    CONFIG_FILE_SCHED_CONFIG,
    CONFIG_FILE_RESOURCE_DEF,
)

from typing import Tuple, Optional
import re

PATTERN_LICENSE_RESOURCE_NAME = re.compile('(^[a-z][a-z0-9]*)_lic_([a-z][a-z0-9]*)')
AVAIL_CHECK_DENIED_KEYWORDS = [
    'rm',
    'sudo',
    '..',
    'systemctl',
    'reboot',
    'shutdown',
    'cd',
    '&&',
]


class LicenseService(LicenseServiceProtocol):
    def __init__(self, context: AppContext):
        self.context = context
        self.logger = context.logger('license-service')

        self.license_resources_dao = LicenseResourcesDAO(self.context)
        self.license_resources_dao.initialize()

    def cache_set(self, license_resource: HpcLicenseResource):
        return (
            self.context.cache()
            .short_term()
            .set(f'hpc-license-resource.{license_resource.name}', license_resource)
        )

    def cache_get(self, name: str):
        return self.context.cache().short_term().get(f'hpc-license-resource.{name}')

    def cache_clear(self, name: str):
        self.context.cache().short_term().delete(f'hpc-license-resource.{name}')

    def validate_license_resource(
        self, license_resource: HpcLicenseResource, dry_run: bool = False
    ):
        if Utils.is_empty(license_resource):
            raise exceptions.invalid_params('license_resource is required')
        if Utils.is_empty(license_resource.name):
            raise exceptions.invalid_params('license_resource.name is required')
        if Utils.is_empty(license_resource.title):
            raise exceptions.invalid_params('license_resource.title is required')

        availability_check_cmd = license_resource.availability_check_cmd
        if Utils.is_empty(availability_check_cmd):
            raise exceptions.invalid_params(
                'license_resource.availability_check_cmd is required'
            )

        name = license_resource.name
        if not PATTERN_LICENSE_RESOURCE_NAME.match(name):
            raise exceptions.invalid_params(
                f'license resource name must match the pattern: {PATTERN_LICENSE_RESOURCE_NAME}'
            )

        availability_check_cmd_tokens = availability_check_cmd.split(' ')
        for denied_keyword in AVAIL_CHECK_DENIED_KEYWORDS:
            if denied_keyword in availability_check_cmd_tokens:
                raise exceptions.invalid_params(
                    f'availability_check_cmd contains restricted keywords: [{", ".join(AVAIL_CHECK_DENIED_KEYWORDS)}]'
                )

        available_count, result, exc = self._check_license_availability(
            availability_check_cmd
        )
        if exc is not None:
            raise exc
        if result.returncode != 0:
            raise exceptions.invalid_params(
                f'failed to check license availability: {result}'
            )

        if dry_run:
            return

        with open(CONFIG_FILE_RESOURCE_DEF, 'r') as f:
            content = f.read()
            if f'{license_resource.name} type=long' not in content:
                raise exceptions.soca_exception(
                    error_code=errorcodes.CONFIG_ERROR,
                    message=f'license resource: {license_resource.name} not found in {CONFIG_FILE_RESOURCE_DEF}',
                )

        with open(CONFIG_FILE_SCHED_CONFIG, 'r') as f:
            content = f.read()
            lines = content.splitlines()
            resources_found = False
            server_dyn_res_found = False
            for line in lines:
                if line.startswith('resources:'):
                    if license_resource.name in line:
                        resources_found = True
                        continue
                if line.startswith('server_dyn_res:'):
                    if license_resource.name in line:
                        server_dyn_res_found = True
                        continue
            if not resources_found:
                raise exceptions.soca_exception(
                    error_code=errorcodes.CONFIG_ERROR,
                    message=f'license resource: {license_resource.name} not found under "resources" in {CONFIG_FILE_SCHED_CONFIG}',
                )
            if not server_dyn_res_found:
                raise exceptions.soca_exception(
                    error_code=errorcodes.CONFIG_ERROR,
                    message=f'license resource: {license_resource.name} not found under "server_dyn_res" in {CONFIG_FILE_SCHED_CONFIG}',
                )

    def create_license_resource(
        self, request: CreateHpcLicenseResourceRequest
    ) -> CreateHpcLicenseResourceResult:
        license_resource = request.license_resource

        dry_run = Utils.get_as_bool(request.dry_run, False)
        self.validate_license_resource(license_resource, dry_run)

        existing = self.license_resources_dao.get_license_resource(
            license_resource.name
        )
        if existing is not None:
            raise exceptions.invalid_params(
                f'an existing license resource already exists with name: {license_resource.name}'
            )

        if dry_run:
            created_license_resource = license_resource
        else:
            db_license_resource = self.license_resources_dao.convert_to_db(
                license_resource
            )
            db_created = self.license_resources_dao.create_license_resource(
                db_license_resource
            )
            created_license_resource = self.license_resources_dao.convert_from_db(
                db_created
            )
            self.cache_set(created_license_resource)

        return CreateHpcLicenseResourceResult(license_resource=created_license_resource)

    def get_license_resource(
        self, request: GetHpcLicenseResourceRequest
    ) -> GetHpcLicenseResourceResult:
        name = request.name
        if Utils.is_empty(request.name):
            raise exceptions.invalid_params('name is required')

        license_resource = self.cache_get(name)
        if license_resource is not None:
            return GetHpcLicenseResourceResult(license_resource=license_resource)

        db_license_resource = self.license_resources_dao.get_license_resource(name)
        if db_license_resource is None:
            raise exceptions.invalid_params(
                f'license resource not found for name: {name}'
            )

        license_resource = self.license_resources_dao.convert_from_db(
            db_license_resource
        )

        return GetHpcLicenseResourceResult(license_resource=license_resource)

    def update_license_resource(
        self, request: UpdateHpcLicenseResourceRequest
    ) -> UpdateHpcLicenseResourceResult:
        license_resource = request.license_resource
        dry_run = Utils.get_as_bool(request.dry_run, False)
        self.validate_license_resource(license_resource, dry_run)

        existing = self.license_resources_dao.get_license_resource(
            license_resource.name
        )
        if existing is None:
            raise exceptions.invalid_params(
                f'license resource not found for name: {license_resource.name}'
            )

        dry_run = Utils.get_as_bool(request.dry_run, False)

        if dry_run:
            updated_license_resource = license_resource
        else:
            db_license_resource = self.license_resources_dao.convert_to_db(
                license_resource
            )
            db_updated = self.license_resources_dao.update_license_resource(
                db_license_resource
            )
            updated_license_resource = self.license_resources_dao.convert_from_db(
                db_updated
            )
            self.cache_set(updated_license_resource)

        return UpdateHpcLicenseResourceResult(license_resource=updated_license_resource)

    def delete_license_resource(
        self, request: DeleteHpcLicenseResourceRequest
    ) -> DeleteHpcLicenseResourceResult:
        name = request.name
        if Utils.is_empty(request.name):
            raise exceptions.invalid_params('name is required')

        db_license_resource = self.license_resources_dao.get_license_resource(name)
        if db_license_resource is None:
            raise exceptions.invalid_params(
                f'license resource not found for name: {name}'
            )

        self.license_resources_dao.delete_license_resource(name)

        self.cache_clear(name)

        return DeleteHpcLicenseResourceResult()

    def list_license_resources(
        self, request: ListHpcLicenseResourcesRequest
    ) -> ListHpcLicenseResourcesResult:
        return self.license_resources_dao.list_license_resources(request)

    def check_license_resource_availability(
        self, request: CheckHpcLicenseResourceAvailabilityRequest
    ) -> CheckHpcLicenseResourceAvailabilityResult:
        get_result = self.get_license_resource(
            GetHpcLicenseResourceRequest(name=request.name)
        )
        license_resource = get_result.license_resource

        actual_available_count, result, exc = self._check_license_availability(
            cmd=license_resource.availability_check_cmd
        )

        if exc is not None:
            raise exc

        if result.returncode != 0:
            raise exceptions.soca_exception(
                error_code=errorcodes.SCHEDULER_INVALID_LICENSE_RESOURCE_CONFIGURATION,
                message=f'Invalid Configuration: {result}',
            )

        reserved_count = Utils.get_as_int(license_resource.reserved_count, 0)

        available_count = max((actual_available_count - reserved_count), 0)

        return CheckHpcLicenseResourceAvailabilityResult(
            available_count=available_count
        )

    def _check_license_availability(
        self, cmd: str
    ) -> Tuple[int, Optional[ShellInvocationResult], Optional[Exception]]:
        """
        execute the cmd as shell
        """
        try:
            if Utils.is_empty(cmd):
                raise exceptions.invalid_params('cmd is required')
            cmd_tokens = [
                'su',
                constants.IDEA_SERVICE_ACCOUNT,
                '--shell',
                '/bin/bash',
                '--command',
                f'cd /tmp && {cmd}',
            ]
            result = self.context.shell.invoke(cmd_tokens)
            if result.returncode == 0:
                return int(result.stdout), result, None
            else:
                return 0, result, None
        except Exception as e:
            return 0, None, e

    def get_available_licenses(self, license_resource_name: str) -> int:
        """
        run the check license script to find available licenses

        in case of error or any other exception, returns available licenses as zero

        :param license_resource_name: the name of the license resource configured in the license_mapping.yml file
        :return: int - available licenses as returned by the check_licences.py script
        """
        try:
            license_resource_result = self.get_license_resource(
                GetHpcLicenseResourceRequest(name=license_resource_name)
            )

            license_resource = license_resource_result.license_resource

            availability_check_cmd = license_resource.availability_check_cmd
            if Utils.is_empty(availability_check_cmd):
                return 0

            available_count, result, exc = self._check_license_availability(
                availability_check_cmd
            )
            if exc is not None:
                self.logger.error(
                    f'failed to check available licences for {license_resource_name}: {exc}'
                )
                return 0

            if result.returncode != 0:
                self.logger.error(
                    f'failed to check available licences for {license_resource_name}: {result}'
                )
                return 0

            reserved_count = Utils.get_as_int(license_resource.reserved_count, 0)

            return max((available_count - reserved_count), 0)

        except Exception as e:
            self.logger.error(
                f'exception while checking available licenses for: {license_resource_name} - {e}'
            )
            return 0
