"""
Starts sshd with its stderr on a message socket and passes each message on as one line.

sshd -e writes each log message with one write and leaves a CR or LF inside it as it came, and it
logs the name a client offers before any login. On a pipe a name holding a line break begins a
line of its own, which the task log and the drop tally would take for sshd's. A SOCK_SEQPACKET
socket keeps each write whole, so a line break inside one is written as \\r or \\n and only sshd
begins a line. sshd is exec'd in this process, so the pid the role signals and waits on is
sshd's; the copying runs in a child that ends when the last sshd process does. Only the standard
library. If the socket cannot be made, sshd starts on the stderr it was given.
"""

import os
import signal
import socket
import sys

# sshd's own messages are under 1 KiB; anything else on its stderr is cut at this size
MESSAGE_BYTES = 65536


def one_line(message: bytes) -> bytes:
    """the message with the line end it came with, and any other CR or LF spelled out"""
    end = b'\r\n' if message.endswith(b'\r\n') else b'\n'
    body = message[: -len(end)] if message.endswith(end) else message
    return body.replace(b'\r', b'\\r').replace(b'\n', b'\\n') + end


def copy(messages: socket.socket, out: int) -> None:
    while True:
        try:
            message = messages.recv(MESSAGE_BYTES)
        except InterruptedError:
            continue
        except OSError:
            return
        if not message:
            return
        line = one_line(message)
        try:
            while line:
                line = line[os.write(out, line) :]
        except OSError:
            # the reader is gone; sshd's writes fail from here and it carries on, as on a pipe
            return


def main(command: list) -> None:
    try:
        ours, sshds = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        child = os.fork()
    except OSError as error:
        os.write(2, f'sshd messages are not kept one to a line: {error}\n'.encode())
        os.execv(command[0], command)
    if child == 0:
        sshds.close()
        copy(ours, 2)
        os._exit(0)
    ours.close()
    os.dup2(sshds.fileno(), 2)
    # as a shell would have started it: Python ignores these two and exec keeps that
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    os.execv(command[0], command)


if __name__ == '__main__':
    main(sys.argv[1:])
