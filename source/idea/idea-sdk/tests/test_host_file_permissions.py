"""
Files a host shares between users: the job's scratch mount is world-writable with the sticky
bit, a shared file system keeps its own mode (gaining only the sticky bit when it is already
world-writable), and a rendered bootstrap package is private to the service that built it.
"""

import os
import re
import stat
import subprocess

import pytest

from test_bootstrap_shell_syntax import (
    BASH,
    JOB_PARAMS,
    WINDOWS_BASE_OS,
    job,
    packages,
    render,
)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def compute_node_package(tmp_path, name):
    os.makedirs(tmp_path, exist_ok=True)
    return render(
        tmp_path,
        'amazonlinux2023',
        'm7i.large',
        'compute-node',
        dict(
            job=job(**JOB_PARAMS[name]),
            project='sample-project',
            queue_profile='compute',
            job_directory='/apps/idea-mock/jobs/1',
        ),
    )


def permissions_block(tmp_path, name) -> str:
    """what compute_node_post_reboot.sh runs on the file systems once they are mounted"""
    files = compute_node_package(tmp_path / 'package', name)
    script = next(f for f in files if f.endswith('compute_node_post_reboot.sh'))
    with open(script) as f:
        content = f.read()
    assert 'chmod 777' not in content
    # the mounts: the shared storage check that ends in this stop (it runs mount -a)
    before, after = content.split('\nmount_shared_filesystems || BOOTSTRAP_DETAIL=', 1)
    after = after.split('\n', 1)[1]
    # the existing-FSx mount check (tested on its own) runs between the mounts and this block
    for check in ('# End: Existing FSx Check\n', '# End: New FSx Check\n'):
        if check in after:
            after = after.split(check, 1)[1]
    # nothing opens a path up before the mounts are in place (that would hit whatever the
    # path is at that moment), in any spelling of the mode
    assert not re.search(
        r'chmod\s+(a\+w|o\+w|[0-7]?[0-7]?7[0-7]?7\b|[0-7]*77\b)', before
    )
    lines = after.splitlines()
    block = []
    # never run past the block: what follows installs drivers
    for line in lines:
        if line.lstrip().startswith('# Begin: ') and 'Scratch Permissions' not in line:
            break
        block.append(line)
        if line.startswith('# End: Scratch Permissions'):
            break
    return '\n'.join(block)


@pytest.mark.skipif(BASH is None, reason='bash not installed')
@pytest.mark.parametrize(
    'name,lustre,mounted,provisioned,expected',
    [
        # the job's new FSx and the local scratch disk become shared-writable
        (
            'new fsx',
            ['/fsx', '/shared', '/owned'],
            [],
            'fs-new.fsx.example@tcp:/mnt1',
            {'/fsx': 0o1777},
        ),
        ('efa, ht, scratch', [], ['/scratch'], '/dev/nvme1n1', {'/scratch': 0o1777}),
        # an existing FSx is not the job's to open up
        ('existing fsx', ['/fsx', '/shared', '/owned'], [], None, {'/fsx': 0o755}),
        # no instance store: /scratch is not a mount and is left alone
        ('defaults', ['/shared'], [], None, {'/scratch': 0o755}),
        # a shared file system mounted at the scratch path, nothing provisioned for the job
        ('defaults', ['/shared'], ['/scratch'], None, {'/scratch': 0o755}),
        # something other than the provisioned device is mounted there
        ('efa, ht, scratch', [], ['/scratch'], '/dev/nvme9n1', {'/scratch': 0o755}),
    ],
)
def test_job_scratch_is_sticky_and_shared_roots_keep_their_mode(
    tmp_path, name, lustre, mounted, provisioned, expected
):
    root = tmp_path / 'root'
    for directory in ('/fsx', '/scratch', '/shared', '/owned'):
        (root / directory[1:]).mkdir(parents=True)
        os.chmod(root / directory[1:], 0o755)
    # a shared file system some earlier boot left world-writable
    os.chmod(root / 'shared', 0o777)
    block = permissions_block(tmp_path, name)
    assert 'chmod 1777' not in block.split('# Begin: Scratch Permissions', 1)[0]
    for directory in ('/fsx', '/scratch'):
        for quote in ('"', "'"):
            block = block.replace(
                f'{quote}{directory}{quote}', f'{quote}{root}{directory}{quote}'
            )
    bootstrap = tmp_path / 'bootstrap'
    bootstrap.mkdir()
    if provisioned:
        (bootstrap / 'job_scratch_source').write_text(provisioned + '\n')
    # the mount table lists Lustre by server address, the job's disk by device
    sources = {str(root) + m: '10.0.0.5@tcp:/mnt1' for m in lustre}
    sources.update({str(root) + m: '/dev/nvme1n1' for m in mounted})
    cases = ' '.join(f'{m}) echo {src} ;;' for m, src in sources.items())
    stubs = f"""
BOOTSTRAP_DIR={bootstrap}
mount() {{ for m in {' '.join(str(root) + m for m in lustre)}; do echo "fs@tcp:/x on $m type lustre (rw)"; done; }}
findmnt() {{ case "${{@: -1}}" in {cases} *) return 1 ;; esac; }}
mountpoint() {{ findmnt --mountpoint "$2" > /dev/null; }}
"""
    subprocess.run([BASH, '-c', stubs + block], check=True)
    expected = {
        '/shared': 0o1777 if '/shared' in lustre else 0o777,
        '/owned': 0o755,
        **expected,
    }
    assert {d: oct(mode(root / d[1:])) for d in expected} == {
        d: oct(m) for d, m in expected.items()
    }


@pytest.mark.parametrize('package', ['compute-node', 'windows desktop'])
def test_bootstrap_package_is_private_whatever_the_umask(tmp_path, package):
    package_dir = tmp_path / 'apps' / 'dcv-host-bootstrap' / 'owner' / 'session'
    previous = os.umask(0o022)
    try:
        os.makedirs(package_dir)
        if package == 'compute-node':
            compute_node_package(package_dir, 'defaults')
        else:
            # the Windows desktop package carries the local administrator password
            base_os = WINDOWS_BASE_OS[0]
            _, component, variables = packages(base_os)[0]
            render(package_dir, base_os, 'm7i.large', component, variables)
    finally:
        os.umask(previous)
    (archive,) = [f for f in os.listdir(package_dir) if f.endswith('.tar.gz')]
    assert oct(mode(package_dir)) == oct(0o700)
    assert oct(mode(package_dir / archive)) == oct(0o600)
