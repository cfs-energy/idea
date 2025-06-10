#!/usr/bin/env python
"""
OpenPBS Hook: calculate_ncpus_hook

This hook calculates the proper number of ncpus for a job based on the instance type
and hyper-threading settings before the job is submitted to PBS. This ensures that
jobs submitted via API or directly via qsub both get the same treatment.

To install this hook:
> qmgr -c "create hook calculate_ncpus event='queuejob'"
> qmgr -c "import hook calculate_ncpus application/x-python default ${IDEA_APP_DEPLOY_DIR}/scheduler/resources/openpbs/hooks/calculate_ncpus_hook.py"

Author: @cfsnate
"""

import pbs  # type: ignore[import-unresolved]
import sys
import os
import json
import urllib.request

# Initialize constants
DEFAULT_ENCODING = 'utf-8'
DEBUG_MODE = True

# EC2 Instance Metadata endpoints
EC2_INSTANCE_METADATA_LATEST = 'http://169.254.169.254/latest'
EC2_INSTANCE_METADATA_URL_PREFIX = f'{EC2_INSTANCE_METADATA_LATEST}/meta-data'
EC2_INSTANCE_METADATA_API_URL = f'{EC2_INSTANCE_METADATA_LATEST}/api'
EC2_INSTANCE_METADATA_TOKEN_REQUEST_HEADER = 'X-aws-ec2-metadata-token-ttl-seconds'
EC2_INSTANCE_METADATA_TOKEN_HEADER = 'X-aws-ec2-metadata-token'
EC2_INSTANCE_METADATA_TOKEN_REQUEST_TTL = '900'  # Value in seconds

# AWS API endpoints
AWS_REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
EC2_API_ENDPOINT = f'https://ec2.{AWS_REGION}.amazonaws.com'
EC2_API_VERSION = '2016-11-15'

# Cache for EC2 instance type information
# Format: {'instance_type': {'vcpus': X, 'cores': Y, 'threads_per_core': Z}}
INSTANCE_TYPE_CACHE = {}

# Queue names that should be skipped for ncpus calculation
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


def get_imds_auth_token() -> str:
    """
    Generate an IMDSv2 authentication token
    :returns: str - A suitable value for follow-up authenticated requests.
    """
    try:
        req = urllib.request.Request(
            url=f'{EC2_INSTANCE_METADATA_API_URL}/token', data=b'', method='PUT'
        )
        req.add_header(
            EC2_INSTANCE_METADATA_TOKEN_REQUEST_HEADER,
            EC2_INSTANCE_METADATA_TOKEN_REQUEST_TTL,
        )

        with urllib.request.urlopen(req, timeout=2) as conn:
            content = conn.read()
            if not content:
                raise SocaException(
                    message=f'Failed to retrieve EC2 instance IMDSv2 authentication token. Empty Reply. Statuscode: ({conn.status})'
                )
            else:
                if conn.status != 200:
                    raise SocaException(
                        message=f'Failed to retrieve EC2 instance IMDSv2 authentication token. Statuscode: ({conn.status})'
                    )
                else:
                    return content.decode(DEFAULT_ENCODING)
    except Exception as e:
        log_error(f'Error getting IMDSv2 token: {str(e)}')
        return ''


def get_ec2_instance_type_info_from_aws(instance_type: str) -> dict:
    """
    Attempt to get instance type information from AWS EC2 API
    :param instance_type: The EC2 instance type name (e.g., 'c5.4xlarge')
    :returns: Dictionary with vcpus, cores, and threads_per_core or None if failed
    """
    try:
        # Try to use the AWS CLI command to get instance type information
        command = f"aws ec2 describe-instance-types --instance-types {instance_type} --query 'InstanceTypes[0]' --output json"
        log_debug(f'Executing AWS CLI command: {command}')

        # Use os.popen to execute the command and get the output
        import subprocess

        process = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            log_error(
                f'Error executing AWS CLI command: {stderr.decode(DEFAULT_ENCODING)}'
            )
            return None

        # Parse the output as JSON
        instance_data = json.loads(stdout.decode(DEFAULT_ENCODING))

        # Extract relevant information
        vcpu_info = instance_data.get('VCpuInfo', {})
        default_vcpus = vcpu_info.get('DefaultVCpus')
        default_cores = vcpu_info.get('DefaultCores')
        default_threads_per_core = vcpu_info.get('DefaultThreadsPerCore')

        if default_vcpus and default_cores and default_threads_per_core:
            result = {
                'vcpus': default_vcpus,
                'cores': default_cores,
                'threads_per_core': default_threads_per_core,
            }
            # Update cache
            INSTANCE_TYPE_CACHE[instance_type] = result
            log_debug(
                f'Successfully retrieved instance type info from AWS API: {result}'
            )
            return result

        log_error(
            f'Failed to extract instance type information from AWS API response: {instance_data}'
        )
        return None

    except Exception as e:
        log_error(f'Error getting instance type information from AWS API: {str(e)}')
        return None


