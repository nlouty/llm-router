import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

import requests

from router.services.cancellable_upstream import CancellableUpstreamRequest


class SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.request_seen.set()
        try:
            time.sleep(5)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.server.connection_closed.set()

    def log_message(self, format, *args):
        pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def test_cancellable_upstream_cancel_interrupts_blocked_request():
    server = ThreadedHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.request_seen = threading.Event()
    server.connection_closed = threading.Event()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    client = CancellableUpstreamRequest()
    result = {}

    def make_request():
        try:
            client.request("GET", f"http://127.0.0.1:{server.server_address[1]}/", timeout=(1, 10)).content
        except requests.RequestException as exc:
            result["exception"] = exc

    request_thread = threading.Thread(target=make_request)
    request_thread.start()
    assert server.request_seen.wait(timeout=1)

    client.cancel()
    request_thread.join(timeout=2)

    server.shutdown()
    server.server_close()

    assert not request_thread.is_alive()
    assert isinstance(result.get("exception"), requests.RequestException)


def test_cancel_before_connect_surfaces_as_request_exception():
    """Regression: cancelling before the upstream connection exists used to
    leave http.client with sock=None at sendall() time, surfacing as a raw
    AttributeError ('NoneType' object has no attribute 'sendall') instead of
    a requests exception the router's retry/499 paths can handle."""
    server = ThreadedHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.request_seen = threading.Event()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        client = CancellableUpstreamRequest()
        client.cancel()
        try:
            client.request("GET", f"http://127.0.0.1:{server.server_address[1]}/", timeout=(1, 1))
        except requests.RequestException:
            pass
        else:
            raise AssertionError("cancel() before connect should abort the request with a RequestException")
    finally:
        server.shutdown()
        server.server_close()
