"""Keep file classification and small text checks consistent across hk steps."""

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib


def select_files(files, types):
    # The CLI's resolved path belongs to mise's isolated identify environment.
    # Use its interpreter rather than adding hook libraries to the project venv.
    try:
        from identify.identify import tags_from_path
    except ImportError:
        interpreter = Path(shutil.which('identify-cli')).resolve().with_name('python')
        os.execv(interpreter, [str(interpreter), __file__, *sys.argv[1:]])
    return [
        name
        for name in files
        if Path(name).is_file()
        and not Path(name).is_symlink()
        and (not types or set(types) & tags_from_path(name))
    ]


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate key: {key}')
        result[key] = value
    return result


def format_bytes(kind, content):
    if kind == 'trailing-whitespace':
        result = []
        for line in io.BytesIO(content):
            ending = (
                b'\r\n'
                if line.endswith(b'\r\n')
                else b'\n'
                if line.endswith(b'\n')
                else b''
            )
            result.append((line[: -len(ending)] if ending else line).rstrip() + ending)
        return b''.join(result)
    if kind == 'end-of-file-fixer':
        body = content.rstrip(b'\r\n')
        if not body:
            return b''
        tail = content[len(body) :]
        ending = next((e for e in (b'\n', b'\r\n', b'\r') if tail.startswith(e)), b'\n')
        return body + ending
    if kind == 'fix-byte-order-marker':
        return content.removeprefix(b'\xef\xbb\xbf')
    if kind == 'remove-crlf':
        return content.replace(b'\r\n', b'\n')
    if kind == 'remove-tabs':
        return content.expandtabs(4)
    if kind == 'pretty-format-json':
        normalized = content.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        formatted = json.dumps(json.loads(normalized), indent=2) + '\n'
        return content if normalized == formatted else formatted.encode()
    raise ValueError(f'Unknown text check: {kind}')


def git_output(*args, **kwargs):
    return subprocess.check_output(['git', *args], **kwargs)


def check_files(kind, files, fix=False):
    if kind == 'check-added-large-files':
        added = set(
            git_output('diff', '--cached', '--name-only', '--diff-filter=A', '-z')
            .decode()
            .split('\0')
        )
        files = [name for name in files if name in added]
        if files:
            attrs = (
                git_output(
                    'check-attr',
                    'filter',
                    '-z',
                    '--stdin',
                    input='\0'.join(files).encode(),
                )
                .decode()
                .split('\0')
            )
            lfs = {
                attrs[i] for i in range(0, len(attrs) - 1, 3) if attrs[i + 2] == 'lfs'
            }
            files = [name for name in files if name not in lfs]
    if kind == 'check-merge-conflict':
        git_dir = Path(git_output('rev-parse', '--git-dir').decode().strip())
        if not (
            (git_dir / 'MERGE_MSG').exists()
            and any(
                (git_dir / name).exists()
                for name in ('MERGE_HEAD', 'rebase-apply', 'rebase-merge')
            )
        ):
            return 0
    failed = False
    for name in files:
        path = Path(name)
        content = path.read_bytes()
        try:
            if kind == 'check-json':
                json.loads(content, object_pairs_hook=unique_keys)
                bad = False
            elif kind == 'check-toml':
                tomllib.loads(content.decode())
                bad = False
            elif kind == 'check-added-large-files':
                bad = len(content) > 500 * 1024
            elif kind == 'check-merge-conflict':
                patterns = (
                    b'<<<<<<< ',
                    b'======= ',
                    b'=======\r\n',
                    b'=======\n',
                    b'>>>>>>> ',
                )
                bad = any(line.startswith(patterns) for line in io.BytesIO(content))
            elif kind == 'forbid-crlf':
                bad = b'\r\n' in content
            elif kind == 'forbid-tabs':
                bad = b'\t' in content
            else:
                formatted = format_bytes(kind, content)
                bad = formatted != content
                if bad and fix:
                    path.write_bytes(formatted)
            if bad:
                print(f'{name}: {kind}')
                failed = True
        except (ValueError, UnicodeError) as error:
            print(f'{name}: {error}')
            failed = True
    return int(failed)


def run_command(command, files, copy_check=False):
    if not copy_check:
        return subprocess.call([*command, *files])
    # texthooks has no read-only mode. Check disposable copies, preserving names.
    with tempfile.TemporaryDirectory(prefix='hk-text-') as directory:
        copies = []
        for name in files:
            target = Path(directory) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(name, target)
            copies.append(str(target))
        result = subprocess.run([*command, *copies], capture_output=True, text=True)
        print(result.stdout.replace(directory + '/', ''), end='')
        print(result.stderr.replace(directory + '/', ''), end='', file=sys.stderr)
        return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--type', action='append', default=[])
    parser.add_argument('--check')
    parser.add_argument('--fix', action='store_true')
    parser.add_argument('--copy-check', action='store_true')
    parser.add_argument('files', nargs='*')
    arguments = sys.argv[1:]
    command = []
    if '--' in arguments:
        split = arguments.index('--')
        command = arguments[split + 1 :]
        arguments = arguments[:split]
    args = parser.parse_args(arguments)
    files = select_files(args.files, args.type)
    if not files:
        return 0
    return (
        check_files(args.check, files, args.fix)
        if args.check
        else run_command(command, files, args.copy_check)
    )


if __name__ == '__main__':
    raise SystemExit(main())
