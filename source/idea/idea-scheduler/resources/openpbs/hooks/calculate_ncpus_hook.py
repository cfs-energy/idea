"""
Calculate ncpus hook for OpenPBS (queuejob)

Fills in the CPU count of a job's select when it has none, from what the scheduler app
knows about the instance type: the app answers OpenPBSHook.InstanceTypeInfo over the unix
socket the validate_job hook already uses, out of its instance type cache. Nothing runs
through a shell from inside pbs_server. An ncpus the user, or validate_job, already put
in the select is kept as it is. When the count cannot be learned the job is rejected with
a clear message rather than queued with ncpus=1 on a paid node.

Install:
> qmgr -c "create hook calculate_ncpus event='queuejob'"
> qmgr -c "import hook calculate_ncpus application/x-python default ${IDEA_APP_DEPLOY_DIR}/scheduler/resources/openpbs/hooks/calculate_ncpus_hook.py"
> qmgr -c "set hook calculate_ncpus order=2"
"""

import pbs  # type: ignore[import-unresolved]
import sys
import os
import json
import socket
import uuid

# Initialize constants
DEFAULT_ENCODING = 'utf-8'
DEBUG_MODE = True
SOCKET_TIMEOUT_SECS = 10
SOCKET_RECV_BUFFER_SIZE = 1024

# Cache for EC2 instance type information
# Format: {'instance_type': {'vcpus': X, 'cores': Y, 'threads_per_core': Z}}
INSTANCE_TYPE_CACHE = {}

# Queues that pack jobs onto a node keep the select the job asked for
SKIP_NCPUS_CALCULATION_QUEUES = ['job-shared']


class SocaException(Exception):
    def __init__(self, message):
        self.message = message


def _log(level, message: str):
    pbs.logmsg(level, message)


def log_fine(message: str):
    if not DEBUG_MODE:
        return
    _log(level=pbs.LOG_DEBUG, message=message)


def log_debug(message: str):
    _log(level=pbs.LOG_DEBUG, message=message)


def log_error(message: str):
    _log(level=pbs.LOG_ERROR, message=message)


def get_socket_address() -> str:
    address = os.environ.get('IDEA_SCHEDULER_UNIX_SOCKET', '').strip() or '/run/idea.sock'
    if not os.path.exists(address):
        raise SocaException(f'idea-scheduler is not running (no socket at {address})')
    return address


def ask_scheduler(namespace: str, payload: dict) -> dict:
    """
    raw HTTP over the unix socket, as the validate_job hook does it: pbs python carries no
    external modules, and nothing is shelled out from inside pbs_server
    """
    address = get_socket_address()
    request = json.dumps(
        {
            'header': {'namespace': namespace, 'request_id': str(uuid.uuid4())},
            'payload': payload,
        }
    ).encode(DEFAULT_ENCODING)
    http_header = (
        'POST /scheduler/api/v1 HTTP/1.1\r\n'
        'Content-Type: application/json\r\n'
        'Accept-Charset: utf-8\r\n'
        'Connection: close\r\n'
        f'Content-Length: {len(request)}\r\n\r\n'
    ).encode(DEFAULT_ENCODING)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(SOCKET_TIMEOUT_SECS)
        client.connect(address)
        client.sendall(http_header + request)
        buffer = bytearray()
        while True:
            data = client.recv(SOCKET_RECV_BUFFER_SIZE)
            if not data:
                break
            buffer += data
    except socket.timeout:
        raise SocaException(
            f'idea-scheduler did not answer within {SOCKET_TIMEOUT_SECS} seconds'
        )
    except OSError as exc:
        raise SocaException(f'could not reach idea-scheduler at {address}: {exc}')
    finally:
        client.close()
    # the body is the last line of the response, after the headers
    lines = [line for line in buffer.decode(DEFAULT_ENCODING).splitlines() if line.strip()]
    if not lines:
        raise SocaException('idea-scheduler sent an empty response')
    try:
        return json.loads(lines[-1])
    except ValueError:
        raise SocaException('idea-scheduler sent a response that is not JSON')


def get_instance_type_info(instance_type: str) -> dict:
    """
    :returns: {'vcpus': X, 'cores': Y, 'threads_per_core': Z} for the instance type
    :raises SocaException: when the scheduler app cannot say
    """
    if instance_type in INSTANCE_TYPE_CACHE:
        return INSTANCE_TYPE_CACHE[instance_type]
    response = ask_scheduler(
        'OpenPBSHook.InstanceTypeInfo', {'instance_type': instance_type}
    )
    if not response.get('success'):
        raise SocaException(
            response.get('message')
            or response.get('error_code')
            or f'instance type lookup failed for {instance_type}'
        )
    payload = response.get('payload') or {}
    try:
        info = {key: int(payload[key]) for key in ('vcpus', 'cores', 'threads_per_core')}
    except (KeyError, TypeError, ValueError):
        raise SocaException(f'idea-scheduler has no CPU information for {instance_type}')
    INSTANCE_TYPE_CACHE[instance_type] = info
    log_debug(f'instance type {instance_type}: {info}')
    return info