def get_instance_type_info(instance_type: str) -> dict:
    """
    Get instance type information with CPU details
    :param instance_type: The EC2 instance type name (e.g., 'c5.4xlarge')
    :returns: Dictionary with vcpus, cores, and threads_per_core
    """
    # Check the cache first
    if instance_type in INSTANCE_TYPE_CACHE:
        log_debug(f'Using cached instance type info for {instance_type}')
        return INSTANCE_TYPE_CACHE[instance_type]

    # Try to get from AWS API
    aws_info = get_ec2_instance_type_info_from_aws(instance_type)
    if aws_info:
        return aws_info

    # If AWS API fails, use simple default value
    log_debug(f'Using default value for instance type {instance_type}')
    default_info = {'vcpus': 1, 'cores': 1, 'threads_per_core': 1}

    # Update the cache with the default
    INSTANCE_TYPE_CACHE[instance_type] = default_info
    return default_info


def calculate_ncpus(job):
    """
    Calculate the proper ncpus value based on instance type and HT settings
    :param job: The PBS job object
    :returns: The appropriate ncpus value
    """
    # Get job resource list
    resources = job.Resource_List
    if not resources:
        log_debug('No Resource_List found in job')
        return None

    # Get instance type
    instance_type = None
    if 'instance_type' in resources:
        instance_type = str(resources['instance_type'])

    if not instance_type:
        log_debug('No instance_type specified in job resources')
        return None

    # Check if multiple instance types are specified
    if '+' in instance_type:
        # When multiple instance types are specified, use the first one for calculation
        instance_type = instance_type.split('+')[0]
        log_debug(
            f'Multiple instance types specified, using the first one: {instance_type}'
        )

    # Check if hyper-threading is enabled
    ht_support = True  # Default is True
    if 'ht_support' in resources:
        ht_support_value = str(resources['ht_support']).lower()
        ht_support = ht_support_value in ['true', 'yes', '1']

    # Get instance type info
    instance_info = get_instance_type_info(instance_type)

    # Calculate ncpus based on hyper-threading settings
    if ht_support:
        ncpus = instance_info['vcpus']  # With HT enabled, use vCPUs
    else:
        ncpus = instance_info['cores']  # With HT disabled, use physical cores

    log_debug(
        f'Calculated ncpus for {instance_type} with ht_support={ht_support}: {ncpus}'
    )
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


def update_select_statement(job, ncpus):
    """
    Update the select statement with the calculated ncpus
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

    # Get current select statement
    select = str(resources['select'])

    # Parse select statement
    parsed, count = parse_select(select)
    if not parsed:
        log_error('Could not parse select statement')
        return

    # Update ncpus in the parsed select statement
    parsed['ncpus'] = str(ncpus)

    # Reconstruct select statement
    new_select = f'{count}'
    for key, value in parsed.items():
        new_select += f':{key}={value}'

    # If there's compute_node in the original select, preserve it
    if 'compute_node' in select:
        compute_node = select.split('compute_node=')[1].split(':')[0]
        if 'compute_node' not in parsed:
            new_select += f':compute_node={compute_node}'

    # Update job's select statement
    job.Resource_List['select'] = pbs.select(new_select)
    log_debug(f'Updated select statement: {new_select}')


def should_skip_ncpus_calculation(job):
    """
    Check if the job should skip ncpus calculation based on queue
    :param job: The PBS job object
    :returns: True if calculation should be skipped, False otherwise
    """
    # Get the queue name
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

    # Check if we should skip ncpus calculation for this job's queue
    if should_skip_ncpus_calculation(job):
        log_debug('Skipping ncpus calculation due to job queue')
        e.accept()
        sys.exit(0)

    # Calculate ncpus
    ncpus = calculate_ncpus(job)

    if ncpus:
        # Don't set standalone ncpus resource as it conflicts with select statement
        # job.Resource_List['ncpus'] = ncpus  # Remove this line

        # Update select statement if present
        update_select_statement(job, ncpus)

        log_debug(f'Set ncpus in select statement to {ncpus}')
    else:
        log_debug('Could not calculate ncpus, using default values')

    # Accept the job
    e.accept()

except Exception as ex:
    log_error(f'Error in calculate_ncpus_hook: {str(ex)}')
    # Don't reject the job on error, just continue with the existing settings
    e.accept()
