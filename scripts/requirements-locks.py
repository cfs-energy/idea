"""Compile with the same relative paths in development and in isolated CI checks."""

import argparse
import difflib
import os
import pathlib
import platform
import sys
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def compile_locks(root, name=None, upgrade=False, package=None):
    sources = sorted((root / 'requirements').glob('*.in'))
    if name:
        sources = [root / 'requirements' / (pathlib.Path(name).stem + '.in')]
    for source in sources:
        command = [
            'pip-compile',
            '--strip-extras',
            '--no-emit-index-url',
            '--output-file=' + source.with_suffix('.txt').name,
        ]
        source_path = source.name
        if upgrade:
            command.append('--upgrade')
        if package:
            command.extend(['--upgrade-package', package])
        command.append(source_path)
        subprocess.run(command, cwd=root / 'requirements', check=True)


def check_locks(root):
    with tempfile.TemporaryDirectory(prefix='requirements-check-') as directory:
        temporary = pathlib.Path(directory)
        shutil.copytree(root / 'requirements', temporary / 'requirements')
        compile_locks(temporary)
        differences = []
        for generated in sorted((temporary / 'requirements').glob('*.txt')):
            tracked = root / 'requirements' / generated.name
            before = tracked.read_text() if tracked.exists() else ''
            after = generated.read_text()
            if before != after:
                differences.extend(
                    difflib.unified_diff(
                        before.splitlines(True),
                        after.splitlines(True),
                        fromfile=str(tracked.relative_to(root)),
                        tofile='recompiled/' + generated.name,
                    )
                )
        for tracked in (root / 'requirements').glob('*.txt'):
            if not tracked.with_suffix('.in').exists():
                differences.append(f'Orphan lock: {tracked.name}\n')
        if differences:
            raise SystemExit('Requirements locks differ:\n' + ''.join(differences))


def relaunch_on_linux(force=False):
    """pip-compile resolves platform markers for the machine it runs on (sqlalchemy pulls
    greenlet on Linux only), so the tracked locks are Linux locks, as CI and Renovate make them."""
    if os.environ.get('REQUIREMENTS_LOCKS_NATIVE') or (
        platform.system() == 'Linux' and not force
    ):
        return
    # click's flag handling changed the header text between versions, so both are pinned.
    pins = ' '.join(
        line.strip()
        for line in (ROOT / 'requirements' / 'dev.txt').read_text().splitlines()
        if line.startswith(('pip-tools==', 'click=='))
    )
    version = next(
        line.split(':', 1)[1].strip()
        for line in (ROOT / 'software_versions.yml').read_text().splitlines()
        if line.startswith('python_version:')
    )
    command = [
        'docker',
        'run',
        '--rm',
        '-v',
        f'{ROOT}:/work',
        '-w',
        '/work',
        '-e',
        'REQUIREMENTS_LOCKS_NATIVE=1',
        f'python:{version}-slim',
        'sh',
        '-c',
        f'pip install -q {pins} && python scripts/requirements-locks.py "$@"',
        'sh',
        *sys.argv[1:],
    ]
    raise SystemExit(subprocess.run(command).returncode)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--container', action='store_true')
    parser.add_argument('--name')
    parser.add_argument('--upgrade', action='store_true')
    parser.add_argument('--package')
    args = parser.parse_args()
    relaunch_on_linux(force=args.container)
    if args.check:
        check_locks(ROOT)
    else:
        compile_locks(ROOT, args.name, args.upgrade, args.package)
