import socket
import threading
import time

from router.services import disconnect as disconnect_module
from router.services.disconnect import ClientDisconnectTracker, DisconnectWatcher


class FakeTracker:
    def __init__(self):
        self.calls = 0

    def client_disconnected(self):
        self.calls += 1
        return self.calls >= 2


def test_disconnect_watcher_sets_event_and_calls_callback_once():
    tracker = FakeTracker()
    disconnect_event = threading.Event()
    stop_event = threading.Event()
    calls = []

    watcher = DisconnectWatcher(tracker, disconnect_event, stop_event, lambda: calls.append(None), interval=0.01)
    watcher.start()
    watcher.join(timeout=1)

    assert disconnect_event.is_set()
    assert calls == [None]


def test_disconnect_watcher_stop_event_exits_without_disconnect():
    tracker = FakeTracker()
    disconnect_event = threading.Event()
    stop_event = threading.Event()
    calls = []

    watcher = DisconnectWatcher(tracker, disconnect_event, stop_event, calls.append, interval=0.05)
    watcher.start()
    stop_event.set()
    watcher.join(timeout=1)

    assert not disconnect_event.is_set()
    assert calls == []


def test_client_disconnect_tracker_without_socket_returns_false():
    tracker = ClientDisconnectTracker(None)
    assert tracker.client_disconnected() is False


def test_client_disconnect_tracker_detects_closed_peer():
    client, peer = socket.socketpair()
    tracker = ClientDisconnectTracker(client)
    try:
        assert tracker.client_disconnected() is False
        peer.close()
        assert tracker.client_disconnected() is True
    finally:
        client.close()
        peer.close()


def test_client_disconnect_tracker_disables_itself_when_readiness_check_fails(monkeypatch):
    def out_of_range(sock):
        raise ValueError("filedescriptor out of range in select()")

    monkeypatch.setattr(disconnect_module, "_socket_readable", out_of_range)

    client, peer = socket.socketpair()
    try:
        tracker = ClientDisconnectTracker(client)
        assert tracker.client_disconnected() is False
        assert tracker.client_disconnected() is False
        assert tracker.sock is None
    finally:
        client.close()
        peer.close()


def test_disconnect_watcher_survives_readiness_failure(monkeypatch):
    def out_of_range(sock):
        raise ValueError("filedescriptor out of range in select()")

    monkeypatch.setattr(disconnect_module, "_socket_readable", out_of_range)

    client, peer = socket.socketpair()
    tracker = ClientDisconnectTracker(client)
    client.close()
    peer.close()

    disconnect_event = threading.Event()
    stop_event = threading.Event()
    watcher = DisconnectWatcher(tracker, disconnect_event, stop_event, interval=0.01)
    watcher.start()

    deadline = time.monotonic() + 2
    while tracker.sock is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tracker.sock is None

    stop_event.set()
    watcher.join(timeout=1)

    assert not watcher.is_alive()
    assert not disconnect_event.is_set()
