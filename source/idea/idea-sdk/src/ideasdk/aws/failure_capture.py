"""
What a host that failed was saying, read before it is disposed of.

EC2 keeps the console of an instance whose bootstrap never got far enough to ship a log, so a
kernel panic on the first reboot is only ever there. On a failed desktop or bake the console and
the tail of the bootstrap log go to the module log, which reaches Datadog tagged idea_cluster:
on GovCloud that puts them in the restricted log dataset, and every cluster's are masked by the
scanner group on idea_cluster. A known signature becomes a metric tag and one plain sentence for
the portal. The sentence comes from the table below, never from the text, so no path or package
name reaches anyone's screen.

Who a host belonged to is no part of why it failed. What is logged is the structured first line
(fixed fields, the reason from the table) and tail lines after scrub(): shell trace lines dropped,
secrets redacted, usernames and every path outside the system's own directories replaced.

Nothing here may delay or stop the disposal that follows: the read runs on its own thread against
a hard limit, and any failure just means no cause was found.
"""

from ideadatamodel import constants
from ideadatamodel.model_utils import ModelUtils
from ideasdk.metrics.health import HealthMetrics

from botocore.exceptions import ClientError
from dataclasses import dataclass
from threading import Event, Thread
from typing import Dict, List, Optional, Tuple
import re

ENABLED_KEY = 'cluster.failure_capture.enabled'
CAPTURE_MARKER = 'IDEA_FAILURE_CAPTURE'
# the sweep that calls this has a queue message's 30 seconds; a healthy read takes under one
TIME_LIMIT_SECONDS = 5.0
CONSOLE_TAIL_CHARS = 12000
LOG_TAIL_CHARS = 4000
LOG_TAIL_EVENTS = 200
LINE_CHARS = 400
# a host wrote this text, and a pattern costs the square of an unbroken run. the regex engine
# holds the interpreter while it works, so no thread's time limit stops it: the text is cut to
# these before any pattern sees it. READ_CHARS is what EC2 keeps of a console.
READ_CHARS = 64 * 1024
CUT_CHARS = LINE_CHARS * 2
PEM_BEGIN, PEM_END = '-----BEGIN ', '-----END '
ACCESS_DENIED_CODES = ('UnauthorizedOperation', 'AccessDenied', 'AccessDeniedException')

# most specific first: a missing initramfs also panics, and a panic outranks a failed lookup
# earlier in the same boot. normal boots print dracut's stages and panic= on the command line,
# so these match only what is said when it has gone wrong.
SIGNATURES: Tuple[Tuple[str, str, 're.Pattern'], ...] = (
    (
        'initramfs_missing',
        'the operating system could not find its boot files (initramfs)',
        re.compile(
            r'VFS: (Unable to mount root fs|Cannot open root device)'
            r'|initramfs (unpacking failed|not found)'
            r'|(no|missing|cannot find|could not find) initramfs'
            r'|failed to load initrd'
            r'|you need to load the kernel first',
            re.I,
        ),
    ),
    (
        'dracut',
        'the early boot stage (dracut) could not finish',
        re.compile(
            r'dracut-initqueue.*(timeout|timed out)'
            r'|dracut:/#'
            r'|dracut (warning|fatal)'
            r'|dracut: (fatal|refusing)'
            r'|Warning: /dev/\S+ does not exist',
            re.I,
        ),
    ),
    (
        'kernel_panic',
        'the operating system crashed while starting (kernel panic)',
        re.compile(r'Kernel panic - not syncing|kernel BUG at ', re.I),
    ),
    (
        'no_space',
        'the disk ran out of space',
        re.compile(r'No space left on device|ENOSPC', re.I),
    ),
    (
        'nfs_hang',
        'a shared file system stopped answering (NFS)',
        re.compile(
            r'nfs: server \S+ not responding'
            r'|mount\.nfs4?: .*(timed out|Connection refused)'
            r'|NFS4ERR_DELAY',
            re.I,
        ),
    ),
    (
        'dns',
        'the host could not look up names on the network (DNS)',
        re.compile(
            r'Temporary failure in name resolution'
            r'|Could not resolve host'
            r'|Name or service not known',
            re.I,
        ),
    ),
)
CAUSES: Dict[str, str] = {name: cause for name, cause, _ in SIGNATURES}
UNKNOWN_SIGNATURE = 'unknown'

