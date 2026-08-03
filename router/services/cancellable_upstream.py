from __future__ import annotations

import socket
import threading
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.poolmanager import SSL_KEYWORDS


def _shutdown_socket(connection) -> None:
    """Abort in-flight I/O on a connection without calling close().

    HTTPConnection.close() nulls ``sock``, which races with http.client's
    ``send()`` (``self.sock.sendall(...)``) and surfaces as a spurious
    ``AttributeError: 'NoneType' object has no attribute 'sendall'``. A bare
    shutdown() is enough to fail the in-flight request with an OSError that
    urllib3/requests convert into ConnectionError; the broken connection is
    then closed by urllib3's error path (or session.close()).
    """
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class UpstreamCancellationController:
    def __init__(self):
        self._lock = threading.Lock()
        self._connections = set()
        self._cancelled = False

    def register(self, connection) -> None:
        with self._lock:
            if self._cancelled:
                should_abort = True
            else:
                self._connections.add(connection)
                should_abort = False
        if should_abort:
            _shutdown_socket(connection)

    def unregister(self, connection) -> None:
        with self._lock:
            self._connections.discard(connection)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            connections = list(self._connections)
        for connection in connections:
            _shutdown_socket(connection)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled


class CancellableHTTPConnection(HTTPConnection):
    def __init__(self, *args, cancel_controller: UpstreamCancellationController | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._cancel_controller = cancel_controller

    def connect(self) -> None:
        super().connect()
        if self._cancel_controller:
            self._cancel_controller.register(self)

    def close(self) -> None:
        if self._cancel_controller:
            self._cancel_controller.unregister(self)
        super().close()


class CancellableHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, cancel_controller: UpstreamCancellationController | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._cancel_controller = cancel_controller

    def connect(self) -> None:
        super().connect()
        if self._cancel_controller:
            self._cancel_controller.register(self)

    def close(self) -> None:
        if self._cancel_controller:
            self._cancel_controller.unregister(self)
        super().close()


class CancellableHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = CancellableHTTPConnection

    def __init__(self, *args, cancel_controller: UpstreamCancellationController | None = None, **kwargs):
        kwargs["cancel_controller"] = cancel_controller
        super().__init__(*args, **kwargs)


class CancellableHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = CancellableHTTPSConnection

    def __init__(self, *args, cancel_controller: UpstreamCancellationController | None = None, **kwargs):
        kwargs["cancel_controller"] = cancel_controller
        super().__init__(*args, **kwargs)


class CancellablePoolManager(PoolManager):
    def __init__(self, *args, cancel_controller: UpstreamCancellationController, **kwargs):
        self.cancel_controller = cancel_controller
        super().__init__(*args, **kwargs)
        self.pool_classes_by_scheme = {
            "http": CancellableHTTPConnectionPool,
            "https": CancellableHTTPSConnectionPool,
        }

    def _new_pool(self, scheme: str, host: str, port: int, request_context: dict[str, Any] | None = None):
        pool_cls = self.pool_classes_by_scheme[scheme]
        if request_context is None:
            request_context = self.connection_pool_kw.copy()
        else:
            request_context = request_context.copy()

        if request_context.get("blocksize") is None:
            request_context["blocksize"] = 16384

        for key in ("scheme", "host", "port"):
            request_context.pop(key, None)

        if scheme == "http":
            for keyword in SSL_KEYWORDS:
                request_context.pop(keyword, None)

        return pool_cls(host, port, cancel_controller=self.cancel_controller, **request_context)


class CancellableHTTPAdapter(HTTPAdapter):
    def __init__(self, cancel_controller: UpstreamCancellationController, *args, **kwargs):
        self.cancel_controller = cancel_controller
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs) -> None:
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block
        self.poolmanager = CancellablePoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            cancel_controller=self.cancel_controller,
            **pool_kwargs,
        )


class CancellableUpstreamRequest:
    def __init__(self):
        self._controller = UpstreamCancellationController()
        self._session = requests.Session()
        adapter = CancellableHTTPAdapter(self._controller)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._lock = threading.Lock()
        self._response = None

    def request(self, method: str, url: str, **kwargs):
        kwargs["stream"] = True
        response = self._session.request(method, url, **kwargs)
        with self._lock:
            self._response = response
        if self._controller.cancelled:
            response.close()
        return response

    def cancel(self) -> None:
        self._controller.cancel()
        with self._lock:
            response = self._response
        if response is not None:
            response.close()
        self._session.close()

    def close(self) -> None:
        with self._lock:
            response = self._response
            self._response = None
        if response is not None:
            response.close()
        self._session.close()
