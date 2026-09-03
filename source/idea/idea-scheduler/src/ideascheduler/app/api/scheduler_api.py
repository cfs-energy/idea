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

from ideadatamodel import constants, errorcodes, exceptions, SocaPaginator, SocaFilter
from ideadatamodel.scheduler import (
    SocaJob,
    ListJobsRequest,
    ListJobsResult,
    GetUserApplicationsRequest,
    DryRunOption,
    SubmitJobRequest,
    SubmitJobResult,
    GetInstanceTypeOptionsRequest,
    GetInstanceTypeOptionsResult,
    GetJobRequest,
    GetJobResult,
    DeleteJobRequest,
    DeleteJobResult,
)
from ideasdk.api import BaseAPI, ApiInvocationContext
from ideasdk.utils import Utils, GroupNameHelper
from ideascheduler.app.api.job_waiting_signals import apply_waiting_signals
from ideascheduler.app.scheduler.job_param_builder import (
    JobParamsBuilderContext,
    InstanceTypesParamBuilder,
)

from cacheout import Cache
from threading import RLock
from typing import Optional

import ideascheduler
import grp
import os
import pwd
import re
import shutil

# how long a completed submission can be replayed for: must be at least
# SERVICE_WORKER_REPLY_TIMEOUT (660s), the delay before the webapp shows a client timeout.
JOB_SUBMISSION_DEDUPE_TTL_SECONDS = 900
JOB_SUBMISSION_DEDUPE_MAX_ENTRIES = 512
# same shape as the api envelope's request_id: the value is used as a cache key and is logged.
PATTERN_CLIENT_SUBMISSION_ID = r'^[0-9a-zA-Z-_]{1,64}$'

# cross-module read: the maintenance window is owned by cluster-manager. read on every
# submission, never cached, so closing the window takes effect straight away.
MAINTENANCE_ENABLED_CONFIG_KEY = 'cluster-manager.maintenance.enabled'
MAINTENANCE_MESSAGE_CONFIG_KEY = 'cluster-manager.maintenance.message'
DEFAULT_MAINTENANCE_MESSAGE = 'This cluster is undergoing maintenance.'

# the job name is taken from the user-supplied '#PBS -N' line and is only used as a
# cosmetic filename prefix. it must never influence the path a root process writes to.
JOB_NAME_PREFIX_MAX_LENGTH = 64
PATTERN_JOB_NAME_DISALLOWED = re.compile(r'[^A-Za-z0-9._-]')


def sanitize_job_name_prefix(job_name: Optional[str]) -> str:
    """
    reduce an untrusted '#PBS -N' job name to a safe filename prefix.

    collapses any path structure to a single component, restricts the charset to
    [A-Za-z0-9._-], strips leading/trailing dots (so the result can never be '.' or
    '..'), and caps the length. the return value never contains a path separator and
    can never escape the intended directory when joined into a filename.
    """
    if Utils.is_empty(job_name):
        return ''
    candidate = os.path.basename(job_name.strip())
    candidate = PATTERN_JOB_NAME_DISALLOWED.sub('_', candidate)
    candidate = candidate.strip('.')
    return candidate[:JOB_NAME_PREFIX_MAX_LENGTH]


def open_new_job_script(path: str, dir_fd: Optional[int] = None):
    """
    open a brand-new job script for writing under a root process.

    O_EXCL fails if anything already exists at the path (a pre-planted file cannot be
    overwritten); O_NOFOLLOW refuses to follow a symlink at the final component (a
    pre-planted symlink cannot redirect the write). mode 0o600 keeps the root-owned
    script from being world-readable/writable before ownership is handed to the user.
    with dir_fd, path is resolved relative to an already opened directory, so a directory
    swapped for a symlink after it was checked cannot redirect the write either.
    """
    fd = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
        dir_fd=dir_fd,
    )
    return os.fdopen(fd, 'w')


def chown_fd(fd: int, user: str, group: str):
    """hand the open file to its owner; the fd cannot be redirected the way a path can"""
    os.fchown(fd, pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid)


class JobSubmissionEntry:
    def __init__(self, fingerprint: str, result: Optional[SubmitJobResult] = None):
        self.fingerprint = fingerprint
        self.result = result


