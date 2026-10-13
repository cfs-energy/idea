"""Container startup reconciliation and the supervised daily log archive."""

import argparse
import logging
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
from threading import Event

from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)
DAY_SECONDS = 24 * 60 * 60


def configure_licenses(table, pbs_home):
    """Restore the host's manual license declarations before either PBS daemon starts."""
    licenses = {}
    scan = {'ConsistentRead': True}
    while True:
        try:
            page = table.scan(**scan)
        except ClientError as error:
            if error.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
            LOGGER.info('License table does not exist yet; no licenses to restore')
            return
        for item in page.get('Items', []):
            name, command = item['name'], item['availability_check_cmd']
            if not re.fullmatch(r'[a-z][a-z0-9]*_lic_[a-z][a-z0-9]*', name):
                raise ValueError(f'Invalid PBS license resource name: {name!r}')
            if (
                not command.strip()
                or any(c in command for c in '\r\n\x00')
                or ('"' in command and "'" in command)
            ):
                raise ValueError(f'Invalid PBS dynamic resource command for {name}')
            licenses[name] = command
        if not page.get('LastEvaluatedKey'):
            break
        scan['ExclusiveStartKey'] = page['LastEvaluatedKey']

    home = Path(pbs_home)
    resource_file = home / 'server_priv/resourcedef'
    config_file = home / 'sched_priv/sched_config'
    resources = resource_file.read_text()
    config = config_file.read_text()
    for name in sorted(licenses):
        existing = re.search(rf'^{name}\s+([^\n]*)', resources, re.MULTILINE)
        if existing:
            if existing[1].split('#', 1)[0].strip() != 'type=long':
                raise ValueError(f'Conflicting PBS resource definition for {name}')
        else:
            resources = resources.rstrip('\n') + f'\n{name} type=long\n'

    def add_resources(match):
        names = [name.strip() for name in match[3].split(',')]
        for name in ['compute_node', *sorted(licenses)]:
            if name not in names:
                names.append(name)
        return match[1] + match[2] + ', '.join(names) + match[2] + match[4]

    config, count = re.subn(
        r"""^(\s*resources:\s*)(["'])([^\n]*?)\2([^\n]*)$""",
        add_resources,
        config,
        flags=re.MULTILINE,
    )
    if not count:
        raise ValueError('sched_config has no active resources declaration')
    # Replace only entries represented in the table; keep unrelated operator policy.
    config = (
        '\n'.join(
            line
            for line in config.splitlines()
            if not any(
                re.match(rf"""^\s*server_dyn_res:\s*["']{name}\s""", line)
                for name in licenses
            )
        )
        + '\n'
    )
    for name, command in sorted(licenses.items()):
        quote = "'" if '"' in command else '"'
        config += f'server_dyn_res: {quote}{name} !{command}{quote}\n'
    resource_file.write_text(resources)
    config_file.write_text(config)
    LOGGER.info('Restored %d PBS license resources', len(licenses))


def bastion_hosts(ec2, security_group):
    """Find outbound clients, excluding the NLB that shares the bastion security group."""
    hosts = set()
    pages = ec2.get_paginator('describe_network_interfaces').paginate(
        Filters=[
            {'Name': 'group-id', 'Values': [security_group]},
            {'Name': 'interface-type', 'Values': ['interface']},
            {'Name': 'status', 'Values': ['in-use']},
        ]
    )
    for page in pages:
        for interface in page['NetworkInterfaces']:
            address = interface['PrivateIpAddress']
            hostname = socket.gethostbyaddr(address)[0]
            if address not in socket.gethostbyname_ex(hostname)[2]:
                raise ValueError(f'Reverse DNS does not forward-confirm {address}')
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', hostname):
                raise ValueError(f'Invalid bastion hostname: {hostname!r}')
            hosts.add(hostname)
    if not hosts:
        raise ValueError('No active bastion interfaces found; cannot grant qmgr access')
    return sorted(hosts)


def run_archive(script):
    command = ['/bin/bash', str(script)]
    with subprocess.Popen(command, start_new_session=True) as process:
        try:
            if process.wait(timeout=3600) != 0:
                raise subprocess.CalledProcessError(process.returncode, command)
        finally:
            # Stop aws children too, so timeout/shutdown cannot strand the archive lock.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def archive_logs(script, stop):
    """Run immediately, then daily; a failed upload is logged and retried next day."""
    while not stop.is_set():
        try:
            run_archive(script)
        except (OSError, subprocess.SubprocessError):
            LOGGER.exception('Scheduler log archive failed')
        if stop.wait(DAY_SECONDS):
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task', choices=['licenses', 'bastion-hosts', 'archive-logs'])
    parser.add_argument('argument')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.task == 'archive-logs':
        # Raising interrupts the wait and cleans up the archive process group.
        def terminate(_signum, _frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        archive_logs(args.argument, Event())
        return

    import boto3

    session = boto3.Session(region_name=os.environ['AWS_DEFAULT_REGION'])
    if args.task == 'licenses':
        table = session.resource('dynamodb').Table(
            f'{os.environ["IDEA_CLUSTER_NAME"]}.{os.environ["IDEA_MODULE_ID"]}.license-resources'
        )
        configure_licenses(table, args.argument)
    else:
        for hostname in bastion_hosts(session.client('ec2'), args.argument):
            print(hostname)


if __name__ == '__main__':
    main()
