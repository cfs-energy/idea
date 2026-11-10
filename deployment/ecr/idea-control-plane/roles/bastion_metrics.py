"""
Bastion sessions, health and dropped connections, for each task, as DogStatsD. A separate process
with only the standard library, so that nothing here can stop sshd or slow its log path: the
role restarts it if it exits and sshd never waits for it. It counts; it keeps no username or
address, and reads only process titles, the kernel's socket table and the drop tally the role
writes (each line holds just the phrase that matched).
"""

import os
import re
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
# what sshd sends a connection it drops, under MaxStartups or a penalty (loopback has none)
# a login name may hold an @ itself, and sshd shows it in its monitor's and pre-auth titles;
# a connection with several terminals (a shared ControlMaster) lists them: user@pts/0,pts/1
SESSION_TITLE = re.compile(rb'(sshd|sshd-session): .*@(pts/\d+(,pts/\d+)*|notty)$')
REFUSED = b'Not allowed at this time'
# the line the task's own checks add to the tally when sshd refuses them (bastion_health.sh)
LOOPBACK_REFUSED = b'loopback check refused'


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
        if SESSION_TITLE.match(title):
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


def sshd_check(address=('127.0.0.1', 22), drops_file: str = DROPS_FILE):
    """whether sshd offers its banner, and how fast; reads the banner and closes, as the task's
    health check does, leaving nothing unread that would make the close a reset sshd logs.
    A refusal is not up, since a user would be refused too, and is noted as a loopback drop."""
    started = time.monotonic()
    try:
        with socket.create_connection(address, CHECK_TIMEOUT_SECONDS) as connection:
            connection.settimeout(CHECK_TIMEOUT_SECONDS)
            banner = connection.recv(256)
    except OSError:
        return False, None
    if banner.startswith(REFUSED):
        try:
            with open(drops_file, 'ab') as drops:
                drops.write(LOOPBACK_REFUSED + b'\n')
        except OSError:
            pass
    return banner.startswith(b'SSH-'), time.monotonic() - started


class DropTally:
    """
    Drops by reason and source. sshd names only some MaxStartups drops and gives the rest as a
    count, so the count is what is added up: 9.9 reports the episode so far ("in|exited
    MaxStartups throttling ..., N connections dropped", with the next named drop already in N),
    10.5 the drops it did not name ("additional N connections dropped"). loopback is the task's
    own checks, which note each refusal themselves; sshd's own loopback lines are left out, as
    they would count a check twice. Every other drop is remote: on 9.9 the episode less what was
    named or noted, on 10.5 the unnamed drops less the checks' refusals sshd did not name, worked
    out over everything read so far, as sshd names the drop that ends its rate limit before the
    check that drop refused notes it.
    Remote drops are counted when sshd reports them, which is late: 9.9 names the first drop of an
    episode and gives the total when it ends (its "in" line needs five minutes without a drop
    inside an episode, so a flood never logs one), 10.5 every five minutes and when its log rate
    limit ends. So remote is exact over an episode, reads 1 while a flood on 9.9 lasts, and a
    flood that never ends is never counted past that; loopback is counted as it happens.
    ponytail: a loopback client other than the checks (a user's `ssh localhost`) is counted as
    remote on 9.9 and not at all on 10.5 when sshd names it; name the checks' port if that matters.
    A read that falls between sshd naming a check's drop and the check noting it, in a batch with
    a 10.5 summary, counts that drop as remote once.
    """

    def __init__(self, path: str = DROPS_FILE):
        self.path = path
        self.offset = 0
        self.episode = 0
        self.counted_ahead = False
        # 10.5: checks' refusals sshd did not name, unnamed drops, and how many were remote
        self.loopback_unnamed = 0
        self.unnamed = 0
        self.unnamed_remote = 0

    def read(self):
        counts = {}

        def add(key, count=1):
            if count > 0:
                counts[key] = counts.get(key, 0) + count

        try:
            with open(self.path, 'rb') as handle:
                handle.seek(self.offset)
                data = handle.read()
                self.offset += len(data)
        except OSError:
            return counts
        for line in data.splitlines():
            if line.startswith(LOOPBACK_REFUSED):
                add(('maxstartups', 'loopback'))
                self.episode += 1
                self.loopback_unnamed += 1
            elif line.endswith(b' connections dropped'):
                fields = line.split()
                try:
                    dropped = int(fields[-3])
                except (IndexError, ValueError):
                    continue
                if b' additional ' in line:
                    self.unnamed += dropped
                    continue
                add(('maxstartups', 'remote'), dropped - self.episode)
                self.episode = max(dropped, self.episode)
                if line.startswith(b'exited '):
                    self.episode = 0
                else:
                    self.counted_ahead = True
            elif b'Maxstartups' in line:
                if self.counted_ahead:
                    self.counted_ahead = False
                elif b'127.0.0.1' in line:
                    self.loopback_unnamed -= 1
                else:
                    add(('maxstartups', 'remote'))
                    self.episode += 1
            elif b'before authentication' in line:
                add(('auth_timeout', 'remote'))
            elif b'client not responding' in line:
                add(('client_timeout', 'remote'))
        remote = self.unnamed - self.loopback_unnamed
        if self.unnamed and remote > self.unnamed_remote:
            add(('maxstartups', 'remote'), remote - self.unnamed_remote)
            self.unnamed_remote = remote
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
