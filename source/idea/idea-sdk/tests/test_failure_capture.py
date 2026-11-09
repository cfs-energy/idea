"""What a failed host said: read, redacted, bounded, and never in the way of its disposal."""

import base64
import threading
import time
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

import boto3

from ideasdk.aws import failure_capture as module
from ideasdk.aws.failure_capture import (
    CAPTURE_MARKER,
    FailedHost,
    capture_failure,
    clean_for_log,
    detect_signature,
    plain_cause,
    redact,
)

HEALTHY_BOOT = """
[    0.000000] Command line: BOOT_IMAGE=/boot/vmlinuz root=UUID=1 ro panic=1 console=ttyS0
[    1.100000] dracut-pre-udev[201]: modprobe: FATAL: Module nothing not found
[    2.000000] systemd[1]: Starting dracut initqueue hook...
[    2.100000] systemd[1]: Finished dracut pre-mount hook.
[    3.000000] Freeing initrd memory: 34000K
Rocky Linux 9.4 (Blue Onyx)
"""


@pytest.mark.parametrize(
    'text, expected',
    [
        ('Kernel panic - not syncing: Fatal exception', 'kernel_panic'),
        (
            'Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)',
            'initramfs_missing',
        ),
        ('error: you need to load the kernel first', 'initramfs_missing'),
        ('tar: write error: No space left on device', 'no_space'),
        ('curl: (6) Could not resolve host: example.test', 'dns'),
        ('Temporary failure in name resolution', 'dns'),
        ('nfs: server fs-1.example not responding, still trying', 'nfs_hang'),
        ('dracut-initqueue[300]: Warning: dracut-initqueue timeout', 'dracut'),
        ('Warning: /dev/mapper/root does not exist', 'dracut'),
        (HEALTHY_BOOT, 'unknown'),
        ('', 'unknown'),
    ],
)
def test_signatures_name_what_went_wrong_and_not_a_healthy_boot(text, expected):
    assert detect_signature(text) == expected


def test_a_panic_outranks_an_earlier_failed_lookup():
    assert (
        detect_signature('Could not resolve host: x', 'Kernel panic - not syncing')
        == 'kernel_panic'
    )


def test_the_portal_sentence_comes_from_the_table_never_from_the_text():
    for signature in module.CAUSES:
        sentence = plain_cause(signature)
        assert '/' not in sentence and sentence.startswith('Likely cause: ')
    assert plain_cause('unknown') is None
    assert plain_cause(None) is None


@pytest.mark.parametrize(
    'secret_line',
    [
        'password=hunter2sss',
        'DB_PASSWORD: "hunter2sss"',
        'api_key = hunter2sss',
        'Authorization: Basic hunter2sss',
        'curl -H "bearer hunter2sss"',
        'scheme://admin:' + 'hunter2sss' + '@host/x',
        'join token hunter2sss',
    ],
)
def test_redaction_removes_the_secret(secret_line):
    assert 'hunter2sss' not in redact(secret_line)


def test_redaction_removes_keys_and_long_tokens():
    text = 'AKIAABCDEFGHIJKLMNOP ' + 'a1B2' * 15
    assert redact(text).split() == ['[redacted]', '[redacted]']
    pem = '-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----'
    assert redact(pem) == '[redacted]'


def test_package_lines_do_not_travel():
    lines = clean_for_log(
        '\n'.join(
            [
                '  Installing       : secret-solver-4.2-1.el9.x86_64      1/3',
                '  secret-solver.x86_64  4.2-1.el9  appstream',
                'Unpacking secret-solver (4.2) over (4.1) ...',
                'Setting up secret-solver (4.2) ...',
                'Get:1 http://archive.test jammy/main amd64 secret-solver',
                'Kernel panic - not syncing',
            ]
        ),
        1000,
    )
    assert lines == ['| Kernel panic - not syncing']


