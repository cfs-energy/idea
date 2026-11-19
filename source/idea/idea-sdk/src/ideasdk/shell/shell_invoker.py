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

from ideasdk.utils import Utils

from typing import List, Optional, Dict, Callable, AnyStr, Union
from subprocess import CompletedProcess
import os
import pwd
import subprocess
import signal
from threading import RLock
import time

StreamOutputCallback = Callable[[AnyStr], None]

# what a command run for a user keeps from this process: the search path, locale, time zone
# and the batch client's settings (PBS_CONF_FILE, PBS_EXEC, PBS_HOME, PBS_SERVER and the rest)
USER_ENVIRONMENT_NAMES = ('PATH', 'LANG', 'TZ')
USER_ENVIRONMENT_PREFIXES = ('LC_', 'PBS_')


def user_environment(account: pwd.struct_passwd) -> Dict[str, str]:
    """
    the environment a command run for a user starts with. nothing else of this process
    reaches it, so the service's credentials and settings stay with the service.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if name in USER_ENVIRONMENT_NAMES or name.startswith(USER_ENVIRONMENT_PREFIXES)
    }
    env.setdefault('PATH', os.defpath)
    env.update(
        HOME=account.pw_dir,
        USER=account.pw_name,
        LOGNAME=account.pw_name,
        SHELL=account.pw_shell or '/bin/bash',
    )
    return env


class ShellInvocationResult:
    def __init__(
        self,
        command,
        result: CompletedProcess,
        total_time_ms: int,
        text=True,
        pipe=False,
    ):
        self.command = command
        self.args = result.args
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr
        self.total_time_ms = total_time_ms
        self.pipe = pipe
        if text:
            if result.stdout:
                self.stdout = result.stdout.strip()
            if result.stderr:
                self.stderr = result.stderr.strip()

    def __str__(self):
        shell_command = self.shell_command
        if self.returncode == 0:
            return f'shell> {shell_command} >> returncode: {self.returncode} >> {self.total_time_ms} ms'
        else:
            if Utils.is_not_empty(self.stderr):
                output = self.stderr
            else:
                output = self.stdout
            return (
                f'shell> {shell_command} >> returncode: {self.returncode} '
                f'>> stderr: {output} >> {self.total_time_ms} ms'
            )

    @property
    def shell_command(self):
        command = self.command
        if isinstance(command, list):
            if self.pipe:
                commands = command
                shelled = []
                for cmd in commands:
                    shelled.append(' '.join(cmd))
                command = ' | '.join(shelled)
            else:
                command = ' '.join(command)
        return command


class StreamInvocationProcess:
    def __init__(
        self,
        cmd: Union[List[str], str],
        callback: StreamOutputCallback,
        cwd: Optional[str] = None,
        shell=False,
        text=True,
        env: Optional[Dict] = None,
        start_new_session=False,
        stop_signal=signal.SIGINT,
        ignore_keyboard_interrupt=False,
    ):
        self._cmd = cmd
        self._callback = callback
        self._cwd = cwd
        self._shell = shell
        self._text = text
        self._env = env
        self._start_new_session = start_new_session
        self._stop_signal = stop_signal
        self._ignore_keyboard_interrupt = ignore_keyboard_interrupt

        self._lock = RLock()
        self._process: Optional[subprocess.Popen] = None
        self._is_stop_signaled = False

    @property
    def process(self) -> subprocess.Popen:
        assert self._process is not None
        return self._process

    @property
    def stop_signal(self) -> int:
        return self._stop_signal

    def send_signal(self, sig: int):
        assert self._process is not None
        self._process.send_signal(sig)

    def send_stop_signal(self):
        assert self._process is not None
        with self._lock:
            if self.is_stop_signaled:
                return
            self.send_signal(self.stop_signal)
            self._is_stop_signaled = True

    def send_kill_signal(self):
        self.send_signal(signal.SIGKILL)

    @property
    def is_stop_signaled(self) -> bool:
        return self._is_stop_signaled

    def start_streaming(self) -> int:
        self._process = subprocess.Popen(
            args=self._cmd,
            shell=self._shell,
            text=self._text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self._cwd,
            env=self._env,
            start_new_session=self._start_new_session,
            encoding='utf-8',
        )
        for line in iter(self.process.stdout.readline, ''):
            try:
                self._callback(line)
            except KeyboardInterrupt as e:
                if self._ignore_keyboard_interrupt:
                    pass
                raise e
        while self.process.poll() is None:
            time.sleep(0.01)
        return self.process.poll()

    def wait(self, timeout=None) -> int:
        assert self._process is not None
        return self._process.wait(timeout)


class ShellInvoker:
    def __init__(self, logger=None, cwd: str = None):
        self._logger = logger
        self._cwd = cwd

    @property
    def cwd(self) -> Optional[str]:
        return self._cwd

    @cwd.setter
    def cwd(self, path: Optional[str]):
        self._cwd = path

    def invoke(
        self,
        cmd=None,
        shell=False,
        text=True,
        skip_error_logging=False,
        env: Optional[Dict] = None,
        cmd_input=None,
        timeout: Optional[float] = None,
    ) -> ShellInvocationResult:
        """
        invoke cmd and wait for it to finish.
        timeout is in seconds; on expiry the child is killed and subprocess.TimeoutExpired
        is raised. timeout=None waits forever.
        """
        return self._run(
            cmd,
            skip_error_logging,
            input=cmd_input,
            shell=shell,
            text=text,
            cwd=self.cwd,
            env=env,
            timeout=timeout,
        )

    def invoke_as_user(
        self,
        username: str,
        cmd: List[str],
        cwd: Optional[str] = None,
        skip_error_logging=False,
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
        capture_stdout: bool = True,
        output_fd: Optional[int] = None,
    ) -> ShellInvocationResult:
        """
        run cmd, an argument list run without a shell, as the named user with that user's
        groups and the environment user_environment() builds. every command the service
        runs for a user goes through here.
        env replaces user_environment() for a command the service runs as its own account;
        never pass the service environment for a person's account.
        capture_stdout False discards standard output (standard error is still kept).
        output_fd sends both streams to that open file instead; nothing is captured.
        cwd is entered by the command itself after the identity change, so a folder the
        user cannot reach is refused rather than entered with the service's access.
        """
        if not isinstance(cmd, list):
            raise ValueError('cmd must be a list of arguments')
        account = pwd.getpwnam(username)
        if account.pw_uid == 0:
            raise PermissionError('commands are not run for the superuser')
        identity = {}
        if os.geteuid() == 0:
            identity = dict(
                user=account.pw_uid,
                group=account.pw_gid,
                extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
            )
        elif os.geteuid() != account.pw_uid:
            raise PermissionError(f'cannot run a command as {username}')
        if cwd is not None:
            # subprocess changes directory before it drops the identity; env does it after
            cmd = ['/usr/bin/env', '-C', cwd, '--', *cmd]
        if output_fd is not None:
            output = dict(stdout=output_fd, stderr=subprocess.STDOUT)
        elif capture_stdout:
            output = {}
        else:
            output = dict(stdout=subprocess.DEVNULL)
        return self._run(
            cmd,
            skip_error_logging,
            text=True,
            stdin=subprocess.DEVNULL,
            env=user_environment(account) if env is None else env,
            **output,
            timeout=timeout,
            **identity,
        )

    def _run(
        self, cmd, skip_error_logging, text=True, **kwargs
    ) -> ShellInvocationResult:
        start_time = Utils.current_time_ms()

        if 'stdout' in kwargs:
            kwargs.setdefault('stderr', subprocess.PIPE)
        else:
            kwargs['capture_output'] = True
        result = subprocess.run(args=cmd, text=text, **kwargs)

        total_time = Utils.current_time_ms() - start_time

        response = ShellInvocationResult(
            command=cmd, result=result, total_time_ms=total_time, text=text
        )

        if self._logger is None:
            return response

        if response.returncode == 0:
            self._logger.debug(response)
        else:
            if not skip_error_logging:
                self._logger.error(response)

        return response

    def invoke_stream(
        self,
        cmd: Union[List[str], str],
        callback: StreamOutputCallback,
        shell=False,
        text=True,
        env: Optional[Dict] = None,
        start_new_session=False,
        stop_signal=signal.SIGINT,
        ignore_keyboard_interrupt=False,
    ):
        return StreamInvocationProcess(
            cmd=cmd,
            cwd=self.cwd,
            callback=callback,
            shell=shell,
            text=text,
            env=env,
            start_new_session=start_new_session,
            stop_signal=stop_signal,
            ignore_keyboard_interrupt=ignore_keyboard_interrupt,
        )
