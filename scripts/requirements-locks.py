"""Compile with the same relative paths in development and in isolated CI checks."""

import argparse
import difflib
import pathlib
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--name')
    parser.add_argument('--upgrade', action='store_true')
    parser.add_argument('--package')
    args = parser.parse_args()
    if args.check:
        check_locks(ROOT)
    else:
        compile_locks(ROOT, args.name, args.upgrade, args.package)
