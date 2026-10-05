"""Shared finished-job efficiency formulas for reporting and scheduler metrics."""

import math
import re
from datetime import datetime

CPU_EFFICIENCY_MAX = 1.05


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def seconds(value):
    if isinstance(value, str) and ':' in value:
        match = re.fullmatch(r'(?:(\d+)-)?(\d+):(\d{2}):(\d{2})', value)
        if not match:
            return None
        days, hours, minutes, secs = (int(v or 0) for v in match.groups())
        if minutes >= 60 or secs >= 60:
            return None
        return days * 86400 + hours * 3600 + minutes * 60 + secs
    return numeric(value)


def memory_bytes(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    if isinstance(value, str):
        match = re.fullmatch(r'([\d.]+)\s*([a-zA-Z]+)', value)
        if not match:
            return None
        value = dict(value=match[1], unit=match[2].lower())
    if not isinstance(value, dict):
        return None
    amount = numeric(value.get('value'))
    units = {'bytes': 1, 'b': 1}
    for exponent, prefix in enumerate(('k', 'm', 'g', 't'), 1):
        units[prefix + 'b'] = 1000**exponent
        units[prefix + 'ib'] = 1024**exponent
    factor = units.get(str(value.get('unit')).lower())
    return amount * factor if amount is not None and factor else None


def instant(value):
    try:
        if isinstance(value, (int, float)):
            return value / 1000
        if not isinstance(value, datetime):
            value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.timestamp() if value.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def allocation(params):
    select = (params.get('custom_params') or {}).get('select')
    if select is not None:
        cpus, memory, nodes = 0, 0, 0
        try:
            for chunk in str(select).split('+'):
                tokens = chunk.split(':')
                count = int(tokens.pop(0)) if '=' not in tokens[0] else 1
                if count <= 0:
                    raise ValueError()
                fields = dict(token.split('=', 1) for token in tokens)
                ncpus = numeric(fields.get('ncpus'))
                mem = memory_bytes(fields.get('mem'))
                cpus = cpus + count * ncpus if cpus is not None and ncpus else None
                memory = memory + count * mem if memory is not None and mem else None
                nodes += count
            return (
                cpus,
                memory if memory is not None else memory_bytes(params.get('memory')),
                nodes,
            )
        except (ValueError, TypeError, IndexError):
            return None, None, None
    nodes = numeric(params.get('nodes') if params.get('nodes') is not None else 1)
    cpus = numeric(params.get('cpus'))
    # Resource_List.ncpus and mem are job-wide requests when no select is retained.
    return (
        cpus if cpus and nodes else None,
        memory_bytes(params.get('memory')),
        nodes,
    )


def instance_memory(job):
    """Memory of one instance a job had to itself, from the provisioned instance types."""
    # Batch and always-on capacity can share a node between jobs.
    options = job.get('provisioning_options') or {}
    if job.get('scaling_mode') != 'single-job' or options.get(
        'keep_forever', (job.get('params') or {}).get('keep_forever')
    ):
        return None
    ran = {
        host.get('instance_type')
        for host in job.get('execution_hosts') or []
        if host.get('instance_type')
    }
    sizes = {
        memory_bytes(option.get('memory'))
        for option in (job.get('provisioning_options') or {}).get('instance_types')
        or []
        if not ran or option.get('name') in ran
    } - {None}
    return sizes.pop() if len(sizes) == 1 else None


def job_efficiency(job):
    if hasattr(job, 'model_dump'):
        job = job.model_dump(mode='json', exclude_none=True)
    params = job.get('params') or {}
    cpus, requested_memory, nodes = allocation(params)
    start, end = instant(job.get('start_time')), instant(job.get('end_time'))
    elapsed = (
        end - start
        if start is not None and end is not None
        else numeric(job.get('total_time_secs'))
    )
    elapsed = elapsed if elapsed is not None and elapsed > 0 else None
    job_cpu_times, local_cpu_times, memories, host_memories = [], [], [], []
    cpu_hosts = 0
    for host in job.get('execution_hosts') or []:
        local_memories, host_cpu_times = [], []
        local_scope = True
        for run in (host.get('execution') or {}).get('runs') or []:
            used = run.get('resources_used') or {}
            cpu = numeric(used.get('cpu_time_secs'))
            if cpu is not None:
                used_cpus = numeric(used.get('cpus'))
                if used_cpus and cpus and used_cpus < cpus:
                    host_cpu_times.append(cpu)
                else:
                    job_cpu_times.append(cpu)
            mem = memory_bytes(used.get('memory'))
            if mem is not None:
                memories.append(mem)
                local_memories.append(mem)
                used_cpus = numeric(used.get('cpus'))
                local_scope &= bool(used_cpus and cpus and used_cpus < cpus)
        if host_cpu_times:
            cpu_hosts += 1
            local_cpu_times.extend(host_cpu_times)
        if local_memories and local_scope:
            host_memories.append(max(local_memories))
    # Job-wide PBS totals can repeat on host end events, so take the largest; host-local
    # samples add up, but only when every allocated host reported one.
    if job_cpu_times:
        cpu_times = [max(job_cpu_times)]
    elif local_cpu_times and (not nodes or nodes <= 1 or cpu_hosts == nodes):
        cpu_times = local_cpu_times
    else:
        cpu_times = []
    cpu = sum(cpu_times) / (elapsed * cpus) if cpu_times and elapsed and cpus else None
    if cpu is not None:
        cpu = min(cpu, 1) if cpu <= CPU_EFFICIENCY_MAX else None
    # Job-wide PBS values can repeat on host end events. Local samples identify
    # fewer CPUs and need every host before comparison with a job-wide request.
    peak_memory = max(memories) if memories else None
    if nodes and nodes > 1 and host_memories:
        peak_memory = sum(host_memories) if len(host_memories) == nodes else None
    # Without a memory request, compare with the memory of the instances the job had.
    instance = None if requested_memory else instance_memory(job)
    available = requested_memory or (instance * nodes if instance and nodes else None)
    memory = peak_memory / available if peak_memory is not None and available else None
    requested_wall = seconds(params.get('walltime'))
    wall = elapsed / requested_wall if elapsed and requested_wall else None
    core_hours = elapsed * cpus / 3600 if elapsed and cpus else None
    return dict(
        requested_cores=int(cpus) if cpus and float(cpus).is_integer() else None,
        used_cores=sum(cpu_times) / elapsed if cpu_times and elapsed else None,
        requested_memory_gib=requested_memory / 1024**3 if requested_memory else None,
        peak_memory_gib=peak_memory / 1024**3 if peak_memory is not None else None,
        instance_memory_gib=instance / 1024**3 if instance else None,
        cpu_efficiency_pct=100 * cpu if cpu is not None else None,
        memory_efficiency_pct=100 * memory if memory is not None else None,
        walltime_efficiency_pct=100 * wall if wall is not None else None,
        elapsed_hours=elapsed / 3600 if elapsed else None,
        core_hours=core_hours,
        wasted_core_hours=core_hours * (1 - cpu) if cpu is not None else None,
        nodes=int(nodes) if nodes else None,
    )


# the idle stop checks running desktops on a 30-minute schedule, so each check stands
# for half an hour of the desktop running
DESKTOP_CHECK_HOURS = 0.5


def desktop_check_idle(check, cpu_utilization_threshold):
    """
    Whether one idle-stop check found the desktop idle, by the idle stop's own rules: CPU
    under the threshold, no desktop connection and no login session. The idle stop's
    grace delay decides when to stop, not whether the desktop is idle, so it is not
    applied. None when the check cannot be read: it counts as neither idle nor in use.
    """
    if not isinstance(check, dict) or not isinstance(check.get('DCV'), dict):
        return None
    cpu = numeric(check.get('CPUAveragePerformanceLast10Secs'))
    if cpu is None:
        return None
    if cpu >= cpu_utilization_threshold:
        return False
    connections = numeric(check['DCV'].get('num-of-connections'))
    if connections is None:
        return None
    # Windows hosts report no login sessions
    logins = numeric(check.get('SSH_Connection_Count')) or 0
    return connections == 0 and logins == 0
