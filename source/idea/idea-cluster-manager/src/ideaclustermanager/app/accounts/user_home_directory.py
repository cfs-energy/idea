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
from ideadatamodel import errorcodes, exceptions

import os
import secrets
import stat
import subprocess
import tempfile
import threading
import time
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend as crypto_default_backend

from ideaclustermanager.app.accounts.ppk import to_ppk
from ideasdk.shell import ShellInvoker
from ideasdk.filesystem.user_identity import UserIdentity


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
        the user's key as a PuTTY file, in process in the shipped container. An
        explicitly installed puttygen takes precedence. The server runs as
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

    # far above any key: a larger file is not one, and is not read into the server
    KEY_FILE_LIMIT = 64 * 1024

    def _read_user_file(self, path: str) -> bytes:
        """
        a file in the user's directory, read as the account itself (the file browser's
        worker), so it returns only what the account can read. Never through a link the
        user put there, only a regular file (a FIFO would hang), one the account owns,
        with one link, and of key size.
        """
        identity = UserIdentity(self._context.accounts, self.user.username)
        return identity.run(self._read_own_file, path, identity.uid)

    def _read_own_file(self, path: str, uid: int) -> bytes:
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
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise exceptions.general_exception(f'{path} is not a regular file')
            # a second name for a file kept elsewhere is not the user's own file
            if info.st_nlink > 1:
                raise exceptions.general_exception(f'{path} has more than one link')
            # a file left by another account (a restore, a uid change, a rename into
            # the folder) is not this account's key
            if info.st_uid != uid:
                raise exceptions.general_exception(
                    f'{path} is not owned by the account'
                )
            if info.st_size > self.KEY_FILE_LIMIT:
                raise exceptions.general_exception(f'{path} is too large to be a key')
            data = f.read(self.KEY_FILE_LIMIT + 1)
            if len(data) > self.KEY_FILE_LIMIT:
                raise exceptions.general_exception(f'{path} is too large to be a key')
            return data

    # the one key file the bastion's sshd consults (AuthorizedKeysFile); while the
    # account is disabled it waits under this name

    def set_aside_authorized_keys(self, timeout=300) -> str:
        """
        a disabled account's authorized_keys, with every key in it, moved aside so key
        sign-in stops. :return: what was done
        """
        return self._administrative(self._move_keys, self.user.uid, True, timeout)

    def restore_authorized_keys(self, timeout=300) -> str:
        """an enabled account's keys put back; one made while disabled is kept"""
        return self._administrative(self._move_keys, self.user.uid, False, timeout)

    @staticmethod
    def _administrative(operation, uid, aside, timeout):
        """
        done by the service with the account's stored ids, never as the account: a
        disabled account must not run anything. A rename through a directory descriptor
        that follows no link changes nothing outside the account's own .ssh folder.
        Run on its own thread, so a home that does not answer costs the timeout only.
        """
        outcome = {}

        def run():
            try:
                outcome['done'] = operation(uid, aside)
            except Exception as error:
                outcome['error'] = error

        worker = threading.Thread(target=run, name='ssh-keys', daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise exceptions.SocaException(
                error_code=errorcodes.FILE_OPERATION_TIMED_OUT,
                message='This took too long and was stopped.',
            )
        if 'error' in outcome:
            raise outcome['error']
        return outcome['done']

    # the files sshd reads keys from: the bastion's AuthorizedKeysFile is
    # .ssh/authorized_keys only; OpenSSH's default on other hosts adds authorized_keys2
    KEY_FILE_NAMES = ('authorized_keys', 'authorized_keys2')
    SET_ASIDE_SUFFIX = '.disabled'

    @staticmethod
    def _is_placeholder(dir_fd, name) -> bool:
        """the empty, read-only file the service left in a set-aside entry's place"""
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_size == 0
            and stat.S_IMODE(info.st_mode) == 0o444
            and info.st_uid == os.geteuid()
        )

    @classmethod
    def _set_entry_aside(cls, dir_fd, name) -> bool:
        """
        renames the entry itself, whatever it is (a file, a link, a second name), never
        what it points to, and leaves an empty read-only file of the service's own in its
        place, so nothing is read through the old name
        """
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if cls._is_placeholder(dir_fd, name):
            return False
        target = name + cls.SET_ASIDE_SUFFIX
        try:
            os.stat(target, dir_fd=dir_fd, follow_symlinks=False)
            # one is aside already: it moves on, and the current keys take the slot
            # that enabling restores, so a later enable brings back these, not those
            os.rename(
                target,
                f'{target}-{int(time.time())}',
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
        except FileNotFoundError:
            pass
        os.rename(name, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o444,
            dir_fd=dir_fd,
        )
        try:
            os.fchmod(fd, 0o444)
        finally:
            os.close(fd)
        return True

    @classmethod
    def _restore_entry(cls, dir_fd, name) -> bool:
        """puts back only what a set-aside made: its placeholder is still in place"""
        if not cls._is_placeholder(dir_fd, name):
            return False
        try:
            os.stat(name + cls.SET_ASIDE_SUFFIX, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        os.unlink(name, dir_fd=dir_fd)
        os.rename(
            name + cls.SET_ASIDE_SUFFIX, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd
        )
        return True

    def _move_keys(self, uid: int, aside: bool) -> str:
        """
        the account's key files set aside on disable or put back on enable, by name in
        directories opened without following a link; a .ssh that is not a plain directory
        is set aside as a whole
        """
        try:
            home_fd = os.open(
                self.home_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
        except FileNotFoundError:
            return 'no keys'
        try:
            try:
                info = os.stat('.ssh', dir_fd=home_fd, follow_symlinks=False)
            except FileNotFoundError:
                return 'no keys'
            if not stat.S_ISDIR(info.st_mode) or (
                not aside and self._is_placeholder(home_fd, '.ssh')
            ):
                # a link or anything else in .ssh's place: the name itself goes aside
                done = (
                    self._set_entry_aside(home_fd, '.ssh')
                    if aside
                    else self._restore_entry(home_fd, '.ssh')
                )
                return 'moved' if done else 'nothing to move'
            dir_fd = os.open('.ssh', _DIRECTORY_NOFOLLOW, dir_fd=home_fd)
            try:
                done = [
                    self._set_entry_aside(dir_fd, name)
                    if aside
                    else self._restore_entry(dir_fd, name)
                    for name in self.KEY_FILE_NAMES
                ]
            finally:
                os.close(dir_fd)
            return 'moved' if any(done) else 'nothing to move'
        finally:
            os.close(home_fd)

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

    # well inside the task deadline, which stops the whole process: a large home is
    # repaired over several attempts, each skipping what an earlier one changed
    REPAIR_BUDGET_SECONDS = 90

    def repair_ownership(self, dry_run=False, stale_uid=None):
        self.validate_ownership()
        deadline = time.monotonic() + self.REPAIR_BUDGET_SECONDS
        out_of_time = []
        result = RepairHomeOwnershipResult(username=self.user.username, dry_run=dry_run)
        try:
            home = os.lstat(self.home_dir)
        except OSError:
            result.failed = 1
            self._ownership_summary(result)
            return result
        result.stale_uid = home.st_uid if stale_uid is None else stale_uid

        # walked by descriptor: a folder swapped for a link while the walk is inside it
        # cannot redirect a chown, and another file system mounted inside is not entered
        def update(parent_fd, name, path, info, fd=None):
            if info.st_uid != result.stale_uid or (info.st_uid, info.st_gid) == (
                self.user.uid,
                self.user.gid,
            ):
                result.skipped += 1
                return
            # a file with a second name may be kept elsewhere: not the home's to give
            if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
                result.skipped += 1
                return
            # Keep the stale home owner until a complete traversal succeeds.
            if path == self.home_dir and result.failed:
                result.skipped += 1
                return
            if not dry_run:
                if fd is not None:
                    os.fchown(fd, self.user.uid, self.user.gid)
                else:
                    os.chown(
                        name,
                        self.user.uid,
                        self.user.gid,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
            result.changed += 1
            if len(result.paths) < 20:
                result.paths.append(path)

        def visit(parent_fd, name, path, info=None):
            if out_of_time or time.monotonic() > deadline:
                # out of time: counted once as failed, so the home keeps its stale
                # owner and the task is tried again
                if not out_of_time:
                    out_of_time.append(True)
                    result.failed += 1
                return
            try:
                if info is None:
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    update(parent_fd, name, path, info)
                    return
                if info.st_dev != home.st_dev:
                    result.skipped += 1
                    return
                fd = os.open(name, _DIRECTORY_NOFOLLOW, dir_fd=parent_fd)
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise FileNotFoundError(path)
                    with os.scandir(fd) as entries:
                        names = [entry.name for entry in entries]
                    for child in names:
                        visit(fd, child, os.path.join(path, child))
                    update(parent_fd, name, path, info, fd)
                finally:
                    os.close(fd)
            except OSError as error:
                result.failed += 1
                self._logger.warning(
                    f'home ownership failed: {path}: {type(error).__name__}'
                )

        visit(None, self.home_dir, self.home_dir, home)
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
        # each entry by name inside the one before it, opened without following a link,
        # and checked and changed through that descriptor: an entry swapped between the
        # check and the change is not the one changed
        opened = []
        try:
            for path, mode in paths:
                parent_fd = opened[-1] if opened else None
                name = os.path.basename(path) if opened else path
                is_dir = path in (self.home_dir, self.ssh_dir)
                try:
                    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    result.skipped += 1
                    if is_dir:
                        break
                    continue
                except OSError as error:
                    result.failed += 1
                    self._logger.warning(
                        f'home ownership failed: {path}: {type(error).__name__}'
                    )
                    if is_dir:
                        break
                    continue
                if not (stat.S_ISDIR if is_dir else stat.S_ISREG)(named.st_mode):
                    # a link, or not the kind of entry this is: left as it is
                    result.skipped += 1
                    self._logger.warning(
                        f'home login path left as it is: {path}: not a plain '
                        f'{"directory" if is_dir else "file"}'
                    )
                    if is_dir:
                        break
                    continue
                try:
                    fd = os.open(
                        name,
                        _DIRECTORY_NOFOLLOW
                        if is_dir
                        else os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                        dir_fd=parent_fd,
                    )
                except OSError as error:
                    result.failed += 1
                    self._logger.warning(
                        f'home ownership failed: {path}: {type(error).__name__}'
                    )
                    if is_dir:
                        break
                    continue
                if is_dir:
                    opened.append(fd)
                try:
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                        result.failed += 1
                        if is_dir:
                            break
                        continue
                    if path == self.home_dir:
                        result.stale_uid = info.st_uid
                        owners = {self.user.uid, info.st_uid, 0}
                        if (
                            not dry_run
                            and info.st_uid != self.user.uid
                            and remember_stale_uid
                        ):
                            remember_stale_uid(info.st_uid)
                    if path == self.ssh_dir:
                        owners.add(info.st_uid)
                    elif not is_dir and (
                        info.st_nlink > 1 or info.st_uid not in owners
                    ):
                        # a second name for a file kept elsewhere, or a file owned by
                        # no one this home has belonged to: not the user's to be given
                        result.skipped += 1
                        self._logger.warning(
                            f'home login path left as it is: {path}: linked or owned elsewhere'
                        )
                        continue
                    owner_changed = (info.st_uid, info.st_gid) != (
                        self.user.uid,
                        self.user.gid,
                    )
                    mode_changed = stat.S_IMODE(info.st_mode) != mode
                    if path == self.home_dir and not include_home:
                        continue
                    if not (owner_changed or mode_changed):
                        result.skipped += 1
                        continue
                    self._logger.info(
                        f'home login path: {path} uid={self.user.uid} gid={self.user.gid} mode={mode:o} dry_run={dry_run}'
                    )
                    try:
                        if not dry_run:
                            if owner_changed:
                                if assert_lease:
                                    assert_lease()
                                os.fchown(fd, self.user.uid, self.user.gid)
                            if mode_changed:
                                if assert_lease:
                                    assert_lease()
                                os.fchmod(fd, mode)
                        result.changed += 1
                        result.paths.append(path)
                    except OSError as error:
                        result.failed += 1
                        self._logger.warning(
                            f'home ownership failed: {path}: {type(error).__name__}'
                        )
                finally:
                    if not is_dir:
                        os.close(fd)
        finally:
            for fd in opened:
                os.close(fd)
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

        # the private key last: its presence marks the set done, so a stop part way
        # leaves no private key whose public half was never authorized
        self._write_user_file(
            os.path.join(self.ssh_dir, 'authorized_keys'), public_key, 0o600
        )
        self._write_user_file(
            os.path.join(self.ssh_dir, 'id_rsa.pub'), public_key, 0o644
        )
        self._write_user_file(id_rsa_file, private_key, 0o600)

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

    def archive(self) -> str:
        """
        a deleted account's home renamed aside, root-only, beside where it was. A home
        that is a link or its own mount is left where it is: a rename would move the link,
        or copy a whole volume. :return: what was done
        """
        try:
            info = os.lstat(self.home_dir)
        except FileNotFoundError:
            return 'no home'
        if not stat.S_ISDIR(info.st_mode):
            self._logger.warning(
                f'home left in place, not a directory: {self.home_dir}'
            )
            return 'left in place'
        parent = os.path.dirname(os.path.normpath(self.home_dir))
        if os.lstat(parent).st_dev != info.st_dev:
            self._logger.warning(f'home left in place, its own mount: {self.home_dir}')
            return 'left in place'
        archive_dir = os.path.join(
            parent, f'{self.user.username}_{Utils.file_system_friendly_timestamp()}'
        )
        # a rename only: one file system, nothing copied
        os.rename(self.home_dir, archive_dir)
        fd = os.open(archive_dir, _DIRECTORY_NOFOLLOW)
        try:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        return 'archived'

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
