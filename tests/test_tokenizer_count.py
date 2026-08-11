import sys
from types import SimpleNamespace

import pytest

from router.utils import tokenizer_count
from router.utils.tokenizer_count import count_tokens_with_latency


@pytest.fixture(autouse=True)
def _reset_tokenizer_state():
    tokenizer_count._tokenizers.clear()
    tokenizer_count._failed_paths.clear()
    tokenizer_count._import_failed = False
    yield


class FakeTokenizer:
    is_fast = True

    def __init__(self, tokens=(1,)):
        self.tokens = tokens

    def encode(self, text, add_special_tokens=True):
        if text == "boom":
            raise ValueError("encode failed")
        return list(self.tokens) * len(text)


def test_empty_path_or_text_counts_zero():
    assert count_tokens_with_latency(None, "hi") == (0, 0)
    assert count_tokens_with_latency("path", "") == (0, 0)


def test_counts_text_and_reports_latency(monkeypatch):
    monkeypatch.setattr(tokenizer_count, "_get_tokenizer", lambda path: FakeTokenizer())
    count, latency = count_tokens_with_latency("p", "hello")
    assert count == 5
    assert latency >= 0


def test_encode_failure_counts_zero(monkeypatch):
    monkeypatch.setattr(tokenizer_count, "_get_tokenizer", lambda path: FakeTokenizer())
    assert count_tokens_with_latency("p", "boom") == (0, 0)


def test_unloadable_path_counts_zero(monkeypatch):
    monkeypatch.setattr(tokenizer_count, "_get_tokenizer", lambda path: None)
    assert count_tokens_with_latency("p", "hello") == (0, 0)


def test_tokenizer_loaded_once_per_path(monkeypatch):
    calls = []
    transformers = SimpleNamespace(
        AutoTokenizer=SimpleNamespace(
            from_pretrained=lambda path, use_fast: (calls.append(path), FakeTokenizer())[1]
        )
    )
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    first = tokenizer_count._get_tokenizer("p")
    second = tokenizer_count._get_tokenizer("p")
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
    assert tokenizer_count._get_tokenizer("p") is not None
    assert calls == [True, False]


def test_failed_path_not_retried(monkeypatch):
    calls = []

    def from_pretrained(path, use_fast):
        calls.append(use_fast)
        raise OSError("always fails")

    transformers = SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained))
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    assert tokenizer_count._get_tokenizer("p") is None
    assert tokenizer_count._get_tokenizer("p") is None
    # fast+slow once per path, never retried on subsequent calls
    assert len(calls) == 2


def test_missing_transformers_disables_counting(monkeypatch):
    monkeypatch.setitem(sys.modules, "transformers", None)
    assert tokenizer_count._get_tokenizer("p") is None
    assert tokenizer_count._import_failed is True
    # Short-circuits without retrying the import
    assert tokenizer_count._get_tokenizer("p") is None
