"""
Test launch for the desktop image pipeline: a desktop from the candidate image, created
through the same validation and CreateSession path an admin uses, owned by the
validation user in the validation project, checked over SSM, rebooted once, deleted.
Every timeout is a failure. Also reads the builder's in-bake check results.

Nothing here decides promotion; the pipeline (software_stacks/image_pipeline.py) does.
"""

import json
import shlex
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from botocore.exceptions import ClientError

from ideadatamodel import (
    ImageBuildRecord,
    ImageCheck,
    ImagePipelineSettings,
    ImageVariant,
    Project,
    SocaBaseModel,
    VirtualDesktopServer,
    VirtualDesktopGPU,
    VirtualDesktopSession,
    VirtualDesktopSessionState,
    VirtualDesktopSoftwareStack,
)
from ideasdk.aws.validation_identity import (
    ensure_validation_identity,
    invoke_cluster_manager,
)
from ideasdk.utils import Utils
from ideavirtualdesktopcontroller.app.sessions import constants as sessions_constants
from ideavirtualdesktopcontroller.app.virtual_desktop_controller_utils import (
    BOOTSTRAP_LOG_STREAM,
    BOOTSTRAP_STATUS_TAG,
    dcv_host_cloudwatch_logs_enabled,
    dcv_host_log_group,
)

VALIDATION_STACK_PREFIX = 'ss-validate-'
VALIDATION_NAME_PREFIX = 'validate-'

LINUX_CHECKS_FILE = '/var/lib/idea/image-checks.json'
WINDOWS_CHECKS_FILE = 'C:\\ProgramData\\IDEA\\image-checks.json'

# the host bootstrap's last line in its CloudWatch stream
BOOTSTRAP_COMPLETE_SENTINEL = 'IDEA_BOOTSTRAP_COMPLETE'
BOOTSTRAP_LOG_WAIT_SECONDS = 120
SSM_TIMEOUT_SECONDS = 180
# a host that just reached READY can still be rebooting (Windows applies its rename/join
# with a restart): wait this long for it to run and answer SSM before a check fails
HOST_ONLINE_SECONDS = 180
POLL_SECONDS = 15
GATEWAY_TIMEOUT_SECONDS = 15

# a size every region and stack allows; GPU variants get the smallest GPU of the vendor
VALIDATION_INSTANCE_TYPES = {
    ('x86_64', ImageVariant.CPU.value): 'm6i.xlarge',
    ('arm64', ImageVariant.CPU.value): 'm7g.xlarge',
    ('x86_64', ImageVariant.NVIDIA.value): 'g4dn.xlarge',
    ('arm64', ImageVariant.NVIDIA.value): 'g5g.xlarge',
    ('x86_64', ImageVariant.AMD.value): 'g4ad.xlarge',
}

VALIDATION_GPU = {
    ImageVariant.NVIDIA.value: VirtualDesktopGPU.NVIDIA,
    ImageVariant.AMD.value: VirtualDesktopGPU.AMD,
}

# EC2 refusals that mean "not now", not "broken": the row waits and tries again
CAPACITY_MARKERS = (
    'InsufficientInstanceCapacity',
    'InsufficientCapacity',
    'VcpuLimitExceeded',
    'InstanceLimitExceeded',
    'MaxSpotInstanceCountExceeded',
    'RequestLimitExceeded',
    'Throttling',
)


class CapacityWait(Exception):
    """EC2 had no capacity or quota for a launch; the row waits and retries"""


def is_capacity_problem(text: Optional[str]) -> bool:
    return any(marker in (text or '') for marker in CAPACITY_MARKERS)


def log_line(message: str) -> str:
    """
    a CloudWatch event as text: Windows PowerShell 5.1 wrote its logs as UTF-16, which the
    agent ships byte for byte (NUL between characters, a BOM at the start)
    """
    return message.replace('\x00', '').replace('\ufeff', '').strip()


def check(name: str, ok: bool, detail: str, started: float) -> ImageCheck:
    return ImageCheck(
        name=name,
        ok=bool(ok),
        detail=(detail or '')[:500],
        seconds=int(max(0, time.time() - started)),
    )


def parse_check_lines(stdout: str, started: float) -> List[ImageCheck]:
    """CHECK|<name>|ok|<detail> lines from a host script; anything else is ignored"""
    checks = []
    for line in (stdout or '').splitlines():
        parts = line.strip().split('|', 3)
        if len(parts) == 4 and parts[0] == 'CHECK':
            checks.append(check(parts[1], parts[2] == 'ok', parts[3], started))
    return checks


