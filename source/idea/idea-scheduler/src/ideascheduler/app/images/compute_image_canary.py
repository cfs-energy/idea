"""Validate a candidate through qsub and the ordinary compute provisioner."""

import base64
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from ideadatamodel import (
    ImageCheck,
    ImagePipelineSettings,
    SocaJobParams,
    SocaQueueManagementParams,
    SocaQueueMode,
    SocaScalingMode,
    SubmitJobRequest,
    exceptions,
)
from ideasdk.aws.image_builds import default_builder_instance_type

# OpenPBS PBS_MAXQUEUENAME is 15: this prefix plus 12 digest characters.
VALIDATION_QUEUE_PREFIX = 'iv-'
PIPELINE_SETTINGS = 'virtual-desktop-controller.software_stacks.image_pipeline'


def canary_script(queue, output, mounts, token):
    """PBS stdout carries structured evidence, not a successful shell exit alone."""
    program = """import json, os, socket, tempfile, time
checks = []
for name, mount in MOUNTS:
    path = None
    try:
        if not os.path.ismount(mount):
            raise RuntimeError('The configured filesystem is not mounted.')
        with tempfile.NamedTemporaryFile(dir=mount, prefix='.idea-validate-', delete=False) as probe:
            path = probe.name
            probe.write(TOKEN.encode())
            probe.flush()
            os.fsync(probe.fileno())
        with open(path, 'rb') as probe:
            if probe.read() != TOKEN.encode():
                raise RuntimeError('The filesystem probe did not read back its contents.')
        os.unlink(path)
        path = None
        checks.append(dict(name='filesystem:' + name, ok=True, detail='Mounted filesystem passed write, fsync, read and delete.', seconds=0))
    except Exception as error:
        checks.append(dict(name='filesystem:' + name, ok=False, detail=str(error), seconds=0))
    finally:
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass  # The failed delete is already recorded by the probe.
print(json.dumps(dict(token=TOKEN, host=socket.gethostname(), checks=checks)), flush=True)
# Keep the running job observable to pbsnodes before normal single-job cleanup.
time.sleep(10)
raise SystemExit(0 if all(check['ok'] for check in checks) else 1)
""".replace('MOUNTS', repr(mounts)).replace('TOKEN', repr(token))
    return (
        f'#!/bin/bash\n#PBS -N {queue}\n#PBS -q {queue}\n'
        f'#PBS -o {output}\n#PBS -e {output}.err\n#PBS -l walltime=00:05:00\n'
        'set -eu\nCANARY_PYTHON=$(command -v python3 || command -v /usr/libexec/platform-python)\n"${CANARY_PYTHON}" - <<\'PY\'\n'
        + program
        + 'PY\n'
    )


