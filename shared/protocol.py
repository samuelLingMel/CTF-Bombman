"""Newline-delimited JSON messaging over a TCP socket.

Each message is a JSON object terminated by "\n". TCP is a byte stream with
no built-in message boundaries, so this framing is what lets both sides agree
on where one message ends and the next begins.
"""

import json
import socket


def send_message(sock: socket.socket, message: dict) -> None:
    data = (json.dumps(message) + "\n").encode("utf-8")
    sock.sendall(data)


class MessageReader:
    """Buffers incoming bytes and yields complete newline-delimited JSON messages."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = b""

    def read_messages(self):
        """Blocks for one recv() and returns a list of complete messages.

        Returns None if the connection was closed by the peer.
        """
        data = self.sock.recv(4096)
        if not data:
            return None

        self.buffer += data
        messages = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line:
                messages.append(json.loads(line.decode("utf-8")))
        return messages
