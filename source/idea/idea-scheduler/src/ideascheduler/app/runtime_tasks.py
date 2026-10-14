"""Container startup reconciliation and the supervised daily log archive."""

import argparse
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
from threading import Event

from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)
DAY_SECONDS = 24 * 60 * 60


# the names of the licenses IDEA restored, next to their check scripts
MANAGED = '.managed'
LICENSE_NAME = re.compile(r'[a-z][a-z0-9_]*')


def _license_rows(table):
    """the table's rows, each validated on its own: a bad row is logged and skipped"""
    from ideascheduler.app.licenses.license_service import PATTERN_LICENSE_RESOURCE_NAME

    licenses = {}
    scan = {'ConsistentRead': True}
    while True:
        try:
            page = table.scan(**scan)
        except ClientError as error:
            if error.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
            LOGGER.info('License table does not exist yet; no licenses to restore')
            return None
        for item in page.get('Items', []):
            name = str(item.get('name') or '')
            command = str(item.get('availability_check_cmd') or '')
            # the API's own rule, plus a plain name for the file the check runs from
            if not PATTERN_LICENSE_RESOURCE_NAME.match(
                name
            ) or not LICENSE_NAME.fullmatch(name):
                LOGGER.error('Skipping license resource with an invalid name: %r', name)
                continue
            if not command.strip() or '\x00' in command:
                LOGGER.error('Skipping license resource %s: no usable command', name)
                continue
            licenses[name] = command
        if not page.get('LastEvaluatedKey'):
            break
        scan['ExclusiveStartKey'] = page['LastEvaluatedKey']
    return licenses


def configure_licenses(table, pbs_home):
    """
    Restore the host's manual license declarations before either PBS daemon starts.
    Each check runs from its own script under PBS_HOME, so a command carries no quoting
    rules. A row that cannot be restored is logged and skipped: one entry must not keep
    the scheduler down. Only licenses IDEA restored before (recorded in .managed) are
    removed when they leave the table; resources an operator declared by hand stay. A
    table that cannot be read leaves every file as it is.
    """
    try:
        licenses = _license_rows(table)
    except Exception as error:
        LOGGER.error(
            'License table could not be read, PBS license resources left as they are: %s',
            error,
        )
        return
    if licenses is None:
        return
    home = Path(pbs_home)
    resource_file = home / 'server_priv/resourcedef'
    config_file = home / 'sched_priv/sched_config'
    checks_dir = home / 'idea' / 'licenses'
    managed_file = checks_dir / MANAGED
    managed = set(managed_file.read_text().split()) if managed_file.is_file() else set()
    resources = resource_file.read_text()
    config = config_file.read_text()

    restored = {}
    for name, command in sorted(licenses.items()):
        existing = re.search(rf'^{name}\s+([^\n]*)', resources, re.MULTILINE)
        if existing and existing[1].split('#', 1)[0].strip() != 'type=long':
            LOGGER.error(
                'Skipping license resource %s: resourcedef declares it as %r',
                name,
                existing[1].strip(),
            )
            continue
        restored[name] = command

    def stale(name):
        # restored by IDEA on an earlier start and gone from the table since
        return name in managed and name not in restored

    kept = []
    for line in resources.splitlines():
        declared = re.match(r'^([a-z][a-z0-9_]*)\s+type=long\s*$', line)
        if declared and stale(declared[1]):
            LOGGER.info('Removing resource %s: no longer a license', declared[1])
            continue
        kept.append(line)
    resources = '\n'.join(kept).rstrip('\n') + '\n'
    for name in sorted(restored):
        if not re.search(rf'^{name}\s', resources, re.MULTILINE):
            resources += f'{name} type=long\n'

    def add_resources(match):
        names = [
            n.strip() for n in match[3].split(',') if n.strip() and not stale(n.strip())
        ]
        for name in ['compute_node', *sorted(restored)]:
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
        LOGGER.error(
            'sched_config has no active resources declaration; licenses not restored'
        )
        return
    quote = re.search(r"""^\s*resources:\s*(["'])""", config, re.MULTILINE)[1]
    # the dynamic resources of licenses IDEA restores or retires are rewritten; anything
    # else the operator put there stays
    config = (
        '\n'.join(
            line
            for line in config.splitlines()
            if not (
                (entry := re.match(r"""^\s*server_dyn_res:\s*["']([^\s"']+)\s""", line))
                and (entry[1] in restored or stale(entry[1]))
            )
        )
        + '\n'
    )
    checks_dir.mkdir(parents=True, exist_ok=True)
    for script in checks_dir.iterdir():
        if script.name != MANAGED and script.name not in restored:
            script.unlink()
    for name, command in sorted(restored.items()):
        script = checks_dir / name
        script.write_text(f'#!/bin/bash\n{command}\n')
        script.chmod(0o755)
        config += f'server_dyn_res: {quote}{name} !{script}{quote}\n'
    resource_file.write_text(resources)
    config_file.write_text(config)
    managed_file.write_text(''.join(f'{name}\n' for name in sorted(restored)))
    LOGGER.info(
        'Restored %d PBS license resources (%d skipped)',
        len(restored),
        len(licenses) - len(restored),
    )


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
    parser.add_argument('task', choices=['licenses', 'archive-logs'])
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
    table = session.resource('dynamodb').Table(
        f'{os.environ["IDEA_CLUSTER_NAME"]}.{os.environ["IDEA_MODULE_ID"]}.license-resources'
    )
    configure_licenses(table, args.argument)


if __name__ == '__main__':
    main()
