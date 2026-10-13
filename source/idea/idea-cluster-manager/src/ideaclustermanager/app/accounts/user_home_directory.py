#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

from ideasdk.context import SocaContext
from ideadatamodel.auth import User, RepairHomeOwnershipResult
from ideasdk.utils import Utils
from ideadatamodel import exceptions

import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend as crypto_default_backend

from ideaclustermanager.app.accounts.auth_constants import USER_HOME_DIR_BASE
from ideaclustermanager.app.accounts.ppk import to_ppk
from ideasdk.shell import ShellInvoker


# a directory opened this way is never a symbolic link; files are then opened relative to it
_DIRECTORY_NOFOLLOW = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UserHomeDirectory:
    def __init__(self, context: SocaContext, user: User):
        self._context = context
        self._logger = context.logger('user-home-init')
        self.user = user
        self._shell = ShellInvoker(self._logger)

    @property
    def home_dir(self) -> str:
        return self.user.home_dir

    @property
    def ssh_dir(self) -> str:
        return os.path.join(self.user.home_dir, '.ssh')

    def write_ppk(self, private_key_file: str, ppk_file: str) -> str:
        """
        the user's key as a PuTTY file: puttygen where the host has it, as before, and in
        process where it does not (the container image carries none). The server runs as
        root, so the key is read and the result written without following links in the
        user's directory; puttygen works in a private temporary directory.
        :returns: 'puttygen' or 'in-process'
        """
        private_key = self._read_user_file(private_key_file)
        result = self._shell.invoke('command -v puttygen', shell=True)
        if result.returncode == 0:
            putty_gen_bin = result.stdout.strip()
            if not os.access(putty_gen_bin, os.X_OK):
                os.chmod(putty_gen_bin, 0o700)
            with tempfile.TemporaryDirectory(prefix='idea-ppk-') as work:
                key_file = os.path.join(work, 'id_rsa')
                with open(
                    os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb'
                ) as f:
                    f.write(private_key)
                out_file = os.path.join(work, 'id_rsa.ppk')
                result = subprocess.run(
                    [putty_gen_bin, key_file, '-o', out_file],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    raise exceptions.general_exception(
                        f'failed to generate .ppk file: {result.stderr.strip()}'
                    )
                with open(out_file, 'rb') as f:
                    ppk = f.read()
            self._write_user_file(ppk_file, ppk, 0o600)
            return 'puttygen'
        try:
            ppk = to_ppk(private_key)
        except ValueError as e:
            raise exceptions.general_exception(f'failed to generate .ppk file: {e}')
        self._write_user_file(ppk_file, ppk.encode(), 0o600)
        return 'in-process'

    def _read_user_file(self, path: str) -> bytes:
        """
        a file in the user's directory, read as root: never through a link the user put
        there, and only a regular file (a FIFO would hang the server)
        """
        dir_fd = os.open(os.path.dirname(path), _DIRECTORY_NOFOLLOW)
        try:
            fd = os.open(
                os.path.basename(path),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=dir_fd,
            )
        except OSError as e:
            raise exceptions.general_exception(f'{path} cannot be read: {e.strerror}')
        finally:
            os.close(dir_fd)
        with os.fdopen(fd, 'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise exceptions.general_exception(f'{path} is not a regular file')
            return f.read()

    def _write_user_file(self, path: str, data: bytes, mode: int):
        """
        writes a file into the user's directory as root and gives it to the user. The data
        goes to a new file created exclusively, never through a link, and is handed over by
        descriptor; the finished file is renamed over the old one, which a rename does not
        follow. An existing entry that is not a regular file (a link the user planted) is
        refused rather than replaced.
        """
        dir_fd = os.open(os.path.dirname(path), _DIRECTORY_NOFOLLOW)
        try:
            name = os.path.basename(path)
            try:
                existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise exceptions.general_exception(
                    f'{path} is not a regular file; not written'
                )
            temporary = f'.{name}.{secrets.token_hex(8)}.tmp'
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                os.fchmod(fd, mode)
                os.fchown(fd, self.user.uid, self.user.gid)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                os.unlink(temporary, dir_fd=dir_fd)
                raise
            os.close(fd)
            os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        finally:
            os.close(dir_fd)

    def own_path(self, path: str):
        # Account IDs are authoritative and work without NSS/SSSD in containers.
        os.chown(path, self.user.uid, self.user.gid)

    def validate_ownership(self):
        if (
            self.user.uid is None
            or self.user.gid is None
            or self.user.uid <= 0
            or self.user.gid <= 0
        ):
            raise exceptions.invalid_params(
                'Home directory requires positive uid and gid'
            )
        if (
            not self.home_dir
            or not os.path.isabs(self.home_dir)
            or os.path.normpath(self.home_dir) == '/'
        ):
            raise exceptions.invalid_params(
                'Home directory requires an absolute non-root path'
            )

    def _ownership_summary(self, result):
        self._logger.info(
            f'home ownership: username={self.user.username} dry_run={result.dry_run} '
            f'stale_uid={result.stale_uid} uid={self.user.uid} gid={self.user.gid} '
            f'changed={result.changed} skipped={result.skipped} failed={result.failed}'
        )

    def repair_ownership(self, dry_run=False, stale_uid=None):
        self.validate_ownership()
        result = RepairHomeOwnershipResult(username=self.user.username, dry_run=dry_run)
        try:
            home = os.lstat(self.home_dir)
        except OSError:
            result.failed = 1
            self._ownership_summary(result)
            return result
        result.stale_uid = home.st_uid if stale_uid is None else stale_uid

        def visit(path, info=None):
            try:
                info = info if info is not None else os.lstat(path)
                if stat.S_ISDIR(info.st_mode):
                    with os.scandir(path) as entries:
                        for entry in entries:
                            visit(entry.path)
                if info.st_uid != result.stale_uid or (info.st_uid, info.st_gid) == (
                    self.user.uid,
                    self.user.gid,
                ):
                    result.skipped += 1
                    return
                # Keep the stale home owner until a complete traversal succeeds.
                if path == self.home_dir and result.failed:
                    result.skipped += 1
                    return
                if not dry_run:
                    os.lchown(path, self.user.uid, self.user.gid)
                result.changed += 1
                if len(result.paths) < 20:
                    result.paths.append(path)
            except OSError as error:
                result.failed += 1
                self._logger.warning(
                    f'home ownership failed: {path}: {type(error).__name__}'
                )

        visit(self.home_dir, home)
        self._ownership_summary(result)
        return result

    def converge_login_paths(
        self,
        dry_run=False,
        assert_lease=None,
        include_home=True,
        remember_stale_uid=None,
    ):
        self.validate_ownership()
        result = RepairHomeOwnershipResult(username=self.user.username, dry_run=dry_run)
        paths = [(self.home_dir, 0o710), (self.ssh_dir, 0o700)]
        paths.extend(
            (os.path.join(self.ssh_dir, name), mode)
            for name, mode in (
                ('authorized_keys', 0o600),
                ('id_rsa', 0o600),
                ('id_rsa.pub', 0o644),
            )
        )
        for path, mode in paths:
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                result.skipped += 1
                if path in (self.home_dir, self.ssh_dir):
                    break
                continue
            except OSError as error:
                result.failed += 1
                self._logger.warning(
                    f'home ownership failed: {path}: {type(error).__name__}'
                )
                if path in (self.home_dir, self.ssh_dir):
                    break
                continue
            if path == self.home_dir:
                result.stale_uid = info.st_uid
                if not dry_run and info.st_uid != self.user.uid and remember_stale_uid:
                    remember_stale_uid(info.st_uid)
            is_link = stat.S_ISLNK(info.st_mode)
            owner_changed = (info.st_uid, info.st_gid) != (self.user.uid, self.user.gid)
            mode_changed = not is_link and stat.S_IMODE(info.st_mode) != mode
            if path != self.home_dir or include_home:
                if owner_changed or mode_changed:
                    self._logger.info(
                        f'home login path: {path} uid={self.user.uid} gid={self.user.gid} mode={mode:o} dry_run={dry_run}'
                    )
                    try:
                        if not dry_run:
                            if owner_changed:
                                if assert_lease:
                                    assert_lease()
                                os.lchown(path, self.user.uid, self.user.gid)
                            if mode_changed:
                                if assert_lease:
                                    assert_lease()
                                os.chmod(path, mode, follow_symlinks=False)
                        result.changed += 1
                        result.paths.append(path)
                    except OSError as error:
                        result.failed += 1
                        self._logger.warning(
                            f'home ownership failed: {path}: {type(error).__name__}'
                        )
                else:
                    result.skipped += 1
            if path in (self.home_dir, self.ssh_dir) and not stat.S_ISDIR(info.st_mode):
                break
        return result

    def initialize_ssh_dir(self):
        self.validate_ownership()
        if os.path.lexists(self.ssh_dir):
            result = self.converge_login_paths(include_home=False)
            if result.failed:
                raise exceptions.general_exception('SSH ownership convergence failed')
            if not stat.S_ISDIR(os.lstat(self.ssh_dir).st_mode):
                return
        else:
            # created here, then set by descriptor: a link swapped in after the mkdir is not followed
            os.mkdir(self.ssh_dir, 0o700)
            fd = os.open(self.ssh_dir, _DIRECTORY_NOFOLLOW)
            try:
                os.fchmod(fd, 0o700)
                os.fchown(fd, self.user.uid, self.user.gid)
            finally:
                os.close(fd)

        id_rsa_file = os.path.join(self.ssh_dir, 'id_rsa')

        if os.path.lexists(id_rsa_file):
            return

        key = rsa.generate_private_key(
            backend=crypto_default_backend(), public_exponent=65537, key_size=2048
        )
        private_key = key.private_bytes(
            crypto_serialization.Encoding.PEM,
            crypto_serialization.PrivateFormat.TraditionalOpenSSL,
            crypto_serialization.NoEncryption(),
        )
        public_key = key.public_key().public_bytes(
            crypto_serialization.Encoding.OpenSSH,
            crypto_serialization.PublicFormat.OpenSSH,
        )

        self._write_user_file(id_rsa_file, private_key, 0o600)
        self._write_user_file(
            os.path.join(self.ssh_dir, 'id_rsa.pub'), public_key, 0o644
        )
        self._write_user_file(
            os.path.join(self.ssh_dir, 'authorized_keys'), public_key, 0o600
        )

    def initialize_home_dir(self):
        os.makedirs(self.home_dir, exist_ok=True)
        os.chmod(self.home_dir, 0o710)
        self.own_path(self.home_dir)

        skeleton_files = [
            '/etc/skel/.bashrc',
            '/etc/skel/.bash_profile',
            '/etc/skel/.bash_logout',
        ]

        for src_file in skeleton_files:
            dest_file = os.path.join(self.home_dir, os.path.basename(src_file))
            # lexists: a link the user left there, dangling or not, is theirs and is kept
            if os.path.lexists(dest_file) or not os.path.isfile(src_file):
                continue
            with open(src_file, 'rb') as f:
                self._write_user_file(dest_file, f.read(), 0o644)

    def initialize(self):
        self.validate_ownership()

        self.initialize_home_dir()
        self.initialize_ssh_dir()

    def archive(self):
        if not Utils.is_dir(self.home_dir):
            return

        archive_dir = os.path.join(
            USER_HOME_DIR_BASE,
            f'{self.user.username}_{Utils.file_system_friendly_timestamp()}',
        )
        shutil.move(self.home_dir, archive_dir)
        os.chown(archive_dir, 0, 0)
        os.chmod(archive_dir, 0o700)

    @staticmethod
    def validate_and_sanitize_key_format(key_format: str) -> str:
        if Utils.is_empty(key_format):
            raise exceptions.invalid_params('key_format is required')
        key_format = key_format.strip().lower()
        if key_format not in ('ppk', 'pem'):
            raise exceptions.invalid_params('key_format must be one of [pem, ppk]')
        return key_format

    def get_key_name(self, key_format: str) -> str:
        key_format = self.validate_and_sanitize_key_format(key_format)
        cluster_name = self._context.cluster_name()
        username = self.user.username
        return f'{username}_{cluster_name}_privatekey.{key_format}'

    def get_key_material(self, key_format: str, platform: str = 'linux') -> str:
        """
        read the user's private key from user's ~/.ssh directory
        add \r\n (for platform = 'windows) or \n (for platform = 'linux' or 'osx')
        :param key_format: one of [ppk, pem]
        :param platform: one of [windows, linux, osx]
        :return: private key content
        """

        if Utils.is_empty(platform):
            platform = 'linux'

        id_rsa_file = os.path.join(self.ssh_dir, 'id_rsa')
        if not Utils.is_empty(id_rsa_file):
            exceptions.general_exception(
                f'private key not found in home directory for user: {self.user.username}'
            )

        key_format = self.validate_and_sanitize_key_format(key_format)

        def read_private_key_content(file: str) -> str:
            private_key_content = Utils.from_bytes(self._read_user_file(file))

            lines = private_key_content.splitlines()
            if platform in ('linux', 'osx'):
                return '\n'.join(lines)
            elif 'windows' in platform.lower():
                return '\r\n'.join(lines)

            # default
            return '\n'.join(lines)

        if key_format == 'pem':
            return read_private_key_content(id_rsa_file)

        elif key_format == 'ppk':
            id_rsa_ppk_file = os.path.join(self.ssh_dir, 'id_rsa.ppk')
            if not os.path.lexists(id_rsa_ppk_file):
                self.write_ppk(id_rsa_file, id_rsa_ppk_file)

            return read_private_key_content(id_rsa_ppk_file)
