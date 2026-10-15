"""Unpack an approved capture bundle without letting it overwrite executable code."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import sys
import tarfile


ALLOWED = ('tools/parity/live/', 'tools/parity/fixtures/', 'docs/port/')


def unpack(archive, expected_sha256, destination):
    """Validate every member before writing any file; accept regular data only."""
    archive = Path(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError('capture checksum mismatch')
    destination = Path(destination).resolve()
    with tarfile.open(archive, 'r:gz') as bundle:
        members = bundle.getmembers()
        seen = set()
        total = 0
        for member in members:
            path = PurePosixPath(member.name)
            name = path.as_posix()
            if path.is_absolute() or '..' in path.parts or '\\' in name:
                raise ValueError('invalid capture path')
            if not any((name + '/').startswith(prefix) for prefix in ALLOWED):
                # tar may include the parent directory entries, but never their files.
                if member.isdir() and name in ('tools', 'tools/parity', 'docs'):
                    continue
                raise ValueError('capture outside allowed directories')
            if not (member.isfile() or member.isdir()) or name in seen:
                raise ValueError(
                    'capture links, special files and duplicates are forbidden'
                )
            seen.add(name)
            target = destination / path
            if any(part.is_symlink() for part in (target, *target.parents)):
                raise ValueError('capture path traverses a symlink')
            if not target.resolve().is_relative_to(destination):
                raise ValueError('capture path traverses a symlink')
            if target.exists() and not (member.isdir() and target.is_dir()):
                raise ValueError('capture would overwrite an existing file')
            total += member.size
            if total > 1024 * 1024 * 1024:
                raise ValueError('capture bundle exceeds 1 GiB')
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with bundle.extractfile(member) as source:
                    with target.open('xb') as output:
                        os.chmod(target, 0o600)
                        output.write(source.read())


if __name__ == '__main__':
    os.umask(0o077)
    unpack(sys.argv[1], sys.argv[2], Path.cwd())
