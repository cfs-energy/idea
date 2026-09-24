import multiprocessing
import os

from ideadatamodel import exceptions


class UserIdentity:
    def __init__(self, accounts, username):
        user = accounts.get_user(username)
        if user is None or not self._valid_id(user.uid) or not self._valid_id(user.gid):
            raise exceptions.unauthorized_access()
        self.uid = user.uid
        self.gid = user.gid
        gids = {self.gid}
        for name in user.additional_groups or []:
            try:
                group = accounts.get_group(name)
            except exceptions.SocaException:
                group = None
            # A stale membership only removes access; it must not refuse the request.
            if group is not None and self._valid_id(group.gid):
                gids.add(group.gid)
        self.gids = sorted(gids)
        self._worker_pid = None

    @staticmethod
    def _valid_id(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def run(self, operation, *args, timeout=300, **kwargs):
        if self._worker_pid == os.getpid():
            return operation(*args, **kwargs)

        def worker():
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
                raise exceptions.unauthorized_access('Identity worker timed out')
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
                    process.join()
                process.close()
