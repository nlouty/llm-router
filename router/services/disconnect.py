from __future__ import annotations

import select
import socket
import threading
import time


def _socket_readable(sock) -> bool:
    # select.select() raises "filedescriptor out of range in select()" once an
    # fd exceeds FD_SETSIZE (1024 on Linux), which happens routinely in
    # long-lived gunicorn workers. poll() has no such limit.
    if hasattr(select, "poll"):
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        return bool(poller.poll(0))
    readable, _, _ = select.select([sock], [], [], 0)
    return bool(readable)


class ClientDisconnectTracker:
    def __init__(self, sock=None):
        self.sock = sock
        self._disconnected = False

    def client_disconnected(self) -> bool:
        if self._disconnected or self.sock is None:
            return self._disconnected
        try:
            if not _socket_readable(self.sock):
                return False
            data = self.sock.recv(1, socket.MSG_PEEK)
            if data == b"":
                self._disconnected = True
        except ValueError:
            # The socket cannot be checked for readiness (e.g. its fd is out
            # of range or already closed). That is not evidence of a client
            # disconnect, so disable tracking instead of guessing either way.
            self.sock = None
        except (ConnectionResetError, BrokenPipeError, OSError):
            self._disconnected = True
        return self._disconnected


class DisconnectWatcher(threading.Thread):
    def __init__(
        self,
        tracker: ClientDisconnectTracker,
        disconnect_event: threading.Event,
        stop_event: threading.Event | float | None = None,
        on_disconnect=None,
        interval: float = 0.5,
    ):
        super().__init__(daemon=True)
        if isinstance(stop_event, (int, float)):
            interval = float(stop_event)
            stop_event = None
        self.tracker = tracker
        self.disconnect_event = disconnect_event
        self.stop_event = stop_event or disconnect_event
        self.on_disconnect = on_disconnect
        self.interval = interval

    def run(self) -> None:
        while not self.stop_event.is_set():
            if self.tracker.client_disconnected():
                self.disconnect_event.set()
                if self.on_disconnect:
                    try:
                        self.on_disconnect()
                    except Exception:
                        pass
                return
            time.sleep(self.interval)