@pytest.mark.parametrize(
    'line,secret',
    [
        ('user alice: failed to access /home/alice/project', 'alice'),
        ('user alice: failed to access /home/alice/project', 'project'),
        ('export IDEA_SESSION_OWNER=alice', 'alice'),
        ('session_owner: alice', 'alice'),
        ('USER=alice', 'alice'),
        ('LOGNAME=alice', 'alice'),
        ('pam_unix(sshd:session): session opened for user alice by (uid=0)', 'alice'),
        ('Accepted publickey for alice from 10.0.0.1', 'alice'),
        ('uid=1001(alice) gid=1001(fusion)', 'alice'),
        ('uid=1001(alice) gid=1001(fusion)', 'fusion'),
        ('sudo -u alice whoami', 'alice'),
        ('su - alice -c hostname', 'alice'),
        ('chown alice:fusion file', 'alice'),
        ('mail alice@example.com bounced', 'alice'),
        ('ssh alice@10.0.0.1 failed', 'alice'),
        ('CORP\\alice denied', 'alice'),
        ('uid=alice,ou=people,dc=example', 'alice'),
        ('cannot open /data/home/alice/run.log', 'alice'),
        ('cannot open /projects/quench7/in.dat', 'quench7'),
        ('cannot open /fsx/quench7/in.dat', 'quench7'),
        ('cannot open /apps/quench7/bin', 'quench7'),
        ('cannot open /tmp/alice-run/x', 'alice'),
        ('cannot open /mnt/quench7/x', 'quench7'),
        ('cannot open /run/user/alice/x', 'alice'),
        ('cannot open /sysroot/home/alice/x', 'alice'),
        ('HOME=/home/alice', 'alice'),
        ('cannot open ~alice/x', 'alice'),
        ('cannot open C:\\Users\\alice\\x', 'alice'),
    ],
)
def test_no_username_or_personal_path_travels(line, secret):
    assert secret not in '\n'.join(clean_for_log(line, 1000))


def test_a_long_path_like_line_is_scrubbed_at_once():
    started = time.monotonic()
    clean_for_log('/a' * 5000 + ' ' + 'x\\' * 5000 + '\n' + 'a.' * 400, 100000)
    assert time.monotonic() - started < 1


@pytest.mark.parametrize(
    'text',
    [
        'a' * 64000,
        '.-' * 32000,
        'password' * 8000,
        'username' * 8000,
        'x' * 30000 + '\n' + 'token' * 6000,
        ('secret' + ' ' * 790 + '\n' + ' ' * 790 + ';\n') * 40,
    ],
)
def test_one_long_token_cannot_hold_the_interpreter(text, monkeypatch):
    # the patterns hold the GIL, so a slow one stops every thread and no join times it out
    started = time.monotonic()
    clean_for_log(text, 12000, 'alice')
    detect_signature(text, text)
    assert time.monotonic() - started < 2

    h = Harness(FakeEc2(text), tail=text)
    started = time.monotonic()
    h.run(monkeypatch)
    assert time.monotonic() - started < module.TIME_LIMIT_SECONDS
    assert h.metrics[0][1]['console'] == 'ok'


def test_a_line_cut_short_leaves_no_part_of_a_secret():
    token = 'Zq9' * 30
    for line in (
        'x ' * 380 + token,
        'x ' * 380 + 'scheme://admin:' + token + '@host',
        'x ' * 370 + 'password="first ' + token + ' last"',
        'join.' * 200 + token,
    ):
        shown = '\n'.join(clean_for_log('[redacted] ' * 30 + line, 100000))
        assert 'Zq9' not in shown and 'last' not in shown, line[-200:]
    assert redact('password="first second\nnext') == 'password=[redacted]\nnext'


