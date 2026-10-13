"""
A user's .ppk download on a host without puttygen is written in process, in the layout
puttygen produced, so PuTTY, WinSCP and MobaXterm load it.
"""

import base64
import hashlib
import hmac
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from ideaclustermanager.app.accounts import user_home_directory
from ideaclustermanager.app.accounts.ppk import MAC_KEY, to_ppk
from ideaclustermanager.app.accounts.user_home_directory import UserHomeDirectory


def _read(blob: bytes):
    """the SSH strings and mpints in a blob, in order"""
    values, offset = [], 0
    while offset < len(blob):
        (length,) = struct.unpack('>I', blob[offset : offset + 4])
        values.append(blob[offset + 4 : offset + 4 + length])
        offset += 4 + length
    assert offset == len(blob)
    return values


def _parse(ppk: str):
    """the header fields and the two blobs, read back the way PuTTY does"""
    lines = ppk.splitlines()
    assert ppk.endswith('\n') and len(lines) >= 6
    algorithm = lines[0].split('PuTTY-User-Key-File-2: ', 1)[1]
    encryption = lines[1].split('Encryption: ', 1)[1]
    comment = lines[2].split('Comment: ', 1)[1]
    public_count = int(lines[3].split('Public-Lines: ', 1)[1])
    public = base64.b64decode(''.join(lines[4 : 4 + public_count]))
    private_count = int(lines[4 + public_count].split('Private-Lines: ', 1)[1])
    private = base64.b64decode(
        ''.join(lines[5 + public_count : 5 + public_count + private_count])
    )
    mac = lines[5 + public_count + private_count].split('Private-MAC: ', 1)[1]
    assert len(lines) == 6 + public_count + private_count
    assert all(len(line) <= 64 for line in lines[4:-1])
    expected = hmac.new(
        MAC_KEY,
        b''.join(
            struct.pack('>I', len(part)) + part
            for part in (
                algorithm.encode(),
                encryption.encode(),
                comment.encode(),
                public,
                private,
            )
        ),
        hashlib.sha1,
    ).hexdigest()
    assert mac == expected, 'MAC does not verify'
    return algorithm, encryption, comment, public, private


def _openssh_public(key) -> bytes:
    return base64.b64decode(
        key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        .split()[1]
    )


@pytest.mark.parametrize('private_format', ['TraditionalOpenSSL', 'PKCS8', 'OpenSSH'])
def test_an_rsa_key_round_trips_with_a_verified_mac(private_format):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    data = key.private_bytes(
        serialization.Encoding.PEM,
        getattr(serialization.PrivateFormat, private_format),
        serialization.NoEncryption(),
    )
    algorithm, encryption, comment, public, private = _parse(to_ppk(data))
    assert (algorithm, encryption, comment) == (
        'ssh-rsa',
        'none',
        'imported-openssh-key',
    )
    assert public == _openssh_public(key)
    d, p, q, iqmp = (int.from_bytes(v, 'big') for v in _read(private))
    numbers = key.private_numbers()
    assert (
        d == numbers.d
        and {p, q} == {numbers.p, numbers.q}
        and p > q
        and (q * iqmp) % p == 1
    )


def test_an_ed25519_key_round_trips_with_a_verified_mac():
    key = ed25519.Ed25519PrivateKey.generate()
    data = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    algorithm, _, _, public, private = _parse(to_ppk(data, comment='someone@idea'))
    assert algorithm == 'ssh-ed25519'
    assert public == _openssh_public(key)
    assert _read(private) == [
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ]


def test_primes_the_other_way_round_are_normalised_for_putty():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = key.private_numbers()
    swapped = rsa.RSAPrivateNumbers(
        p=n.q,
        q=n.p,
        d=n.d,
        dmp1=n.dmq1,
        dmq1=n.dmp1,
        iqmp=pow(n.p, -1, n.q),
        public_numbers=n.public_numbers,
    ).private_key()
    data = swapped.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _, _, _, _, private = _parse(to_ppk(data))
    d, p, q, iqmp = (int.from_bytes(v, 'big') for v in _read(private))
    assert p > q and (q * iqmp) % p == 1


def test_other_key_types_are_refused():
    data = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    with pytest.raises(ValueError):
        to_ppk(data)


def _home(puttygen):
    import os

    home = UserHomeDirectory.__new__(UserHomeDirectory)
    home.user = SimpleNamespace(uid=os.getuid(), gid=os.getgid())
    home._shell = Mock()
    home._shell.invoke.return_value = SimpleNamespace(
        returncode=0 if puttygen else 1, stdout=f'{puttygen}\n' if puttygen else ''
    )
    return home


def test_puttygen_is_used_where_present_and_the_key_written_in_process_elsewhere(
    tmp_path, monkeypatch
):
    key_file = tmp_path / 'id_rsa'
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    ppk_file = tmp_path / 'id_rsa.ppk'
    runs = []

    def run(args, **_):
        runs.append(args)
        with open(args[3], 'w') as f:
            f.write('from puttygen\n')
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr(user_home_directory.subprocess, 'run', run)
    # the container: no puttygen on PATH
    assert _home(None).write_ppk(str(key_file), str(ppk_file)) == 'in-process'
    assert runs == []
    assert _parse(ppk_file.read_text())[3] == _openssh_public(key)
    # a host: puttygen does the work, as before
    puttygen = tmp_path / 'puttygen'
    puttygen.write_text('#!/bin/sh\n')
    puttygen.chmod(0o755)
    assert (
        _home(puttygen).write_ppk(str(key_file), str(tmp_path / 'host.ppk'))
        == 'puttygen'
    )
    # puttygen converts a private copy; the result is written into the user's directory
    assert len(runs) == 1 and runs[0][0] == str(puttygen) and runs[0][2] == '-o'
    assert not runs[0][1].startswith(str(tmp_path))
    assert (tmp_path / 'host.ppk').read_text() == 'from puttygen\n'