class ComputeImageCanary:
    def __init__(self, context):
        self.context = context

    def validate(self, record, progress):
        from ideascheduler.app.api.scheduler_api import SchedulerAPI

        settings = ImagePipelineSettings(
            **dict(
                self.context.config().get_config(PIPELINE_SETTINGS, default={}) or {}
            )
        )
        user, project_name = settings.validation_user, settings.validation_project
        for value in (user, project_name):
            if not value or not re.fullmatch(r'[A-Za-z0-9_.-]+', value):
                raise exceptions.invalid_params(
                    'The validation user and project must be safe account names.'
                )
        project = self.context.projects_client.get_project_by_name(project_name)
        if not project or not project.project_id:
            raise exceptions.invalid_params('The validation project does not exist.')
        profiles = self.context.queue_profiles
        sources = [
            p
            for p in profiles.list_queue_profiles()
            if not (p.name or '').startswith(VALIDATION_QUEUE_PREFIX)
        ]
        if not sources:
            raise exceptions.invalid_params(
                'A compute queue profile is required for validation.'
            )
        token = uuid.uuid4().hex
        name = (
            VALIDATION_QUEUE_PREFIX
            + hashlib.sha256(record.image_id.encode()).hexdigest()[:12]
        )
        candidate = sources[0].model_copy(deep=True)
        candidate.queue_profile_id = None
        candidate.name = candidate.title = name
        candidate.queues = [name]
        candidate.projects = [project]
        candidate.keep_forever = False
        candidate.stack_uuid = None
        candidate.scaling_mode = SocaScalingMode.SINGLE_JOB
        candidate.queue_mode = SocaQueueMode.FIFO
        candidate.terminate_when_idle = 0
        candidate.image_pinned = True
        candidate.queue_management_params = SocaQueueManagementParams(
            max_running_jobs=1,
            max_provisioned_instances=1,
            max_nodes_per_job=1,
            restricted_parameters=['instance_ami', 'base_os', 'instance_types'],
        )
        params = candidate.default_job_params or SocaJobParams()
        params.base_os, params.instance_ami = record.base_os, record.image_id
        params.nodes = params.cpus = params.mpiprocs = 1
        params.gpus = 0
        params.memory = None
        params.instance_types = [
            default_builder_instance_type(record.architecture, 'c7i.large')
        ]
        params.spot = params.enable_efa_support = False
        params.compute_stack = params.stack_id = params.job_group = None
        candidate.default_job_params = params
        mounts = []
        storage = self.context.config().get_config('shared-storage', default={}) or {}
        # only what the canary node mounts: the bootstrap's scope rules for the scheduler
        # module, the validation project and the hidden queue (a list names who gets it)
        scoped = {
            'module': ('modules', 'scheduler'),
            'project': ('projects', project_name),
            'scheduler:queue-profile': ('queue_profiles', name),
        }
        for key, fs in storage.items():
            if not isinstance(fs, dict) or not fs.get('mount_dir'):
                continue
            scope = fs.get('scope') or []
            if scope and 'cluster' not in scope:
                if any(
                    s in scoped
                    and fs.get(scoped[s][0])
                    and scoped[s][1] not in fs[scoped[s][0]]
                    for s in scope
                ):
                    continue
            mounts.append((key, fs['mount_dir']))
        if not mounts:
            raise exceptions.invalid_params(
                'No configured compute shared filesystems were found.'
            )
        data = self.context.config().get_string(
            'shared-storage.data.mount_dir', required=True
        )
        output = Path(data) / 'home' / user / 'jobs' / (name + '.json')
        # PBS directive values cannot contain line breaks or whitespace.
        if any(ch.isspace() for ch in str(output)):
            raise exceptions.invalid_params(
                'The validation output path cannot contain whitespace.'
            )
        checks = list(record.checks or [])
        started = time.monotonic()
        deadline = started + settings.ready_gate_seconds_linux
        job_id = None
        created = None

        def check(check_name, ok, detail, fatal=True):
            checks.append(
                ImageCheck(
                    name=check_name,
                    ok=ok,
                    detail=detail,
                    seconds=int(time.monotonic() - started),
                )
            )
            progress({'checks': checks})
            if not ok and fatal:
                raise exceptions.general_exception(detail)

        try:
            # The deterministic queue name lets a restarted leader reap its prior canary.
            for stale in profiles.list_queue_profiles():
                if stale.name == name:
                    for job in self.context.scheduler.list_jobs(queue=name):
                        if self.context.scheduler.is_job_active(job.job_id):
                            self.context.scheduler.delete_job(job.job_id)
                    profiles.delete_queue_profile(
                        queue_profile_id=stale.queue_profile_id
                    )
            created = profiles.create_queue_profile(candidate)
            self.context.scheduler.set_queue_attributes(
                name,
                {
                    'acl_user_enable': True,
                    'acl_users': user,
                },
            )
            profiles.enable_queue_profile(queue_profile_id=created.queue_profile_id)
            submission = SchedulerAPI(self.context)._submit_job(
                request=SubmitJobRequest(
                    job_script=base64.b64encode(
                        canary_script(name, str(output), mounts, token).encode()
                    ).decode(),
                    job_script_interpreter='pbs',
                    project=project_name,
                ),
                job_owner=user,
                dry_run=None,
            )
            job_id = submission.job.job_id if submission.job else None
            check(
                'compute_submit',
                submission.accepted is True and bool(job_id),
                'The validation job was accepted by PBS.'
                if job_id
                else 'PBS did not accept the validation job.',
            )
            observed_node = None
            finished = None
            while time.monotonic() < deadline:
                job = self.context.scheduler.get_job(job_id)
                if job is None:
                    job = self.context.scheduler.get_finished_job(job_id)
                if job:
                    # qstat's SocaJob has no execution_hosts outside hook events.
                    # pbsnodes lists the jobs actually running on each registered MOM.
                    for node in self.context.scheduler.list_nodes():
                        if job_id in (node.jobs or []) and node.instance_id:
                            observed_node = node
                    if job.exit_status is not None:
                        finished = job
                        break
                time.sleep(2)
            in_time = finished is not None and time.monotonic() < deadline
            check(
                'compute_timeout',
                in_time,
                'The validation job finished within the ready gate.'
                if in_time
                else 'The validation job timed out before completion.',
            )
            check(
                'compute_identity',
                finished.owner == user
                and finished.project == project_name
                and finished.queue == name,
                'The validation job must run as the configured user in the validation project and queue.',
            )
            check(
                'pbs_registered',
                observed_node is not None,
                'The candidate node must register with PBS and run the validation job.',
            )
            instances = (
                self.context.aws()
                .ec2()
                .describe_instances(InstanceIds=[observed_node.instance_id])
            )
            launched = [
                i
                for r in instances.get('Reservations', [])
                for i in r.get('Instances', [])
            ]
            check(
                'compute_ami',
                len(launched) == 1 and launched[0].get('ImageId') == record.image_id,
                'The validation node must launch on the exact candidate AMI.',
            )
            evidence = {}
            while time.monotonic() < deadline:
                try:
                    evidence = json.loads(output.read_text())
                    break
                except (OSError, ValueError):
                    time.sleep(2)
            node_names = {observed_node.host, launched[0].get('PrivateDnsName', '')}
            node_names |= {
                name.split('.')[0]
                for name in node_names
                if name and not name[0].isdigit()
            }
            check(
                'compute_output',
                evidence.get('token') == token and evidence.get('host') in node_names,
                'The validation job must produce its known output on the registered node.',
            )
            results = {item.get('name'): item for item in evidence.get('checks', [])}
            for fs, _ in mounts:
                result = results.get('filesystem:' + fs, {})
                check(
                    'filesystem:' + fs,
                    result.get('ok') is True,
                    result.get('detail')
                    or f'The {fs} filesystem probe did not report a result.',
                    fatal=False,
                )
            check(
                'compute_job',
                finished.exit_status == 0,
                f'The validation job exited with status {finished.exit_status}.',
                fatal=False,
            )
            if any(c.ok is False for c in checks):
                raise exceptions.general_exception(
                    next(c.detail for c in checks if c.ok is False)
                )
        except Exception as error:
            if not any(c.ok is False for c in checks):
                checks.append(
                    ImageCheck(
                        name='compute_canary',
                        ok=False,
                        detail=getattr(error, 'message', None) or str(error),
                        seconds=int(time.monotonic() - started),
                    )
                )
                progress({'checks': checks})
            raise
        finally:
            cleanup_errors = []
            try:
                if job_id and self.context.scheduler.is_job_active(job_id):
                    self.context.scheduler.delete_job(job_id)
            except Exception as error:
                cleanup_errors.append(str(error))
            try:
                if created:
                    profiles.delete_queue_profile(
                        queue_profile_id=created.queue_profile_id
                    )
            except Exception as error:
                cleanup_errors.append(str(error))
            for path in (output, Path(str(output) + '.err')):
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    cleanup_errors.append(str(error))
            if cleanup_errors:
                check(
                    'compute_cleanup',
                    False,
                    'The validation resources could not be cleaned up: '
                    + '; '.join(cleanup_errors),
                )
        return checks
