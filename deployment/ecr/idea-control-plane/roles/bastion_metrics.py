"""
Bastion sessions, health and dropped connections, for each task, as DogStatsD. A separate process
with only the standard library, so that nothing here can stop sshd or slow its log path: the
role restarts it if it exits and sshd never waits for it. It counts; it keeps no username or
address, and reads only process titles, the kernel's socket table and the drop tally the role
writes (each line holds just the phrase that matched).
"""

import os
import socket
import sys
import time

DROPS_FILE = os.environ.get('SSHD_DROPS_FILE', '/run/idea-sshd-drops')
INTERVAL_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 5
# Bounded even if sshd is refusing a client every second: read lines are cut away at this size.
DROPS_MAX_BYTES = 1024 * 1024
TCP_ESTABLISHED = '01'
SSH_PORT_HEX = f'{22:04X}'


def active_sessions(proc_root: str = '/proc') -> int:
    """one per logged-in connection: sshd titles its per-user process user@pts/N or user@notty."""
    count = 0
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, entry, 'cmdline'), 'rb') as handle:
                title = handle.read(256).split(b'\0', 1)[0]
        except OSError:
            continue
        if title.startswith((b'sshd: ', b'sshd-session: ')) and b'@' in title:
            count += 1
    return count


def established_connections(table: str = '/proc/net/tcp') -> int:
    count = 0
    try:
        with open(table) as handle:
            next(handle, None)
            for line in handle:
                fields = line.split()
                if (
                    len(fields) > 3
                    and fields[3] == TCP_ESTABLISHED
                    and fields[1].endswith(f':{SSH_PORT_HEX}')
                ):
                    count += 1
    except OSError:
        pass
    return count


def sshd_check(address=('127.0.0.1', 22)):
    """whether sshd offers its banner, and how fast; closes the way the task's health check does."""
    started = time.monotonic()
    try:
        with socket.create_connection(address, CHECK_TIMEOUT_SECONDS) as connection:
            connection.settimeout(CHECK_TIMEOUT_SECONDS)
            banner = connection.recv(64)
            connection.sendall(b'SSH-2.0-idea-health\r\n')
        return banner.startswith(b'SSH-'), time.monotonic() - started
    except OSError:
        return False, None


class DropTally:
    def __init__(self, path: str = DROPS_FILE):
        self.path = path
        self.offset = 0

    def read(self):
        counts = {}
        try:
            with open(self.path, 'rb') as handle:
                handle.seek(self.offset)
                data = handle.read()
                self.offset += len(data)
        except OSError:
            return counts
        for line in data.splitlines():
            if b'Maxstartups' in line:
                source = 'loopback' if b'127.0.0.1' in line else 'remote'
                key = ('maxstartups', source)
            elif b'before authentication' in line:
                key = ('auth_timeout', 'remote')
            elif b'client not responding' in line:
                key = ('client_timeout', 'remote')
            else:
                continue
            counts[key] = counts.get(key, 0) + 1
        if self.offset > DROPS_MAX_BYTES:
            try:
                os.truncate(self.path, 0)
                self.offset = 0
            except OSError:
                pass
        return counts


def lines(cluster: str, tally: DropTally, proc_root: str = '/proc'):
    # by cluster: each task has its own host, and the hostname of a task is its private address
    base = f'idea_cluster:{cluster},idea_module:bastion-host'

    def gauge(name, value):
        return f'idea.health.bastion.{name}:{value}|g|#{base}|card:none'

    up, seconds = sshd_check()
    found = [
        gauge('sshd_up', 1 if up else 0),
        gauge('sessions', active_sessions(proc_root)),
        gauge('tcp_established', established_connections()),
    ]
    if seconds is not None:
        found.append(gauge('sshd_check_seconds', round(seconds, 3)))
    for (reason, source), count in tally.read().items():
        found.append(
            f'idea.health.bastion.connection_drops:{count}|c|'
            f'#{base},reason:{reason},source:{source}|card:none'
        )
    return found


def main() -> int:
    url = os.environ.get('DD_DOGSTATSD_URL', '')
    cluster = os.environ.get('IDEA_CLUSTER_NAME', '')
    if not url.startswith('unix://') or not cluster:
        return 0
    path = url[len('unix://') :]
    sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sender.setblocking(False)
    tally = DropTally()
    while True:
        for line in lines(cluster, tally):
            try:
                sender.sendto(line.encode(), path)
            except OSError:
                pass  # the agent being away costs a sample, never the task
        time.sleep(INTERVAL_SECONDS)


if __name__ == '__main__':
    sys.exit(main())
