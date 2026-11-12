"""Prefill read-timeout removal (issue #323 comment).

A fixed prefill read cap (previously 300s) killed requests that were merely
queued long — e.g. a crashed prefiller's whole intake storming a healthy
0-prefix-cache prefiller. Client prefill requests now carry no read timeout:
they wait as long as the prefiller needs. The connect timeout still applies
(connection failures feed the retry policy), and the llm-choosing deadline
still clamps internal routing calls (covered in test_llm_choosing_timeout.py).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from router.services.proxy import ProxyService
from router.services.proxy_pd_forward import PDForwardService


def _prefill_response():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
        "kv_transfer_params": {
            "remote_engine_id": "e1",
            "remote_host": "h",
            "remote_port": 1,
        },
    }
    return response


def test_prefill_read_timeout_is_unbounded(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["timeout"] = timeout
        return _prefill_response()

    monkeypatch.setattr("router.services.proxy_pd_forward.requests.post", fake_post)

    proxy = MagicMock()
    proxy._deadline_timeout = ProxyService._deadline_timeout
    service = PDForwardService(proxy)

    assert service.prefill_timeout[1] is None

    service._do_prefill(
        MagicMock(base_url="http://p.example"),
        "http://p.example/v1/chat/completions",
        {},
        b"{}",
    )
    # requests.post received the unbounded (connect, None) read timeout, so a
    # long-queued prefill can no longer die with a synthetic ReadTimeout.
    assert captured["timeout"] == (service.prefill_timeout[0], None)