class JobSubmissionDeduplicator:
    """
    collapses repeat deliveries of a single job submission into one qsub.

    Scheduler.SubmitJob is not idempotent on its own: job_uid is minted server side per call,
    so every delivery of the same submission - a double click that beats the portal's loading
    state, a client or proxy retry after a read timeout - creates another job and therefore
    another compute stack.

    an entry is keyed on (username, client_submission_id) and carries a fingerprint of the
    request. a result is replayed only when the fingerprint matches, so a client that reuses a
    submission id for a different job script gets a fresh submission and never another job's
    result. only accepted submissions are cached: a rejected one must be resubmittable.
    """

    def __init__(self):
        self._lock = RLock()
        self._cache = Cache(
            maxsize=JOB_SUBMISSION_DEDUPE_MAX_ENTRIES,
            ttl=JOB_SUBMISSION_DEDUPE_TTL_SECONDS,
        )

    @staticmethod
    def build_key(username: str, client_submission_id: str) -> str:
        return f'{username}:{client_submission_id}'

    @staticmethod
    def build_fingerprint(request: SubmitJobRequest) -> str:
        return Utils.sha256(
            Utils.to_json(
                {
                    'project': request.project,
                    'job_script_interpreter': request.job_script_interpreter,
                    'job_script': request.job_script,
                }
            )
        )

    def begin(self, key: str, fingerprint: str) -> Optional[SubmitJobResult]:
        """
        claim the key for this submission.

        returns the result of an identical submission that already completed, or None when the
        caller owns the submission and must call commit() or rollback() for the key.
        raises when an identical submission is still in flight - the two concurrent halves of a
        double click land here, and only one of them may reach qsub.
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry.fingerprint == fingerprint:
                if entry.result is not None:
                    return entry.result
                raise exceptions.soca_exception(
                    error_code=errorcodes.JOB_SUBMISSION_IN_PROGRESS,
                    message='An identical job submission is already in progress.',
                )
            self._cache.set(key, JobSubmissionEntry(fingerprint=fingerprint))
            return None

    def commit(self, key: str, fingerprint: str, result: SubmitJobResult):
        with self._lock:
            self._cache.set(
                key, JobSubmissionEntry(fingerprint=fingerprint, result=result)
            )

    def rollback(self, key: str):
        with self._lock:
            self._cache.delete(key)


class SchedulerAPI(BaseAPI):
    def __init__(self, context: ideascheduler.AppContext):
        self.context = context
        self.logger = context.logger('scheduler-api')
        self.group_name_helper = GroupNameHelper(context)
        self.job_submission_deduplicator = JobSubmissionDeduplicator()

        self.SCOPE_WRITE = f'{self.context.module_id()}/write'
        self.SCOPE_READ = f'{self.context.module_id()}/read'

        self.acl = {
            'Scheduler.ListActiveJobs': {
                'scope': self.SCOPE_READ,
                'method': self.list_active_jobs,
            },
            'Scheduler.ListCompletedJobs': {
                'scope': self.SCOPE_READ,
                'method': self.list_completed_jobs,
            },
            'Scheduler.GetUserApplications': {
                'scope': self.SCOPE_READ,
                'method': self.get_user_applications,
            },
            'Scheduler.SubmitJob': {
                'scope': self.SCOPE_WRITE,
                'method': self.submit_job,
            },
            'Scheduler.DeleteJob': {
                'scope': self.SCOPE_WRITE,
                'method': self.delete_job,
            },
            'Scheduler.GetActiveJob': {
                'scope': self.SCOPE_READ,
                'method': self.get_active_job,
            },
            'Scheduler.GetCompletedJob': {
                'scope': self.SCOPE_READ,
                'method': self.get_completed_job,
            },
            'Scheduler.GetInstanceTypeOptions': {
                'scope': self.SCOPE_READ,
                'method': self.get_instance_type_options,
            },
        }

    def list_active_jobs(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(ListJobsRequest)
        page_size = payload.page_size
        page_start = payload.page_start
        entries = self.context.job_cache.list_jobs(
            owner=context.get_username(), _limit=page_size, _offset=page_start
        )
        # enriched here rather than in the job cache: the signals are per-request and
        # must not be written back into the cached job_data by an internal sync.
        apply_waiting_signals(context=self.context, jobs=entries)
        total = self.context.job_cache.get_count(owner=context.get_username())

        context.success(
            ListJobsResult(
                paginator=SocaPaginator(
                    total=total, page_size=payload.page_size, start=payload.page_start
                ),
                listing=entries,
            )
        )

    @staticmethod
    def get_scoped_username(context: ApiInvocationContext) -> str:
        """
        the username every job read is scoped to. an empty value is refused rather than
        passed on: the term filter for an empty value is dropped, which matches every
        owner instead of none.
        """
        username = context.get_username()
        if Utils.is_empty(username):
            raise exceptions.unauthorized_access()
        return username

    def list_completed_jobs(self, context: ApiInvocationContext):
        payload = context.get_request_payload_as(ListJobsRequest)
        username = self.get_scoped_username(context)

        if self.context.document_store.is_enabled():
            # owner is server owned: strip any client-supplied owner/owner.raw filter
            # before adding our own, so a client cannot inject an owner term.
            filters = []
            for search_filter in payload.filters or []:
                if search_filter.key in ('owner', 'owner.raw'):
                    continue
                filters.append(search_filter)

            # owner.raw, not owner: the analysed field is tokenised, so a hyphenated username
            # won't match its own docs and a substring username can cross-match. see DocumentStore._search.
            owner_filter = SocaFilter(key='owner.raw', value=username)
            filters.append(owner_filter)
            payload.filters = filters
            result = self.context.document_store.search_jobs(payload)
        else:
            payload = context.get_request_payload_as(ListJobsRequest)
            page_size = payload.page_size
            page_start = payload.page_start
            entries = self.context.job_cache.list_completed_jobs(
                owner=username, _limit=page_size, _offset=page_start
            )
            total = self.context.job_cache.get_completed_jobs_count(owner=username)
            result = ListJobsResult(
                paginator=SocaPaginator(
                    total=total, page_size=payload.page_size, start=payload.page_start
                ),
                listing=entries,
            )

        context.success(result)

    def get_user_applications(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetUserApplicationsRequest)
        request.username = context.get_username()
        result = self.context.applications.get_user_applications(request)
        return context.success(result)

    def check_maintenance(self):
        """
        Refuse new work while a maintenance window is open. Checked here rather than in the
        OpenPBS submit hook: the hook runs inside the PBS server, so once the scheduler is
        stopped nothing runs it and qsub fails with a generic connection error instead.
        """
        config = self.context.config()
        if not config.get_bool(MAINTENANCE_ENABLED_CONFIG_KEY, default=False):
            return
        message = Utils.get_as_string(
            config.get_string(MAINTENANCE_MESSAGE_CONFIG_KEY, default=''), ''
        ).strip()
        if Utils.is_empty(message):
            message = DEFAULT_MAINTENANCE_MESSAGE
        raise exceptions.soca_exception(
            error_code=errorcodes.JOB_SUBMISSION_FAILED,
            message=f'Cluster is in maintenance: {message}',
        )

    def submit_job(self, context: ApiInvocationContext):
        # before any other validation, so a closed cluster is reported as closed rather
        # than as a malformed request
        self.check_maintenance()

        request = context.get_request_payload_as(SubmitJobRequest)
        job_owner = request.job_owner
        context_user = context.get_username()

        if Utils.is_empty(job_owner):
            job_owner = context.get_username()

        if Utils.is_empty(context_user):
            raise exceptions.invalid_params('Empty context user')

        if context_user != job_owner:
            raise exceptions.invalid_params(
                'Mismatched user information in job request'
            )

        job_script = request.job_script
        if Utils.is_empty(job_script):
            raise exceptions.invalid_params(
                'job_script is required in form of base64 encoded job script.'
            )

        job_script_interpreter = request.job_script_interpreter
        if Utils.is_empty(job_script_interpreter):
            raise exceptions.invalid_params('job_script_interpreter is required.')
        if job_script_interpreter not in ('pbs', 'bash'):
            raise exceptions.invalid_params(
                'job_script_interpreter must be one of [pbs, bash]'
            )

        client_submission_id = request.client_submission_id
        if Utils.is_not_empty(client_submission_id) and not re.match(
            PATTERN_CLIENT_SUBMISSION_ID, client_submission_id
        ):
            raise exceptions.invalid_params(
                f'client_submission_id must satisfy regex: {PATTERN_CLIENT_SUBMISSION_ID}'
            )

        dry_run = DryRunOption.resolve(request.dry_run)

        # a dry run creates no capacity, so it is never deduplicated - and it must not share a
        # key with the real submission that follows it from the same form.
        if dry_run is not None or Utils.is_empty(client_submission_id):
            context.success(
                self._submit_job(request=request, job_owner=job_owner, dry_run=dry_run)
            )
            return

        dedupe_key = JobSubmissionDeduplicator.build_key(
            username=context_user, client_submission_id=client_submission_id
        )
        dedupe_fingerprint = JobSubmissionDeduplicator.build_fingerprint(request)

        submitted_result = self.job_submission_deduplicator.begin(
            key=dedupe_key, fingerprint=dedupe_fingerprint
        )
        if submitted_result is not None:
            self.logger.info(
                f'duplicate job submission suppressed for user: {context_user}, '
                f'client_submission_id: {client_submission_id}'
            )
            context.success(submitted_result)
            return

        try:
            result = self._submit_job(
                request=request, job_owner=job_owner, dry_run=dry_run
            )
        except BaseException:
            self.job_submission_deduplicator.rollback(dedupe_key)
            raise

        if Utils.get_as_bool(result.accepted, False):
            self.job_submission_deduplicator.commit(
                key=dedupe_key, fingerprint=dedupe_fingerprint, result=result
            )
        else:
            # nothing was queued, so the same submission must be able to run again
            self.job_submission_deduplicator.rollback(dedupe_key)

        context.success(result)

    def _submit_job(
        self,
        request: SubmitJobRequest,
        job_owner: str,
        dry_run: Optional[DryRunOption],
    ) -> SubmitJobResult:
        job_script = request.job_script
        job_script_interpreter = request.job_script_interpreter

        data_dir = self.context.config().get_string(
            'shared-storage.data.mount_dir', required=True
        )
        job_submission_dir = os.path.join(data_dir, 'home', job_owner, 'jobs')

        if Utils.is_symlink(job_submission_dir):
            raise exceptions.general_exception(
                f'a symbolic link exists at location: {job_submission_dir}. delete the symbolic link and try again.'
            )

        if not Utils.is_dir(job_submission_dir):
            os.makedirs(job_submission_dir)
            group_name = self.group_name_helper.get_user_group(job_owner)
            shutil.chown(job_submission_dir, user=job_owner, group=group_name)

        job_uid = Utils.short_uuid()
        # Try to extract job name from PBS script if present
        job_name = None
        script_content = Utils.base64_decode(job_script)
        for line in script_content.split('\n'):
            if line.startswith('#PBS -N '):
                job_name = line.replace('#PBS -N ', '').strip()
                break

        # job name is untrusted and cosmetic; sanitizing it to a bare prefix keeps a
        # '#PBS -N /etc/profile.d/x' value from making os.path.join absolute or escaping the dir.
        job_name_prefix = sanitize_job_name_prefix(job_name)
        filename_base = f'{job_name_prefix}_{job_uid}' if job_name_prefix else job_uid

        extension = '.que' if job_script_interpreter == 'pbs' else '.sh'
        script_name = f'{filename_base}{extension}'
        job_submit_script = os.path.join(job_submission_dir, script_name)

        # defense in depth: the resolved target must resolve to a direct child of the
        # user's jobs dir. rejects any residual traversal or a symlinked directory.
        real_dir = os.path.realpath(job_submission_dir)
        real_target = os.path.realpath(job_submit_script)
        if os.path.dirname(real_target) != real_dir:
            raise exceptions.general_exception(
                f'refusing to write job script outside {job_submission_dir}'
            )

        group_name = self.group_name_helper.get_user_group(job_owner)
        # the script is created relative to the opened jobs dir and chowned through its fd,
        # so nothing swapped in between the checks above and the write can redirect it.
        dir_fd = os.open(
            job_submission_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            with open_new_job_script(script_name, dir_fd=dir_fd) as f:
                f.write(Utils.base64_decode(job_script))
                f.write('\n')
                chown_fd(f.fileno(), user=job_owner, group=group_name)
        finally:
            os.close(dir_fd)

        # the script stays in the owner's jobs dir only when the submit command succeeded;
        # a refusal, a dry run or an exception before then leaves no file behind.
        try:
            if job_script_interpreter == 'pbs':
                job_submit_command = ['cd', job_submission_dir, '&&', 'qsub']
                if dry_run is not None:
                    job_submit_command += ['-l', f'dry_run={dry_run}']
                if Utils.is_not_empty(request.project):
                    job_submit_command += ['-P', f'"{request.project}"']

                job_submit_command += ['-l', f'job_uid={job_uid}', job_submit_script]
                job_submit_command_str = ' '.join(job_submit_command)
                command = ['su', job_owner, '-c', f'{job_submit_command_str}']

                self.logger.info(' '.join(command))
                result = self.context.shell.invoke(command)

                job_id = None
                if result.returncode == 0:
                    job_id = str(result.stdout).split('.')[0]

                # read the tracker before surfacing failure: the PBS submit hook's structured
                # result (accepted=False + reasons) is what the caller needs, not the raw shell repr.
                submission_result = self.context.job_submission_tracker.get(job_uid)
                if isinstance(submission_result, BaseException):
                    raise submission_result

                if submission_result is None:
                    if result.returncode != 0:
                        # no structured result was recorded - the raw failure is all we have.
                        self.logger.error(f'Failed to submit job: {result}')
                        raise exceptions.soca_exception(
                            errorcodes.JOB_SUBMISSION_FAILED,
                            f'Failed to submit job: {result}',
                        )
                    # rc == 0 but the tracker has no record for this job_uid (tracker TTL
                    # miss): avoid the opaque AttributeError and report clearly instead.
                    raise exceptions.soca_exception(
                        errorcodes.JOB_SUBMISSION_FAILED,
                        'Job was submitted but the submission result could not be retrieved. '
                        'Check job status with qstat before resubmitting.',
                    )

                if job_id is not None:
                    submission_result.job.job_id = job_id

                # a refused submission is a failure, not a success with accepted=False. use the
                # hook's own message text - the shell repr leaks the su command line and home path.
                if dry_run is None and result.returncode != 0:
                    self.logger.error(f'Failed to submit job: {result}')
                    reason = (
                        result.stderr
                        if Utils.is_not_empty(result.stderr)
                        else result.stdout
                    )
                    raise exceptions.soca_exception(
                        errorcodes.JOB_SUBMISSION_FAILED,
                        f'Failed to submit job: {reason}',
                    )

                return submission_result

            elif job_script_interpreter == 'bash':
                job_submit_command = [
                    'cd',
                    job_submission_dir,
                    '&&',
                    'bash',
                    job_submit_script,
                ]

                job_submit_command_str = ' '.join(job_submit_command)
                command = ['su', job_owner, '-c', f'{job_submit_command_str}']

                self.logger.info(' '.join(command))
                result = self.context.shell.invoke(command)

                if result.returncode == 0:
                    return SubmitJobResult(accepted=True)
                else:
                    raise exceptions.soca_exception(
                        errorcodes.JOB_SUBMISSION_FAILED,
                        f'Failed to submit job: {result}',
                    )
        finally:
            # qsub copies the script into the PBS spool, so the file has no reader after
            # submission; removing it on every outcome leaves nothing root-created behind.
            try:
                os.remove(job_submit_script)
            except OSError:
                pass

    def get_instance_type_options(self, context: ApiInvocationContext):
        """
        This API is used to get the instance type options during job submission.
        Based on ht support is enabled or disabled, threads_per_core is computed by InstanceTypesParamBuilder

        :param context:
        :return:
        """
        request = context.get_request_payload_as(GetInstanceTypeOptionsRequest)

        instance_types = request.instance_types
        queue_name = request.queue_name
        queue_profile_name = request.queue_profile_name
        enable_ht_support = request.enable_ht_support

        if Utils.are_empty(queue_name, queue_profile_name):
            raise exceptions.invalid_params(
                'One of [queue_name, queue_profile_name] is required.'
            )

        queue_profile = self.context.queue_profiles.get_queue_profile(
            queue_profile_name=queue_profile_name, queue_name=queue_name
        )

        if Utils.is_empty(instance_types):
            instance_types = queue_profile.default_job_params.instance_types
        if enable_ht_support is None:
            enable_ht_support = Utils.get_as_bool(
                queue_profile.default_job_params.enable_ht_support, False
            )

        param_builder_context = JobParamsBuilderContext(
            self.context, params={}, queue_profile=queue_profile
        )
        builder = InstanceTypesParamBuilder(
            param_builder_context, constants.JOB_PARAM_INSTANCE_TYPES
        )
        instance_type_options = builder.get_instance_type_options(
            instance_types, enable_ht_support
        )
        context.success(
            GetInstanceTypeOptionsResult(instance_types=instance_type_options)
        )

    def get_active_job(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetJobRequest)
        job_id = request.job_id
        if Utils.is_empty(job_id):
            raise exceptions.invalid_params('job_id is required')

        username = self.get_scoped_username(context)

        job = self.context.job_cache.get_job(job_id)
        if job is None:
            raise exceptions.soca_exception(
                error_code=errorcodes.JOB_NOT_FOUND,
                message=f'Job not found for job id: {job_id}',
            )
        # job ids are sequential, so an unscoped read by id is enumerable. same ownership
        # check as delete_job, with administrators and managers exempt.
        if job.owner != username and not context.is_authorized(elevated_access=True):
            raise exceptions.unauthorized_access()

        return context.success(GetJobResult(job=job))

    def _resolve_completed_job(
        self,
        job_id: Optional[str],
        job_uid: Optional[str],
        owner: Optional[str],
    ) -> Optional[SocaJob]:
        """
        read a completed job from the store the completed jobs listing reads.

        the document store keys jobs on job_uid and survives replacement of the
        scheduler host. the local job cache is keyed on job_id, which the scheduler
        reuses after a replacement, so it is only asked for a job_uid: a job_id lookup
        there can resolve to a different job than the listing holds, with its costs.
        """
        document_store_enabled = self.context.document_store.is_enabled()
        if document_store_enabled:
            job = self.context.document_store.get_job(
                job_uid=job_uid, job_id=job_id, owner=owner
            )
            if job is not None:
                return job
        if Utils.is_not_empty(job_uid):
            return self.context.job_cache.get_completed_job_by_uid(job_uid)
        if not document_store_enabled:
            return self.context.job_cache.get_completed_job(job_id)
        return None

    def get_completed_job(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(GetJobRequest)
        job_id = request.job_id
        job_uid = request.job_uid
        if Utils.are_empty(job_id, job_uid):
            raise exceptions.invalid_params('one of [job_id, job_uid] is required')

        username = self.get_scoped_username(context)
        elevated_access = context.is_authorized(elevated_access=True)

        # a job id is only unique for the life of the scheduler host that issued it, so
        # a lookup by job_id is scoped to the caller; job_uid needs no scoping.
        job = self._resolve_completed_job(
            job_id=job_id,
            job_uid=job_uid,
            owner=None if elevated_access else username,
        )
        if job is None:
            requested = (
                f'job uid: {job_uid}'
                if Utils.is_not_empty(job_uid)
                else f'job id: {job_id}'
            )
            raise exceptions.soca_exception(
                error_code=errorcodes.JOB_NOT_FOUND,
                message=f'Job not found for {requested}',
            )
        # scoped as in get_active_job. a completed job additionally carries
        # estimated_bom_cost and estimated_budget_usage, which is project spend.
        if job.owner != username and not elevated_access:
            raise exceptions.unauthorized_access()

        return context.success(GetJobResult(job=job))

    def delete_job(self, context: ApiInvocationContext):
        request = context.get_request_payload_as(DeleteJobRequest)
        if Utils.is_empty(request.job_id):
            raise exceptions.invalid_params('job_id is required')

        username = self.get_scoped_username(context)

        job = self.context.scheduler.get_job(job_id=request.job_id)
        if job is None:
            raise exceptions.soca_exception(
                errorcodes.JOB_NOT_FOUND, f'Job not found for Job Id: {request.job_id}'
            )
        if job.owner != username:
            raise exceptions.unauthorized_access()

        self.context.scheduler.delete_job(job.job_id)

        context.success(DeleteJobResult())

    def invoke(self, context: ApiInvocationContext):
        if not context.is_authorized_user():
            raise exceptions.unauthorized_access()

        namespace = context.namespace
        if namespace == 'Scheduler.ListActiveJobs':
            return self.list_active_jobs(context)
        elif namespace == 'Scheduler.ListCompletedJobs':
            return self.list_completed_jobs(context)
        elif namespace == 'Scheduler.GetUserApplications':
            return self.get_user_applications(context)
        elif namespace == 'Scheduler.SubmitJob':
            return self.submit_job(context)
        elif namespace == 'Scheduler.DeleteJob':
            return self.delete_job(context)
        elif namespace == 'Scheduler.GetActiveJob':
            return self.get_active_job(context)
        elif namespace == 'Scheduler.GetCompletedJob':
            return self.get_completed_job(context)
        elif namespace == 'Scheduler.GetInstanceTypeOptions':
            return self.get_instance_type_options(context)