def test_the_end_of_a_long_read_starts_at_a_line():
    text = 'k' * 39 + '\n' + 'line\n' * (module.READ_CHARS // 5)
    assert module.bounded(text + 'last').startswith('line\n')
    assert module.bounded('short') == 'short' and module.bounded(None) == ''
    events = [{'message': 'e' * 1000} for _ in range(200)]
    logs = Mock()
    logs.get_log_events.return_value = {'events': events}
    assert len(module._read_log_tail(logs, 'g', 's')) <= module.READ_CHARS


def test_a_last_line_longer_than_the_read_is_kept_from_its_start():
    # one log event with no newline: a cut anywhere in it lands inside whatever is there
    word = 'hunter2sss' + 'Zq9xK' * 3
    for inside in range(1, len(word), 3):
        # the last READ_CHARS of it start inside the word
        after = ' zz' * (module.READ_CHARS // 3)
        after = after[: module.READ_CHARS - len(word) + inside]
        line = 'first words ' + 'q ' * 900 + 'password=' + word + after
        for text in (line, 'earlier\n' + line, line + '\n'):
            kept = module.bounded(text)
            assert kept.startswith('first words ') and len(kept) <= module.READ_CHARS
            shown = '\n'.join(clean_for_log(text, 100000))
            assert shown.startswith('| first words ')
            for part in (word[:6], word[7:13], word[-8:]):
                assert part not in shown, (inside, shown[:80])
    logs = Mock()
    logs.get_log_events.return_value = {'events': [{'message': line}]}
    assert module._read_log_tail(logs, 'g', 's').startswith('first words ')
    assert detect_signature('No space left on device ' + 'z ' * 40000) == 'no_space'


def test_a_key_block_with_its_first_line_cut_away_does_not_travel():
    body = ['Mk9' * 21 + 'M'] * 30 + ['shortlastlineQ9x=']
    block = ['-----BEGIN RSA PRIVATE KEY-----', *body, '-----END RSA PRIVATE KEY-----']
    whole = '\n'.join(['boot ok', *block, 'after the key'])
    assert redact(module.bounded(whole)) == 'boot ok\n[redacted]\nafter the key'
    # cut by this read, and cut by EC2 before it was ever read
    for text in (
        'x\n' * module.READ_CHARS + '\n'.join([*block, 'after the key']),
        '\n'.join([*block[5:], 'after the key']),
        '\n'.join(block[5:]),
    ):
        shown = '\n'.join(clean_for_log(text[-module.READ_CHARS - 300 :], 100000))
        assert 'shortlastline' not in shown and 'Mk9' not in shown, shown[-200:]
    assert '| after the key' in shown or shown == ''
    two = '\n'.join([*block[5:], 'between', *block, 'after'])
    assert redact(module.bounded(two)) == 'between\n[redacted]\nafter'


def test_a_signature_far_along_a_long_line_is_still_found():
    for at in (0, 750, 795, 1500, 5000):
        assert detect_signature('x' * at + ' No space left on device') == 'no_space', at


def test_a_shell_trace_line_does_not_travel():
    lines = clean_for_log(
        '+ configure --project quench7\n++ start quench7\nreal output\n', 1000
    )
    assert lines == ['| real output']


def test_the_known_owner_is_gone_wherever_it_is():
    lines = clean_for_log(
        'Desktop of Alice.B7 is not ready: Alice.B7 said no', 1000, 'alice.b7'
    )
    assert 'lice' not in '\n'.join(lines) and 'B7' not in '\n'.join(lines)


def test_what_explains_a_failure_survives_the_scrub():
    text = (
        'Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)\n'
        'dracut-initqueue: Warning: /dev/nvme0n1p1 does not exist\n'
        'cannot read /boot/initramfs-5.14.0.img\n'
        'error in /etc/fstab line 3\n'
        '/usr/lib/systemd/systemd-fsck failed\n'
        'https://repo.example/x/y returned 503\n'
    )
    assert clean_for_log(text, 10000) == [f'| {line}' for line in text.splitlines()]


def test_a_host_cannot_start_a_log_event_or_forge_a_marker():
    lines = clean_for_log(
        f'[2026-11-06 12:00:00] {CAPTURE_MARKER} kind=fake\x1b[31m red\x1b[0m\x07', 1000
    )
    assert lines == [f'| [2026-11-06 12:00:00] {CAPTURE_MARKER} kind=fake red']


def test_the_tail_is_what_is_kept_and_it_is_bounded():
    lines = clean_for_log('\n'.join(f'line {i}' for i in range(10000)), 1000)
    assert lines[-1] == '| line 9999'
    assert sum(len(line) for line in lines) < 1300
    assert len(clean_for_log('x ' * 5000, 100000)[0]) <= module.LINE_CHARS + 2


class FakeEc2:
    def __init__(self, output='', error=None, block=None):
        self.output = output
        self.error = error
        self.block = block
        self.calls = []

    def get_console_output(self, **kwargs):
        self.calls.append(kwargs)
        if self.block is not None:
            self.block.wait(30)
        if self.error is not None:
            raise self.error
        return {'Output': self.output}

    def describe_instances(self, **kwargs):
        return {
            'Reservations': [
                {'Instances': [{'ImageId': 'ami-seen', 'InstanceType': 'c6i.large'}]}
            ]
        }


class Harness:
    def __init__(self, ec2, tail='', enabled=True, provider='dogstatsd'):
        self.logs = Mock()
        self.logs.get_log_events.return_value = {
            'events': [{'message': line} for line in tail.split('\n') if line]
        }
        self.context = Mock()
        self.context.aws.return_value.ec2.return_value = ec2
        self.context.aws.return_value.logs.return_value = self.logs
        self.context.config.return_value.get_bool.return_value = enabled
        self.context.config.return_value.get_string.return_value = provider
        self.logger = Mock()
        self.metrics = []

    def run(self, monkeypatch, **fields):
        return self.run_with(monkeypatch, **fields)

    def run_with(self, monkeypatch, failure='never reported back', **fields):
        monkeypatch.setattr(
            module.HealthMetrics,
            'add',
            lambda _, name, value=1, **tags: self.metrics.append((name, tags)),
        )
        return capture_failure(
            self.context,
            self.logger,
            FailedHost(
                kind='desktop',
                identifier='session-1',
                instance_id='i-1',
                failure=failure,
                os='amazonlinux2023',
                arch='x86_64',
                release='26.11.2',
                log_group='/c/vdc/dcv-host',
                log_stream='bootstrap_i-1',
                **fields,
            ),
        )

    def shipped(self):
        assert self.logger.warning.call_count == 1
        return self.logger.warning.call_args.args[0]


def test_a_panic_is_shipped_with_identity_counted_and_explained(monkeypatch):
    h = Harness(
        FakeEc2('Booting\nKernel panic - not syncing: Fatal exception\n'),
        tail='IDEA_BOOTSTRAP step 3\npassword=hunter2sss',
    )
    cause = h.run(monkeypatch)
    assert cause == (
        'Likely cause: the operating system crashed while starting (kernel panic).'
    )
    message = h.shipped()
    head = message.split('\n')[0]
    assert head.startswith(f'{CAPTURE_MARKER} kind=desktop id=session-1 instance=i-1')
    for field in (
        'os=amazonlinux2023',
        'arch=x86_64',
        'ami=ami-seen',
        'instance_type=c6i.large',
        'release=26.11.2',
        'reason=kernel_panic',
        'console=ok',
        'failure=never_reported_back',
    ):
        assert field in head
    assert '| Kernel panic - not syncing: Fatal exception' in message
    assert '| IDEA_BOOTSTRAP step 3' in message
    assert 'hunter2sss' not in message
    assert h.metrics == [
        (
            'failure.captures',
            dict(
                kind='desktop',
                reason='kernel_panic',
                console='ok',
                os='amazonlinux2023',
                arch='x86_64',
            ),
        )
    ]
    # the newest output, not only what ec2 buffered at the last state change
    assert h.context.aws().ec2().calls == [{'InstanceId': 'i-1', 'Latest': True}]


def test_the_failure_field_and_tail_carry_no_owner_or_home(monkeypatch):
    h = Harness(
        FakeEc2('failed for desktop of alice\n'),
        tail='+ IDEA_SESSION_OWNER=alice\nno such file /home/alice/.bashrc\n',
    )
    h.run_with(
        monkeypatch, failure='alice cannot reach /data/home/alice', owner='alice'
    )
    message = h.shipped()
    assert 'alice' not in message
    assert '| no such file [path]' in message


def test_every_line_of_a_shipped_capture_stays_one_python_log_event(monkeypatch):
    # the agent's multi_line rule starts an event on a line starting with a date in brackets
    h = Harness(FakeEc2('[2026-11-06 01:02:03] host said so\n'))
    h.run(monkeypatch)
    for line in h.shipped().split('\n'):
        assert not line.startswith('[20')


def denied(code='UnauthorizedOperation'):
    return ClientError({'Error': {'Code': code, 'Message': 'no'}}, 'GetConsoleOutput')


def test_no_grant_yet_finds_no_cause_and_says_so_in_the_count(monkeypatch):
    # a cluster between the image and the iam update of its stack
    h = Harness(FakeEc2(error=denied()))
    assert h.run(monkeypatch) is None
    assert h.metrics[0][1]['console'] == 'denied'
    assert h.metrics[0][1]['reason'] == 'unknown'
    assert 'console=denied' in h.shipped()


def test_an_instance_type_without_latest_falls_back_to_the_buffered_console(
    monkeypatch,
):
    class Ec2(FakeEc2):
        def get_console_output(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get('Latest'):
                raise denied('UnsupportedOperation')
            return {'Output': 'No space left on device'}

    h = Harness(Ec2())
    assert 'ran out of space' in h.run(monkeypatch)
    assert h.context.aws().ec2().calls[1] == {'InstanceId': 'i-1'}


def test_a_log_stream_that_was_never_created_costs_nothing(monkeypatch):
    h = Harness(FakeEc2('Kernel panic - not syncing'))
    h.logs.get_log_events.side_effect = RuntimeError('no stream')
    assert 'kernel panic' in h.run(monkeypatch)


def test_a_hung_read_returns_within_the_limit_and_is_counted(monkeypatch):
    block = threading.Event()
    h = Harness(FakeEc2('Kernel panic - not syncing', block=block))
    monkeypatch.setattr(module, 'TIME_LIMIT_SECONDS', 0.05)
    started = time.monotonic()
    assert h.run(monkeypatch) is None
    assert time.monotonic() - started < 2
    assert h.metrics[0][1]['console'] == 'timeout'
    block.set()


@pytest.mark.parametrize(
    'break_it',
    [
        lambda ctx: setattr(ctx.aws, 'side_effect', RuntimeError('boom')),
        lambda ctx: setattr(ctx.config, 'side_effect', RuntimeError('boom')),
    ],
)
def test_nothing_that_goes_wrong_can_raise(monkeypatch, break_it):
    h = Harness(FakeEc2())
    break_it(h.context)
    assert h.run(monkeypatch) is None


def test_the_off_switch_reads_nothing(monkeypatch):
    ec2 = FakeEc2('Kernel panic - not syncing')
    h = Harness(ec2, enabled=False)
    assert h.run(monkeypatch) is None
    assert ec2.calls == [] and h.metrics == []
    h.logger.warning.assert_not_called()


def test_no_instance_reads_nothing():
    ec2 = FakeEc2('Kernel panic - not syncing')
    h = Harness(ec2)
    assert (
        capture_failure(
            h.context,
            h.logger,
            FailedHost(kind='desktop', identifier='x', instance_id=None),
        )
        is None
    )
    assert ec2.calls == []


def test_the_real_client_decodes_the_console_and_sends_latest():
    client = boto3.client(
        'ec2', region_name='us-east-2', aws_access_key_id='x', aws_secret_access_key='y'
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            'get_console_output',
            {
                'InstanceId': 'i-1',
                'Output': base64.b64encode(b'Kernel panic - not syncing').decode(),
            },
            {'InstanceId': 'i-1', 'Latest': True},
        )
        text, state = module._read_console(client, 'i-1')
    assert state == 'ok' and 'Kernel panic' in text


def test_a_bake_stopped_by_its_time_limit_is_counted_as_one_when_the_host_says_nothing(
    monkeypatch,
):
    from datetime import timedelta

    from ideadatamodel import ImageBuildRecord, ImageKind
    from ideasdk.aws.image_builds import bake_time_limit_reason, capture_builder_failure

    def captured(console):
        h = Harness(FakeEc2(console))
        monkeypatch.setattr(
            module.HealthMetrics,
            'add',
            lambda _, name, value=1, **tags: h.metrics.append((name, tags)),
        )
        record = ImageBuildRecord(
            base_os='rocky8',
            architecture='arm64',
            status='building',
            instance_id='i-1',
            release='26.11.2',
        )
        reason = bake_time_limit_reason(record, timedelta(minutes=3))
        cause = capture_builder_failure(
            h.context, h.logger, ImageKind.DESKTOP, record, reason
        )
        return h, cause

    h, cause = captured(HEALTHY_BOOT)
    assert h.metrics[0][1]['reason'] == 'time_limit'
    assert 'reason=time_limit' in h.shipped()
    # the row's error already says so; the portal sentence is only for what the host said
    assert cause is None
    # what the host did say outranks it
    h, cause = captured('Kernel panic - not syncing: Fatal exception')
    assert h.metrics[0][1]['reason'] == 'kernel_panic'


def test_a_hung_read_still_counts_the_callers_reason(monkeypatch):
    block = threading.Event()
    monkeypatch.setattr(module, 'TIME_LIMIT_SECONDS', 0.1)
    h = Harness(FakeEc2(block=block))
    h.run_with(monkeypatch, fallback_reason='time_limit')
    block.set()
    assert h.metrics[0][1]['reason'] == 'time_limit'
    assert h.metrics[0][1]['console'] == 'timeout'
