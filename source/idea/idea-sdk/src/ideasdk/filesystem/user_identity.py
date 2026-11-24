import errno
import multiprocessing
import os
import signal
import sys
from typing import Optional

from ideadatamodel import errorcodes, exceptions

# What a person can do about a refusal from the file system, by its cause
_OS_REFUSALS = {
    errno.ENOSPC: (
        errorcodes.FILE_SYSTEM_FULL,
        'There is no space left on this file system. Free some space or use another location.',
    ),
    errno.EDQUOT: (
        errorcodes.FILE_QUOTA_EXCEEDED,
        'Your storage quota on this file system is used up. '
        'Delete files you no longer need, or ask your administrator for more space.',
    ),
    errno.EFBIG: (
        errorcodes.FILE_TOO_LARGE,
        'The file is larger than this file system allows.',
    ),
    errno.ENAMETOOLONG: (
        errorcodes.FILE_NAME_TOO_LONG,
        'A file or folder name is too long for this file system. Use a shorter name.',
    ),
    errno.EROFS: (
        errorcodes.FILE_SYSTEM_READ_ONLY,
        'This location is read-only. Choose a folder you can write to.',
    ),
}


def os_refusal(exc: BaseException) -> Optional[exceptions.SocaException]:
    """
    The product error for an operating-system refusal a person can act on, or None for
    anything else, which stays a server error. A refused permission is an access refusal.
    """
    if isinstance(exc, PermissionError):
        return exceptions.unauthorized_access()
    if isinstance(exc, OSError) and exc.errno in _OS_REFUSALS:
        return exceptions.SocaException(*_OS_REFUSALS[exc.errno])
    return None


class UserIdentity:
    def __init__(self, accounts, username, allow_disabled: bool = False):
        """
        :param allow_disabled: admit a disabled account. Only for read-only work the
            server does about the account and never on its behalf, that runs nothing of
            the account's own (measuring its home for costs); everything a person asks
            for, or that runs as them, keeps the refusal.
        """
        user = accounts.get_user(username)
        # a disabled account keeps no file access for the rest of its token's hour; only an
        # explicit False, as records from before the field have none
        if (
            user is None
            or (user.enabled is False and not allow_disabled)
            or not self._valid_id(user.uid)
            or not self._valid_id(user.gid)
        ):
            raise exceptions.unauthorized_access()
        self.uid = user.uid
        self.gid = user.gid
        gids = {self.gid}
        for name in user.additional_groups or []:
            try:
                group = accounts.get_group(name)
            except exceptions.SocaException:
                group = None
            # A stale membership or a disabled group only removes access; it must not
            # refuse the request.
            if (
                group is not None
                and group.enabled is not False
                and self._valid_id(group.gid)
            ):
                gids.add(group.gid)
        self.gids = sorted(gids)
        self._worker_pid = None

    @staticmethod
    def _valid_id(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def in_worker(self) -> bool:
        return self._worker_pid == os.getpid()

    def run(self, operation, *args, timeout=300, **kwargs):
        if self.in_worker():
            return operation(*args, **kwargs)

        def worker():
            # so being stopped runs this operation's cleanup instead of whatever the server set
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
            receiver.close()
            try:
                # Only the child changes credentials, so concurrent requests retain isolation.
                # An unprivileged caller may use its existing identity without setgroups.
                if os.geteuid() == 0:
                    os.setgroups(self.gids)
                    os.setresgid(self.gid, self.gid, self.gid)
                    os.setresuid(self.uid, self.uid, self.uid)
                elif (
                    os.getuid() != self.uid
                    or os.geteuid() != self.uid
                    or os.getgid() != self.gid
                    or os.getegid() != self.gid
                    or set(os.getgroups()) | {self.gid} != set(self.gids)
                ):
                    raise PermissionError('Cannot assume account identity')
                self._worker_pid = os.getpid()
                sender.send(('result', operation(*args, **kwargs)))
            except PermissionError:
                sender.send(('permission', None))
            except exceptions.SocaException as exc:
                sender.send(('application', (exc.error_code, exc.message)))
            except Exception as exc:
                refusal = os_refusal(exc)
                if refusal is not None:
                    sender.send(('application', (refusal.error_code, refusal.message)))
                else:
                    sender.send(('error', exc))
            finally:
                sender.close()

        context = multiprocessing.get_context('fork')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=worker)
        try:
            process.start()
            sender.close()
            if not receiver.poll(timeout):
                # slow is not forbidden: the person must not be told they lack access
                raise exceptions.SocaException(
                    error_code=errorcodes.FILE_OPERATION_TIMED_OUT,
                    message='This took too long and was stopped.',
                )
            try:
                kind, result = receiver.recv()
            except EOFError:
                raise exceptions.unauthorized_access('Identity worker failed')
            if kind == 'permission':
                raise exceptions.unauthorized_access()
            if kind == 'application':
                raise exceptions.SocaException(*result)
            if kind == 'error':
                raise result
            return result
        finally:
            receiver.close()
            sender.close()
            if process.pid is not None:
                process.join(timeout=1)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    # a child in uninterruptible file-system I/O ends when the I/O returns:
                    # the worker thread is not held waiting for that
                    process.join(timeout=5)
                if not process.is_alive():
                    process.close()
