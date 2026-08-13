import sys
from types import SimpleNamespace

import pytest

from router.utils import token_count
from router.utils.token_count import count_tokens_with_latency


@pytest.fixture(autouse=True)
def _reset_tokenizer_state():
    token_count._tokenizers.clear()
    token_count._failed_paths.clear()
    token_count._load_errors.clear()
    token_count._import_failed = False
    token_count._import_error = None
    yield


class FakeTokenizer:
    is_fast = True

    def __init__(self, tokens=(1,)):
        self.tokens = tokens

    def encode(self, text, add_special_tokens=True):
        if text == "boom":
            raise ValueError("encode failed")
        return list(self.tokens) * len(text)


def test_empty_path_or_text_counts_zero_with_reason():
    count, latency, reason = count_tokens_with_latency(None, "hi")
    assert (count, latency) == (0, 0)
    assert reason == "no model_path configured for model"

    count, latency, reason = count_tokens_with_latency("path", "")
    assert (count, latency) == (0, 0)
    assert reason == "empty request body"


def test_counts_text_and_reports_latency(monkeypatch):
    monkeypatch.setattr(token_count, "_get_tokenizer", lambda path: FakeTokenizer())
    count, latency, reason = count_tokens_with_latency("p", "hello")
    assert count == 5
    assert latency >= 0
    assert reason is None


def test_encode_failure_counts_zero_with_reason(monkeypatch):
    monkeypatch.setattr(token_count, "_get_tokenizer", lambda path: FakeTokenizer())
    count, latency, reason = count_tokens_with_latency("p", "boom")
    assert (count, latency) == (0, 0)
    assert reason == "tokenizer encode failed for model_path=p: encode failed"


def test_unloadable_path_counts_zero_with_reason(monkeypatch):
    monkeypatch.setattr(token_count, "_get_tokenizer", lambda path: None)
    count, latency, reason = count_tokens_with_latency("p", "hello")
    assert (count, latency) == (0, 0)
    assert reason == "tokenizer load failed for model_path=p: unknown error"


def test_failed_load_captures_error_message(monkeypatch):
    def from_pretrained(path, use_fast):
        raise OSError("repo not found")

    transformers = SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained))
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    count, latency, reason = count_tokens_with_latency("p", "hello")
    assert (count, latency) == (0, 0)
    assert reason == "tokenizer load failed for model_path=p: repo not found"
    # Subsequent requests reuse the cached failure reason without reloading.
    assert count_tokens_with_latency("p", "hello")[2] == reason


def test_missing_transformers_reports_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "transformers", None)
    count, latency, reason = count_tokens_with_latency("p", "hello")
    assert (count, latency) == (0, 0)
    assert reason.startswith("transformers unavailable:")


def test_tokenizer_loaded_once_per_path(monkeypatch):
    calls = []
    transformers = SimpleNamespace(
        AutoTokenizer=SimpleNamespace(
            from_pretrained=lambda path, use_fast: (calls.append(path), FakeTokenizer())[1]
        )
    )
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    first = token_count._get_tokenizer("p")
    second = token_count._get_tokenizer("p")
    assert first is second
    assert calls == ["p"]


def test_fast_then_slow_fallback(monkeypatch):
    calls = []

    def from_pretrained(path, use_fast):
        calls.append(use_fast)
        if use_fast:
            raise OSError("no fast tokenizer")
        return FakeTokenizer()

    transformers = SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained))
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    assert token_count._get_tokenizer("p") is not None
    assert calls == [True, False]


def test_failed_path_not_retried(monkeypatch):
    calls = []

    def from_pretrained(path, use_fast):
        calls.append(use_fast)
        raise OSError("always fails")

    transformers = SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained))
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    assert token_count._get_tokenizer("p") is None
    assert token_count._get_tokenizer("p") is None
    # fast+slow once per path, never retried on subsequent calls
    assert len(calls) == 2
    assert token_count._load_errors["p"] == "always fails"


def test_missing_transformers_disables_counting(monkeypatch):
    monkeypatch.setitem(sys.modules, "transformers", None)
    assert token_count._get_tokenizer("p") is None
    assert token_count._import_failed is True
    assert token_count._import_error is not None
    # Short-circuits without retrying the import
    assert token_count._get_tokenizer("p") is None