def run_ssm(
    context,
    instance_id: str,
    windows: bool,
    script: str,
    timeout: int = SSM_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> Tuple[str, str, str]:
    """
    (status, stdout, stderr) of one script; a command that outlives timeout is 'TimedOut',
    and a host that is not running and online over SSM within HOST_ONLINE_SECONDS is 'NotOnline'
    """
    ssm = context.aws().ssm()
    online_by = clock() + HOST_ONLINE_SECONDS
    while True:
        reason = _not_online(context, ssm, instance_id)
        if reason is None:
            try:
                command = ssm.send_command(
                    InstanceIds=[instance_id],
                    DocumentName='AWS-RunPowerShellScript'
                    if windows
                    else 'AWS-RunShellScript',
                    Parameters={
                        'commands': [script],
                        'executionTimeout': [str(timeout)],
                    },
                    TimeoutSeconds=max(30, timeout),
                )
                break
            except ClientError as e:
                # a stale Online ping on a host that is rebooting: try again
                if e.response.get('Error', {}).get('Code') != 'InvalidInstanceId':
                    raise
                reason = str(e)
        if clock() >= online_by:
            return (
                'NotOnline',
                '',
                f'the host did not come back over SSM within {HOST_ONLINE_SECONDS} s: {reason}',
            )
        sleep(10)
    command_id = command['Command']['CommandId']
    deadline = clock() + timeout + 30
    while clock() < deadline:
        sleep(3)
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == 'InvocationDoesNotExist':
                continue
            raise
        status = result.get('Status')
        if status not in ('Pending', 'InProgress', 'Delayed'):
            return (
                status,
                result.get('StandardOutputContent', ''),
                result.get('StandardErrorContent', ''),
            )
    return 'TimedOut', '', f'no result within {timeout} seconds'


def _not_online(context, ssm, instance_id: str) -> Optional[str]:
    """why SSM cannot reach the host now, or None when it is running and Online"""
    reservations = (
        context.aws()
        .ec2()
        .describe_instances(InstanceIds=[instance_id])
        .get('Reservations', [])
    )
    states = [
        (i.get('State') or {}).get('Name') for r in reservations for i in r['Instances']
    ]
    if states and states[0] != 'running':
        return f'instance {states[0]}'
    info = ssm.describe_instance_information(
        Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
    ).get('InstanceInformationList', [])
    ping = info[0].get('PingStatus') if info else 'not registered'
    return None if ping == 'Online' else f'SSM agent {ping}'


def read_in_bake_checks(
    context, instance_id: str, windows: bool
) -> Tuple[Optional[str], List[ImageCheck]]:
    """(release, checks) from the builder's image-checks.json; raises when it cannot be read"""
    script = (
        f"Get-Content -Raw '{WINDOWS_CHECKS_FILE}'"
        if windows
        else f'cat {LINUX_CHECKS_FILE}'
    )
    status, stdout, stderr = run_ssm(context, instance_id, windows, script, 120)
    if status != 'Success':
        raise RuntimeError(
            f'could not read the in-bake check results over SSM ({status}): {stderr.strip()[:200]}'
        )
    data = json.loads(stdout)
    checks = [
        ImageCheck(
            name=c.get('name'),
            ok=bool(c.get('ok')),
            detail=(c.get('detail') or '')[:500],
            seconds=int(c.get('seconds') or 0),
        )
        for c in data.get('checks') or []
    ]
    return data.get('release'), checks


def shared_filesystems(config, windows: bool) -> List[Tuple[str, str]]:
    """
    (name, path) for every shared filesystem a validation desktop mounts: cluster scope,
    or module scope that admits this module. project-scoped storage belongs to other
    projects, which the validation project is not.
    """
    found = []
    tree = config.get_config('shared-storage', default={}) or {}
    # a pyhocon tree's get(key) raises for a missing key; plain dicts take defaults
    tree = (
        tree.as_plain_ordered_dict() if hasattr(tree, 'as_plain_ordered_dict') else tree
    )
    for name, storage in tree.items():
        if not isinstance(storage, dict) or 'provider' not in storage:
            continue
        scope = storage.get('scope') or []
        modules = storage.get('modules') or []
        if scope and 'cluster' not in scope:
            if 'project' in scope or 'module' not in scope:
                continue
            if modules and 'virtual-desktop-controller' not in modules:
                continue
        if windows:
            volume = (storage.get('fsx_netapp_ontap') or {}).get('volume') or {}
            if storage['provider'] == 'fsx_netapp_ontap' and volume.get(
                'cifs_share_name'
            ):
                dns = storage['fsx_netapp_ontap']['svm']['smb_dns']
                found.append((name, f'\\\\{dns}\\{volume["cifs_share_name"]}'))
            elif storage['provider'] == 'fsx_windows_file_server':
                dns = storage['fsx_windows_file_server']['dns']
                found.append((name, f'\\\\{dns}\\share'))
        elif storage.get('mount_dir'):
            found.append((name, storage['mount_dir']))
    return found


def linux_host_script(
    dcv_session_id: str, user: str, mounts: List[Tuple[str, str]], variant: str
) -> str:
    q = shlex.quote
    lines = [
        'check() { echo "CHECK|$1|$2|$3"; }',
        f'SID={q(dcv_session_id)}; U={q(user)}',
        'OUT=$(dcv list-sessions 2>&1 | tr "\\n" " ")',
        'if echo "$OUT" | grep -q -- "$SID"; then check dcv_session ok "dcv list-sessions shows $SID";'
        ' else check dcv_session fail "dcv list-sessions does not show $SID: ${OUT:0:300}"; fi',
        'if getent passwd "$U" >/dev/null && id "$U" >/dev/null 2>&1;'
        ' then check directory_user ok "$U resolves through the directory";'
        ' else check directory_user fail "$U does not resolve (getent passwd / id)"; fi',
        'probe() { f="$1/.idea-image-probe-$(hostname)-$$";'
        ' dd if=/dev/zero of="$f" bs=4k count=1 conv=fsync status=none && cat "$f" >/dev/null && rm -f "$f"; }',
    ]
    for name, path in mounts:
        n = q(f'filesystem:{name}')
        d = q(path)
        lines.append(
            f'if ! mountpoint -q {d}; then check {n} fail "{path} is not mounted";'
            f' elif probe {d} 2>/dev/null || runuser -u "$U" -- bash -c "$(declare -f probe); probe {d}" 2>/dev/null;'
            f' then check {n} ok "{path} mounted; write, fsync, read and delete worked";'
            f' else check {n} fail "{path} is mounted but a write/fsync/read/delete probe failed"; fi'
        )
    if variant == ImageVariant.NVIDIA.value:
        lines.append(
            'if nvidia-smi -L 2>&1 | grep -q GPU; then check gpu_runtime ok "nvidia-smi lists a GPU";'
            ' else check gpu_runtime fail "nvidia-smi did not list a GPU"; fi'
        )
    elif variant == ImageVariant.AMD.value:
        lines.append(
            'if [ -e /dev/kfd ] || lsmod | grep -q amdgpu; then check gpu_runtime ok "amdgpu driver loaded";'
            ' else check gpu_runtime fail "amdgpu driver not loaded"; fi'
        )
    return '\n'.join(lines)


def windows_host_script(
    dcv_session_id: str, mounts: List[Tuple[str, str]], variant: str
) -> str:
    def ps(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    lines = [
        'function Check($n, $ok, $d) { $s = if ($ok) { "ok" } else { "fail" }; Write-Output "CHECK|$n|$s|$d" }',
        f'$sid = {ps(dcv_session_id)}',
        "$out = (& 'C:\\Program Files\\NICE\\DCV\\Server\\bin\\dcv.exe' list-sessions 2>&1 | Out-String)",
        'Check "dcv_session" ($out -match [regex]::Escape($sid)) "dcv list-sessions: $($out.Trim())"',
        'try { $sc = Test-ComputerSecureChannel; Check "directory_user" $sc "domain secure channel: $sc" }'
        ' catch { Check "directory_user" $false "Test-ComputerSecureChannel failed: $($_.Exception.Message)" }',
    ]
    for name, path in mounts:
        if '%' in path:
            # SYSTEM cannot expand per-user paths or prove user access. Only probe a
            # literal UNC parent that includes both a server and a share.
            parts = path.split('\\')
            variable_index = next(i for i, part in enumerate(parts) if '%' in part)
            check = ps('filesystem:' + name)
            if variable_index < 4:
                lines.append(
                    f'Check {check} $true {ps(f"per-user share {path}; not probed (server or share is variable)")}'
                )
                continue
            parent = '\\'.join(parts[:variable_index])
            reachable = ps(f'{parent} reachable (per-user share {path})')
            denied = ps(
                f'{parent} reachable; access denied to SYSTEM (per-user share {path})'
            )
            missing = ps(f'{parent} not reachable (per-user share {path})')
            lines.append(
                f'try {{ Get-Item -LiteralPath {ps(parent)} -ErrorAction Stop | Out-Null; Check {check} $true {reachable} }}'
                f' catch [System.UnauthorizedAccessException] {{ Check {check} $true {denied} }}'
                ' catch [System.Management.Automation.ItemNotFoundException], [System.IO.IOException] {'
                ' $e = $_.Exception; $accessDenied = $_.CategoryInfo.Category -eq "PermissionDenied";'
                ' while ($null -ne $e) {'
                ' if ($e -is [System.UnauthorizedAccessException] -or ($e.HResult -band 0xFFFF) -eq 5) { $accessDenied = $true };'
                ' $e = $e.InnerException };'
                f' if ($accessDenied) {{ Check {check} $true {denied} }}'
                f' else {{ Check {check} $false ({missing} + ": " + $_.Exception.Message) }} }}'
                f' catch {{ Check {check} $false ({missing} + ": " + $_.Exception.Message) }}'
            )
            continue
        lines.append(
            f'try {{ $p = Join-Path {ps(path)} (".idea-image-probe-" + $env:COMPUTERNAME);'
            ' Set-Content -Path $p -Value "probe"; $fs = [IO.File]::Open($p, "Open"); $fs.Flush($true); $fs.Close();'
            ' Get-Content $p | Out-Null; Remove-Item $p;'
            f' Check {ps("filesystem:" + name)} $true "{path} write, flush, read and delete worked" }}'
            f' catch {{ Check {ps("filesystem:" + name)} $false "{path}: $($_.Exception.Message)" }}'
        )
    if variant == ImageVariant.NVIDIA.value:
        lines.append(
            '$g = (& "$env:SystemRoot\\System32\\nvidia-smi.exe" -L 2>&1 | Out-String);'
            ' Check "gpu_runtime" ($g -match "GPU") "nvidia-smi: $($g.Trim())"'
        )
    elif variant == ImageVariant.AMD.value:
        lines.append(
            '$g = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match "AMD|Radeon" };'
            ' Check "gpu_runtime" ($null -ne $g) "AMD display adapter present: $($null -ne $g)"'
        )
    return '\r\n'.join(lines)


class ImageTestLauncher:
    """
    api: a VirtualDesktopAdminAPI (validation, request completion, session utils and
    dbs). clock/sleep are injectable so the gates are testable without waiting.
    """

    def __init__(
        self,
        context,
        api,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.context = context
        self.api = api
        self.clock = clock
        self.sleep = sleep
        self._logger = context.logger('image-validation')

    # identity

    def ensure_identity(self, settings: ImagePipelineSettings) -> Project:
        """the validation user and its hidden project, shared with the compute canary"""
        return ensure_validation_identity(
            self.context, settings, invoke=self._invoke_cluster_manager
        )

    def _invoke_cluster_manager(self, namespace: str, payload: SocaBaseModel):
        invoke_cluster_manager(self.context, namespace, payload)

    # test launch

    def test_launch(
        self,
        record: ImageBuildRecord,
        base_stack: VirtualDesktopSoftwareStack,
        settings: ImagePipelineSettings,
    ) -> List[ImageCheck]:
        """
        every test-launch check for the candidate. raises CapacityWait when EC2 refused
        the launch for capacity; any other launch failure comes back as a failed check.
        the session and the candidate stack are always deleted.
        """
        windows = 'windows' in (record.base_os or '')
        variant = getattr(record.variant, 'value', record.variant) or 'cpu'
        gate = (
            settings.ready_gate_seconds_windows
            if windows
            else settings.ready_gate_seconds_linux
        )
        checks: List[ImageCheck] = []
        project = self.ensure_identity(settings)
        stack = self._candidate_stack(record, base_stack, project, variant)
        session = None
        try:
            requested = self.clock()
            session, launch_check = self._launch(
                record, stack, project, settings, variant
            )
            checks.append(launch_check)
            if not launch_check.ok:
                return checks
            # the gate runs from the request, as a user's wait does, not from launch return
            ready = self._wait_ready(session, gate, 'ready_gate', started=requested)
            checks.append(ready)
            if not ready.ok:
                self._explain(ready, session)
                return checks
            session = self._get(session)
            checks.extend(self._host_checks(session, settings, windows, variant))
            checks.append(self._bootstrap_log(session))
            rebooted = self._reboot(session, gate)
            if not rebooted.ok:
                self._explain(rebooted, session)
            checks.append(rebooted)
            return checks
        finally:
            self._delete(session, stack)

    def _candidate_stack(self, record, base_stack, project, variant):
        stack = base_stack.model_copy(deep=True)
        suffix = '' if variant == 'cpu' else f'-{variant}'
        stack.stack_id = (
            f'{VALIDATION_STACK_PREFIX}{record.base_os}-'
            f'{record.architecture.replace("_", "-")}{suffix}'
        )
        stack.name = f'Image validation {record.base_os} {record.architecture}{suffix}'
        stack.description = 'hidden: candidate image under validation'
        stack.ami_id = record.image_id
        stack.projects = [project]
        stack.enabled = True
        stack.pool_enabled = False
        stack.pool_asg_name = None
        stack.image_pinned = True
        stack.allowed_instance_types = None
        existing = self.api.software_stack_db.get(stack.stack_id, record.base_os)
        if existing is not None:
            self.api.software_stack_db.delete(existing)
        return self.api.software_stack_db.create(stack)

    def _launch(self, record, stack, project, settings, variant):
        started = self.clock()
        suffix = '' if variant == 'cpu' else f'-{variant}'
        session = VirtualDesktopSession(
            name=f'{VALIDATION_NAME_PREFIX}{record.base_os}-{record.architecture}{suffix}',
            owner=settings.validation_user,
            project=project,
            software_stack=VirtualDesktopSoftwareStack(
                stack_id=stack.stack_id, base_os=stack.base_os
            ),
            hibernation_enabled=False,
            description='image pipeline test launch',
            server=VirtualDesktopServer(
                instance_type=self._instance_type(record, stack, settings, variant),
                root_volume_size=stack.min_storage,
            ),
        )
        session, valid = self.api.validate_create_session_request(session)
        if valid:
            session = self.api.complete_create_session_request(session, None)
            session.is_launched_by_admin = True
            session = self.api.session_utils.create_session(session)
        reason = session.failure_reason
        if Utils.is_not_empty(reason):
            if is_capacity_problem(reason):
                raise CapacityWait(reason)
            return session, check('test_launch', False, reason, started)
        return session, check(
            'test_launch',
            True,
            f'CreateSession accepted {session.idea_session_id}',
            started,
        )

    def _instance_type(self, record, stack, settings, variant) -> str:
        """
        the preferred validation size when the cluster offers it to this stack, else the
        smallest offered size with 4 vCPUs and 16 GiB (or the smallest offered at all),
        general purpose first: the test launch goes through the same size filter a user's
        request does. A CPU row never takes a GPU size, and the size must boot the
        candidate's boot mode (a UEFI-only image refused g4ad, which is legacy BIOS only).
        """
        preferred = VALIDATION_INSTANCE_TYPES.get(
            (record.architecture, variant), 'm6i.xlarge'
        )
        utils = getattr(self.api, 'controller_utils', None)
        if utils is None:
            return preferred
        offered = utils.get_valid_instance_types(
            hibernation_support=False,
            software_stack=stack,
            gpu=VALIDATION_GPU.get(variant, VirtualDesktopGPU.NO_GPU),
            username=settings.validation_user,
        )
        boot_mode = (utils.describe_image_id(record.image_id) or {}).get('BootMode')
        if boot_mode in ('uefi', 'legacy-bios'):
            offered = [
                i
                for i in offered
                # an instance type that does not list its boot modes is left to EC2
                if boot_mode in (i.get('SupportedBootModes') or [boot_mode])
            ]
        if not offered or preferred in {i.get('InstanceType') for i in offered}:
            return preferred  # nothing offered: CreateSession reports why

        def size(info):
            name = info.get('InstanceType') or ''
            vcpus = (info.get('VCpuInfo') or {}).get('DefaultVCpus') or 0
            mib = (info.get('MemoryInfo') or {}).get('SizeInMiB') or 0
            return (
                vcpus < 4 or mib < 16384,
                not name.startswith('m'),
                vcpus,
                mib,
                name,
            )

        return min(offered, key=size)['InstanceType']

    def _get(self, session) -> Optional[VirtualDesktopSession]:
        return self.api.session_db.get_from_db(
            idea_session_owner=session.owner, idea_session_id=session.idea_session_id
        )

    def _wait_ready(
        self, session, gate: int, name: str, started: Optional[float] = None
    ) -> ImageCheck:
        """READY within gate seconds of the request, else a failed check with the last state"""
        started = self.clock() if started is None else started
        state = None
        while self.clock() - started <= gate:
            current = self._get(session)
            state = getattr(current, 'state', None)
            if state == VirtualDesktopSessionState.READY:
                return check(
                    name, True, f'READY in {int(self.clock() - started)} s', started
                )
            if state == VirtualDesktopSessionState.ERROR:
                return check(
                    name,
                    False,
                    f'the desktop went to ERROR: {getattr(current, "failure_reason", None) or "no reason recorded"}',
                    started,
                )
            self.sleep(POLL_SECONDS)
        return check(
            name,
            False,
            f'not READY within {gate} s (last state {getattr(state, "value", state)})',
            started,
        )

    def _explain(self, failed: ImageCheck, session):
        """a desktop that never got READY: name its host and the last line its bootstrap logged"""
        try:
            current = self._get(session) or session
            instance_id = getattr(current.server, 'instance_id', None)
            if not instance_id:
                return
            failed.detail = f'{failed.detail}; host {instance_id}'
            if not dcv_host_cloudwatch_logs_enabled(self.context):
                return
            events = (
                self.context.aws()
                .logs()
                .get_log_events(
                    logGroupName=dcv_host_log_group(self.context),
                    logStreamName=BOOTSTRAP_LOG_STREAM.format(instance_id=instance_id),
                    startFromHead=False,
                    limit=20,
                )
                .get('events', [])
            )
            lines = [log_line(e.get('message', '')) for e in events]
            lines = [line for line in lines if line.strip('*')]
            last = lines[-1] if lines else 'no bootstrap log yet'
            failed.detail = f'{failed.detail}; last bootstrap log line: {last}'[:500]
        except Exception as e:
            self._logger.warning(f'could not read the bootstrap log tail: {e}')

    def _host_checks(self, session, settings, windows, variant) -> List[ImageCheck]:
        started = self.clock()
        checks = [self._bootstrap_tag(session), self._connection(session)]
        mounts = shared_filesystems(self.context.config(), windows)
        script = (
            windows_host_script(session.dcv_session_id, mounts, variant)
            if windows
            else linux_host_script(
                session.dcv_session_id, settings.validation_user, mounts, variant
            )
        )
        try:
            status, stdout, stderr = run_ssm(
                self.context,
                session.server.instance_id,
                windows,
                script,
                sleep=self.sleep,
                clock=self.clock,
            )
        except Exception as e:
            status, stdout, stderr = 'Failed', '', str(e)
        found = parse_check_lines(stdout, started)
        expected = ['dcv_session', 'directory_user'] + [
            f'filesystem:{n}' for n, _ in mounts
        ]
        if variant != 'cpu':
            expected.append('gpu_runtime')
        names = {c.name for c in found}
        for name in expected:
            if name not in names:
                found.append(
                    check(
                        name,
                        False,
                        f'no result from the host over SSM ({status}): {stderr.strip()[:200]}',
                        started,
                    )
                )
        return checks + found

    def _bootstrap_tag(self, session) -> ImageCheck:
        started = self.clock()
        result = (
            self.context.aws()
            .ec2()
            .describe_instances(InstanceIds=[session.server.instance_id])
        )
        tags = {
            t['Key']: t['Value']
            for r in result.get('Reservations', [])
            for i in r.get('Instances', [])
            for t in i.get('Tags', [])
        }
        status = tags.get(BOOTSTRAP_STATUS_TAG)
        if status:
            return check(
                'bootstrap_status',
                False,
                f'the host tagged {BOOTSTRAP_STATUS_TAG}={status}',
                started,
            )
        return check('bootstrap_status', True, 'no bootstrap failure recorded', started)

    def _connection(self, session) -> ImageCheck:
        """connection info from the broker, and the gateway answering the URL a user gets"""
        started = self.clock()
        info = self.context.dcv_broker_client.get_session_connection_data(
            dcv_session_id=session.dcv_session_id, username=session.owner
        )
        if Utils.is_not_empty(info.failure_reason) or Utils.is_empty(info.access_token):
            return check(
                'connection_info',
                False,
                f'no connection info: {info.failure_reason or "empty access token"}',
                started,
            )
        config = self.context.config()
        if config.get_bool(
            'virtual-desktop-controller.dcv_connection_gateway.certificate.provided',
            default=False,
        ):
            host = config.get_string(
                'virtual-desktop-controller.dcv_connection_gateway.certificate.custom_dns_name'
            )
        else:
            host = config.get_string(
                'virtual-desktop-controller.external_nlb.load_balancer_dns_name'
            )
        url = f'https://{host}/?authToken={info.access_token}'
        try:
            insecure = ssl.create_default_context()
            insecure.check_hostname = False
            insecure.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(
                url, timeout=GATEWAY_TIMEOUT_SECONDS, context=insecure
            ) as response:
                code = response.status
        except Exception as e:
            return check(
                'connection_info',
                False,
                f'the gateway at {host} did not answer: {e}',
                started,
            )
        return check(
            'connection_info',
            code == 200,
            f'gateway {host} answered HTTP {code}',
            started,
        )

    def _bootstrap_log(self, session) -> ImageCheck:
        started = self.clock()
        if not dcv_host_cloudwatch_logs_enabled(self.context):
            return check(
                'bootstrap_log',
                False,
                'desktop CloudWatch logs are disabled, so bootstrap completion cannot be verified',
                started,
            )
        group = dcv_host_log_group(self.context)
        stream = BOOTSTRAP_LOG_STREAM.format(instance_id=session.server.instance_id)
        logs = self.context.aws().logs()
        while self.clock() - started <= BOOTSTRAP_LOG_WAIT_SECONDS:
            try:
                events = logs.filter_log_events(
                    logGroupName=group,
                    logStreamNames=[stream],
                    filterPattern=f'"{BOOTSTRAP_COMPLETE_SENTINEL}"',
                    limit=1,
                ).get('events', [])
                if events:
                    return check(
                        'bootstrap_log',
                        True,
                        f'{group} {stream} has the completion marker',
                        started,
                    )
            except ClientError as e:
                if (
                    e.response.get('Error', {}).get('Code')
                    != 'ResourceNotFoundException'
                ):
                    raise
            self.sleep(POLL_SECONDS)
        return check(
            'bootstrap_log',
            False,
            f'no {BOOTSTRAP_COMPLETE_SENTINEL} in {group} {stream} within {BOOTSTRAP_LOG_WAIT_SECONDS} s',
            started,
        )

    def _reboot(self, session, gate: int) -> ImageCheck:
        started = self.clock()
        session.force = True
        _, failed = self.api.session_utils.reboot_sessions([session])
        if failed:
            return check(
                'reboot_ready',
                False,
                f'reboot refused: {failed[0].failure_reason}',
                started,
            )
        return self._wait_ready(session, gate, 'reboot_ready')

    def _delete(self, session, stack):
        try:
            if session is not None and Utils.is_not_empty(session.idea_session_id):
                current = self._get(session)
                if current is not None:
                    current.force = True
                    self.api.session_utils.terminate_sessions([current])
        except Exception as e:
            self._logger.error(
                f'could not delete validation desktop {session.idea_session_id}: {e}'
            )
        try:
            self.api.software_stack_db.delete(stack)
        except Exception as e:
            self._logger.error(
                f'could not delete candidate stack {stack.stack_id}: {e}'
            )

    # cleanup

    def reap(self, settings: ImagePipelineSettings, older_than: timedelta) -> List[str]:
        """delete validation desktops older than older_than (a test launch never takes that long)"""
        table = self.api.session_db._table
        from boto3.dynamodb.conditions import Key

        items = table.query(
            KeyConditionExpression=Key(sessions_constants.USER_SESSION_DB_HASH_KEY).eq(
                settings.validation_user
            )
        ).get('Items', [])
        cutoff = datetime.now(tz=timezone.utc) - older_than
        reaped = []
        for item in items:
            session = self.api.session_db.convert_db_dict_to_session_object(item)
            created = session.created_on
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created is None or created > cutoff:
                continue
            session.force = True
            self.api.session_utils.terminate_sessions([session])
            reaped.append(session.idea_session_id)
        if reaped:
            self._logger.warning(f'deleted validation desktops left behind: {reaped}')
        return reaped