def calculate_ncpus(job) -> int:
    """
    The CPUs a whole node brings: vCPUs, or cores with hyper-threading off, the smallest
    across the job's instance types
    :raises SocaException: when the job names no instance type or the count is unknown
    """
    resources = job.Resource_List
    instance_types = (
        str(resources['instance_type']) if resources and 'instance_type' in resources else ''
    )
    if not instance_types:
        raise SocaException('the job names no instance_type')

    ht_support = True  # Default is True
    if 'ht_support' in resources:
        ht_support = str(resources['ht_support']).lower() in ['true', 'yes', '1']

    counts = []
    for instance_type in instance_types.split('+'):
        info = get_instance_type_info(instance_type.strip())
        counts.append(info['vcpus'] if ht_support else info['cores'])
    ncpus = min(counts)
    log_debug(f'Calculated ncpus for {instance_types} with ht_support={ht_support}: {ncpus}')
    return ncpus


def parse_select(select_str: str):
    """
    Parse the select statement from PBS job
    :param select_str: The select statement string
    :returns: Dictionary with parsed values and the count
    """
    if not select_str:
        return None, None

    # Parse select statement (format: count:key1=value1:key2=value2...)
    parts = select_str.split(':')

    # Get count and initialize parsed values
    count = 1  # Default count
    try:
        if parts and parts[0].isdigit():
            count = int(parts[0])
            parts = parts[1:]
    except (ValueError, IndexError):
        pass

    # Parse key=value pairs
    parsed = {}
    for part in parts:
        if '=' in part:
            key, value = part.split('=', 1)
            parsed[key] = value

    return parsed, count


def requested_ncpus(job):
    """the ncpus the job's select already names, or None"""
    resources = job.Resource_List
    if not resources or 'select' not in resources:
        return None
    parsed, _ = parse_select(str(resources['select']))
    value = (parsed or {}).get('ncpus')
    try:
        return int(value) if value else None
    except ValueError:
        return None


def update_select_statement(job, ncpus):
    """
    Put ncpus into a select that has none
    :param job: The PBS job object
    :param ncpus: The calculated ncpus value
    """
    resources = job.Resource_List

    # Check if select is specified
    if 'select' not in resources:
        # If no select statement, create one
        nodes = 1
        if 'nodes' in resources:
            nodes = int(resources['nodes'])

        job.Resource_List['select'] = pbs.select(f'{nodes}:ncpus={ncpus}')
        log_debug(f'Created select statement: {nodes}:ncpus={ncpus}')
        return

    select = str(resources['select'])
    parsed, count = parse_select(select)
    if parsed is None:
        log_error('Could not parse select statement')
        return
    parsed['ncpus'] = str(ncpus)

    new_select = f'{count}'
    for key, value in parsed.items():
        new_select += f':{key}={value}'

    job.Resource_List['select'] = pbs.select(new_select)
    log_debug(f'Updated select statement: {new_select}')


def should_skip_ncpus_calculation(job):
    """
    Check if the job should skip ncpus calculation based on queue
    :param job: The PBS job object
    :returns: True if calculation should be skipped, False otherwise
    """
    queue_name = None
    if hasattr(job, 'queue') and job.queue:
        queue_name = str(job.queue)

    if queue_name in SKIP_NCPUS_CALCULATION_QUEUES:
        log_debug(f'Skipping ncpus calculation for job in queue: {queue_name}')
        return True

    return False


# Main hook function
try:
    e = pbs.event()

    # Only process queuejob events
    if e.type != pbs.QUEUEJOB:
        e.accept()
        sys.exit(0)

    job = e.job
    log_debug(
        f'Processing job {job.id if hasattr(job, "id") else "new"} for ncpus calculation'
    )

    if should_skip_ncpus_calculation(job):
        e.accept()
        sys.exit(0)

    requested = requested_ncpus(job)
    if requested:
        # the user's, or validate_job's, count stands: never rewritten down
        log_debug(f'select already names ncpus={requested}: left as requested')
        e.accept()
        sys.exit(0)

    try:
        ncpus = calculate_ncpus(job)
    except Exception as ex:
        reason = ex.message if isinstance(ex, SocaException) else str(ex)
        log_error(f'ncpus not set for job: {reason}')
        e.reject(
            f'IDEA could not determine the CPU count for this job: {reason}. '
            f'Retry in a minute, or request the CPUs explicitly with -l select=N:ncpus=M.'
        )
        sys.exit(0)

    update_select_statement(job, ncpus)
    e.accept()

except SystemExit:
    raise
except Exception as ex:
    log_error(f'Error in calculate_ncpus_hook: {str(ex)}')
    # Don't reject the job on a hook error, just continue with the existing settings
    e.accept()