ANSI_OR_CONTROL = re.compile(
    r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]|[\x00-\x08\x0b-\x1f\x7f]'
)
REDACTIONS: Tuple[Tuple['re.Pattern', str], ...] = (
    (
        re.compile(r'-----BEGIN [A-Z ]*-----.*?(-----END [A-Z ]*-----|\Z)', re.DOTALL),
        '[redacted]',
    ),
    (re.compile(r'(?i)(\bauthorization\s*[:=]\s*)[^\n]+'), r'\1[redacted]'),
    (re.compile(r'(?i)\bbearer\s+\S+'), 'bearer [redacted]'),
    (
        re.compile(
            # no leading [\w.-]*: what comes before the word is kept either way, and looking
            # for it from every character of a long run is what made this slow. a quote left
            # open runs to the end of its line, where an over-long line was cut
            r'(?im)((?:password|passwd|pwd|passphrase|secret|token|api[_-]?key'
            r'|access[_-]?key|private[_-]?key|credential)[\w.-]*["\']?\s*[:= ]\s*)'
            r'("[^"\n]*(?:"|$)|\'[^\'\n]*(?:\'|$)|[^\s,;]+)'
        ),
        r'\1[redacted]',
    ),
    (re.compile(r'://[^/\s:@]+:[^/\s@]+@'), '://[redacted]@'),
    (re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'), '[redacted]'),
    # a long unbroken run is a key or a token far more often than it is worth reading
    (re.compile(r'(?<![\w/+=-])[A-Za-z0-9+/_=-]{40,}(?![\w/+=-])'), '[redacted]'),
)
PATH_SEGMENT = r'[^\s:,;"\'()\[\]<>=/]*'
# a path outside these is someone's: a home, a project, a shared mount, scratch.
SYSTEM_PATH_ROOTS = (
    'bin|boot|dev|etc|lib|lib64|opt|proc|root|run|sbin|sys|sysroot|usr|var'
)
USER_DATA: Tuple[Tuple['re.Pattern', str], ...] = (
    # paths first, so a name inside one goes with it
    (re.compile(r'\b[A-Za-z]:\\\S+'), '[path]'),
    (
        re.compile(
            rf'(?<!/)(?:/{PATH_SEGMENT})*?/(?:home|users?|projects?|scratch)\b(?:/{PATH_SEGMENT})*',
            re.I,
        ),
        '[path]',
    ),
    (
        re.compile(rf'(?<![\w:/])/(?!(?:{SYSTEM_PATH_ROOTS})\b)[^\s:,;"\'()\[\]<>=]+'),
        '[path]',
    ),
    (re.compile(r'(?<![\w/])~[\w.-]*(?:/\S*)?'), '[path]'),
    # names in the places system tools put them
    (re.compile(r'[\w.+-]+@[\w.-]+'), '[user]'),
    (re.compile(r'\b[\w.-]+\\+[\w.$-]+'), '[user]'),
    (re.compile(r'(\b[ug]id=\d+)\([^)\s]*\)'), r'\1'),
    (
        re.compile(r'(?i)\b(uid|cn|mail|samaccountname|userprincipalname)=[^,\s]+'),
        r'\1=[user]',
    ),
    (
        re.compile(
            r'(?i)(\b(?:accepted|failed)\s+\S+\s+for\s+)(?:invalid\s+user\s+)?\S+'
        ),
        r'\1[user]',
    ),
    (
        re.compile(
            r'(?i)(\b(?:su\s+-\s*|su\s+|(?:sudo|runuser)\s+(?:-\w+\s+)*-[ul]\s+|chown\s+(?:-\w+\s+)*'
            r'|useradd\s+|usermod\s+|userdel\s+|getent\s+passwd\s+'
            r'|loginctl\s+\S+\s+))[^\s,;]+'
        ),
        r'\1[user]',
    ),
    (
        re.compile(
            r'(?i)((?:user(?:name)?|owner|logname)[\w.-]*["\']?\s*[:= ]\s*)'
            r'("[^"\n]*"|\'[^\'\n]*\'|[^\s,;:]+)'
        ),
        r'\1[user]',
    ),
)
# a shell trace repeats every command with its arguments: names, paths, whatever was typed
SHELL_TRACE_LINE = re.compile(r'^\s*\++\s')
# what a package transaction prints: what is installed is not why a host failed, and on GovCloud
# it is exactly what must not spread. the restricted dataset is the control; this just sends less.
PACKAGE_LINE = re.compile(
    r'^\s*(?:'
    r'(?:Installing|Upgrading|Updating|Downgrading|Reinstalling|Removing|Erasing|Verifying'
    r'|Cleanup|Preparing|Running scriptlet|Obsoleting)\s*:'
    r'|(?:Installed|Upgraded|Removed|Dependencies resolved|Packages? altered)\s*:?\s*$'
    r'|Unpacking\s|Setting up\s|Preparing to unpack\s|Selecting previously unselected\s'
    r'|Get:\d+\s|Hit:\d+\s|Ign:\d+\s|Fetched\s'
    r'|\S+\.(?:x86_64|aarch64|noarch|i686)\b'
    r'|\S+-\d[\w.:~+-]*\.(?:rpm|deb)\b'
    r')'
)


@dataclass
class FailedHost:
    kind: str  # desktop, desktop_bake, compute_bake
    identifier: Optional[str]  # session id or build id
    instance_id: Optional[str]
    failure: Optional[str] = None
    os: Optional[str] = None
    arch: Optional[str] = None
    ami: Optional[str] = None
    instance_type: Optional[str] = None
    release: Optional[str] = None
    log_group: Optional[str] = None
    log_stream: Optional[str] = None
    owner: Optional[str] = None  # never logged: removed from everything that is
    # the reason tag when the host says nothing known, from the caller's own record of it
    fallback_reason: Optional[str] = None


def bounded(text: Optional[str]) -> str:
    """the end of it, from the start of a line: a cut inside a token would leave half a secret."""
    text = (text or '').rstrip()
    if len(text) > READ_CHARS:
        cut = len(text) - READ_CHARS
        start = text.find('\n', cut) + 1
        if not start:
            # the last line is longer than what is read: its start, never its middle
            start = text.rfind('\n', 0, cut) + 1
        text = text[start : start + READ_CHARS]
    # a key block whose first line was cut away, here or by EC2, has no header left to find
    end = text.find(PEM_END)
    if end != -1 and not 0 <= text.find(PEM_BEGIN) < end:
        text = text[text.find('\n', end) + 1 :] if '\n' in text[end:] else ''
    return text


def _whole_words(line: str) -> str:
    """a line no pattern can be slow on. cut at a space, so what is left of a secret is all of it
    or none; a line with no space that far in is one token, and says nothing worth keeping."""
    if len(line) <= CUT_CHARS:
        return line
    head = line[:CUT_CHARS]
    return head[: max(head.rfind(' '), head.rfind('\t')) + 1] or '[redacted]'


def _wrapped(text: str) -> str:
    """long lines folded for the signature search, overlapping so no phrase is lost at a fold."""
    step = CUT_CHARS - 100
    return '\n'.join(
        line[start : start + CUT_CHARS]
        for line in text.split('\n')
        for start in range(0, max(len(line) - 100, 1), step)
    )


def detect_signature(*texts: Optional[str]) -> str:
    joined = _wrapped('\n'.join(bounded(text) for text in texts if text))
    for name, _, pattern in SIGNATURES:
        if pattern.search(joined):
            return name
    return UNKNOWN_SIGNATURE


def _reason(host: FailedHost, signature: str) -> str:
    if signature == UNKNOWN_SIGNATURE and host.fallback_reason:
        return host.fallback_reason
    return signature


def plain_cause(signature: Optional[str]) -> Optional[str]:
    """one sentence for the portal, from the table above and nothing else."""
    cause = CAUSES.get(signature or '')
    return f'Likely cause: {cause}.' if cause else None


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def scrub(text: str, owner: Optional[str] = None) -> str:
    """redacted, with the owner, other usernames and personal paths gone."""
    text = redact(text)
    if owner:
        text = re.sub(
            rf'(?<![\w.-]){re.escape(owner)}(?![\w-])', '[user]', text, flags=re.I
        )
    for pattern, replacement in USER_DATA:
        text = pattern.sub(replacement, text)
    return text


def clean_for_log(
    text: Optional[str], limit: int, owner: Optional[str] = None
) -> List[str]:
    """
    no shell trace or package lines, scrubbed, no control codes, the end of it, each line short.
    every line gets a prefix so no line of a host's own output can start a new log event or forge
    a marker.
    """
    if not text:
        return []
    cleaned = '\n'.join(
        _whole_words(line.rstrip())
        for line in ANSI_OR_CONTROL.sub('', bounded(text).replace('\r\n', '\n')).split(
            '\n'
        )
        if line.strip()
    )
    lines = [
        scrub(line, owner)[:LINE_CHARS]
        for line in redact(cleaned).split('\n')
        if line.strip()
        and not PACKAGE_LINE.match(line)
        and not SHELL_TRACE_LINE.match(line)
    ]
    kept: List[str] = []
    used = 0
    for line in reversed(lines):
        used += len(line) + 1
        if used > limit:
            break
        kept.append(f'| {line}')
    kept.reverse()
    return kept


def _field(value: Optional[str]) -> str:
    text = (
        re.sub(r'\s+', '_', ANSI_OR_CONTROL.sub('', str(value).strip()))
        if value
        else ''
    )
    return text[:120] or 'unknown'


def _read_console(ec2, instance_id: str) -> Tuple[str, str]:
    """(text, how it went). boto decodes the base64. latest is Nitro only, and running only."""
    try:
        try:
            output = ec2.get_console_output(InstanceId=instance_id, Latest=True)
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') in ACCESS_DENIED_CODES:
                raise
            output = ec2.get_console_output(InstanceId=instance_id)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code')
        return '', 'denied' if code in ACCESS_DENIED_CODES else 'failed'
    text = output.get('Output')
    return (text, 'ok') if isinstance(text, str) and text else ('', 'empty')


def _read_log_tail(logs, group: Optional[str], stream: Optional[str]) -> str:
    if not group or not stream:
        return ''
    events = logs.get_log_events(
        logGroupName=group,
        logStreamName=stream,
        startFromHead=False,
        limit=LOG_TAIL_EVENTS,
    ).get('events', [])
    return bounded('\n'.join(str(event.get('message', '')) for event in events))


def _fill_identity(ec2, host: FailedHost) -> None:
    if host.ami and host.instance_type:
        return
    for reservation in ec2.describe_instances(InstanceIds=[host.instance_id]).get(
        'Reservations', []
    ):
        for instance in reservation.get('Instances', []):
            host.ami = host.ami or instance.get('ImageId')
            host.instance_type = host.instance_type or instance.get('InstanceType')


def _capture(context, logger, host: FailedHost, abandoned: Event) -> Tuple[str, str]:
    ec2 = context.aws().ec2()
    console, console_state = _read_console(ec2, host.instance_id)
    tail = ''
    try:
        tail = _read_log_tail(context.aws().logs(), host.log_group, host.log_stream)
    except Exception:  # noqa - the stream may never have been created
        pass
    try:
        _fill_identity(ec2, host)
    except Exception:  # noqa - identity is a convenience
        pass
    signature = _reason(host, detect_signature(console, tail))
    if abandoned.is_set():
        return signature, console_state
    lines = [
        f'{CAPTURE_MARKER} kind={host.kind} id={_field(host.identifier)} '
        f'instance={_field(host.instance_id)} os={_field(host.os)} arch={_field(host.arch)} '
        f'ami={_field(host.ami)} instance_type={_field(host.instance_type)} '
        f'release={_field(host.release)} reason={signature} console={console_state} '
        f'failure={_field(scrub((host.failure or "")[:1000], host.owner)[:120])}'
    ]
    console_lines = clean_for_log(console, CONSOLE_TAIL_CHARS, host.owner)
    if console_lines:
        lines += ['| --- ec2 console ---', *console_lines]
    tail_lines = clean_for_log(tail, LOG_TAIL_CHARS, host.owner)
    if tail_lines:
        lines += ['| --- log tail ---', *tail_lines]
    logger.warning('\n'.join(lines))
    return signature, console_state


def capture_failure(context, logger, host: FailedHost) -> Optional[str]:
    """
    ships what the host said, counts it, and returns a sentence for the portal when a known
    signature was found. Never raises and returns within TIME_LIMIT_SECONDS whatever AWS does.
    """
    try:
        if not host.instance_id or not context.config().get_bool(
            ENABLED_KEY, default=True
        ):
            return None
    except Exception:  # noqa - a config that cannot be read is not a reason to stop disposal
        return None

    outcome: Dict[str, Tuple[str, str]] = {}
    abandoned = Event()

    def work():
        try:
            outcome['result'] = _capture(context, logger, host, abandoned)
        except Exception as e:
            outcome['result'] = (
                _reason(host, UNKNOWN_SIGNATURE),
                type(e).__name__.lower()[:32],
            )

    worker = Thread(target=work, name='failure-capture', daemon=True)
    try:
        worker.start()
        worker.join(TIME_LIMIT_SECONDS)
        if worker.is_alive():
            abandoned.set()
        signature, console_state = outcome.get(
            'result', (_reason(host, UNKNOWN_SIGNATURE), 'timeout')
        )
        HealthMetrics(context).add(
            'failure.captures',
            kind=host.kind,
            reason=signature,
            console=console_state,
            os=_field(host.os),
            arch=_field(host.arch),
        )
        return plain_cause(signature)
    except Exception:  # noqa
        return None


def _node_tag_filters(context, job_id: str) -> List[Dict]:
    return [
        {
            'Name': f'tag:{constants.IDEA_TAG_JOB_ID}',
            'Values': [ModelUtils.aws_job_id(job_id)],
        },
        {
            'Name': f'tag:{constants.IDEA_TAG_CLUSTER_NAME}',
            'Values': [context.cluster_name()],
        },
    ]


def find_job_instance(context, job_id: Optional[str]) -> Optional[str]:
    """the node launched for a job, for one that never registered and so was never named to us."""
    if not job_id:
        return None
    try:
        for reservation in (
            context.aws()
            .ec2()
            .describe_instances(Filters=_node_tag_filters(context, job_id))
            .get('Reservations', [])
        ):
            for instance in reservation.get('Instances', []):
                if instance.get('InstanceId'):
                    return instance['InstanceId']
    except Exception:  # noqa
        pass
    return None
