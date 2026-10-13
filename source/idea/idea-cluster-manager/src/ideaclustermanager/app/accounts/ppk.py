"""
PuTTY private key files (.ppk) written in process, for a host without puttygen.

The layout is PuTTY's version 2 as puttygen -o writes it for a key without a passphrase:
every PuTTY, WinSCP and MobaXterm loads it. The control-plane image is Amazon Linux 2023,
which ships no putty package, so the container takes this path; a host with puttygen on
its PATH keeps using it.
"""

import base64
import hashlib
import hmac
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

# the v2 MAC key for a key without a passphrase
MAC_KEY = hashlib.sha1(b'putty-private-key-file-mac-key').digest()
DEFAULT_COMMENT = 'imported-openssh-key'


def _mpint(value: int) -> bytes:
    data = value.to_bytes((value.bit_length() + 8) // 8, 'big')
    return struct.pack('>I', len(data)) + data


def _string(data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + data


def _lines(blob: bytes) -> list:
    text = base64.b64encode(blob).decode('ascii')
    return [text[i : i + 64] for i in range(0, len(text), 64)]


def load_private_key(data: bytes):
    """a PEM (PKCS#1 or PKCS#8) or OpenSSH private key, unencrypted"""
    try:
        return serialization.load_pem_private_key(data, password=None)
    except ValueError:
        return serialization.load_ssh_private_key(data, password=None)


def _blobs(key):
    """(algorithm, public blob, private blob) in PuTTY's wire layout"""
    if isinstance(key, rsa.RSAPrivateKey):
        numbers = key.private_numbers()
        public = numbers.public_numbers
        p, q = numbers.p, numbers.q
        if p < q:
            # PuTTY keeps the larger prime first and iqmp = q^-1 mod p
            p, q = q, p
        iqmp = pow(q, -1, p)
        return (
            b'ssh-rsa',
            _string(b'ssh-rsa') + _mpint(public.e) + _mpint(public.n),
            _mpint(numbers.d) + _mpint(p) + _mpint(q) + _mpint(iqmp),
        )
    if isinstance(key, ed25519.Ed25519PrivateKey):
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        private = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return b'ssh-ed25519', _string(b'ssh-ed25519') + _string(public), _string(private)
    raise ValueError(f'{type(key).__name__} keys cannot be written as .ppk')


def to_ppk(private_key: bytes, comment: str = DEFAULT_COMMENT) -> str:
    algorithm, public_blob, private_blob = _blobs(load_private_key(private_key))
    mac_data = b''.join(
        _string(part)
        for part in (algorithm, b'none', comment.encode(), public_blob, private_blob)
    )
    mac = hmac.new(MAC_KEY, mac_data, hashlib.sha1).hexdigest()
    public_lines = _lines(public_blob)
    private_lines = _lines(private_blob)
    return (
        '\n'.join(
            [
                f'PuTTY-User-Key-File-2: {algorithm.decode()}',
                'Encryption: none',
                f'Comment: {comment}',
                f'Public-Lines: {len(public_lines)}',
                *public_lines,
                f'Private-Lines: {len(private_lines)}',
                *private_lines,
                f'Private-MAC: {mac}',
            ]
        )
        + '\n'
    )
