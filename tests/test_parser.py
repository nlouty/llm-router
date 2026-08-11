import json

import pytest

from router.models import Model
from router.services.parser import RequestParser
from router.utils import tokenizer_count


class _FakeTokenizer:
    is_fast = True

    def encode(self, text, add_special_tokens=True):
        return [1] * len(text)


def test_parser_injects_stream_options_and_default_max_tokens():
    parsed = RequestParser(default_max_tokens=8528).parse(b'{"model":"m1","stream":true}', "chat/completions")
    data = json.loads(parsed.body.decode())
    assert parsed.model_name == "m1"
    assert parsed.stream is True
    assert parsed.max_tokens == 8528
    # No model_path configured for m1: counting degrades to 0.
    assert parsed.estimated_full_body_tokens == 0
    assert parsed.tokenizer_latency_ms == 0
    assert data["stream_options"] == {"include_usage": True}
    assert data["max_tokens"] == 8528


@pytest.mark.django_db
def test_parser_counts_with_tokenizer_when_model_path_set(monkeypatch):
    Model.objects.create(model_name="m1", model_path="/tmp/fake-path")
    monkeypatch.setattr(tokenizer_count, "_get_tokenizer", lambda path: _FakeTokenizer())
    parsed = RequestParser().parse(b'{"model":"m1","messages":[{"role":"user","content":"hi"}]}')
    assert parsed.estimated_full_body_tokens > 0
    assert parsed.tokenizer_latency_ms >= 0


def test_parser_leaves_non_json_unchanged():
    parsed = RequestParser().parse(b"not-json")
    assert parsed.body == b"not-json"
    assert parsed.is_json is False
    assert parsed.model_name is None
    assert parsed.estimated_full_body_tokens >= 0


def test_parser_does_not_inject_chat_params_for_embeddings():
    parsed = RequestParser(default_max_tokens=8528).parse(
        b'{"model":"m1","input":"hello"}', "embeddings"
    )
    data = json.loads(parsed.body.decode())
    assert "max_tokens" not in data
    assert "stream_options" not in data
    assert parsed.max_tokens is None
    assert parsed.stream is False
    assert data["input"] == "hello"


def test_parser_bumps_up_max_tokens_when_below_default():
    parsed = RequestParser(default_max_tokens=28528).parse(
        b'{"model":"m1","max_tokens":1000}', "chat/completions"
    )
    data = json.loads(parsed.body.decode())
    assert parsed.max_tokens == 28528
    assert data["max_tokens"] == 28528


def test_parser_does_not_bump_up_max_tokens_for_vip():
    parsed = RequestParser(default_max_tokens=28528).parse(
        b'{"model":"m1","max_tokens":1000}', "chat/completions", is_vip=True
    )
    data = json.loads(parsed.body.decode())
    assert parsed.max_tokens == 1000
    assert data["max_tokens"] == 1000


def test_parser_does_not_bump_up_when_max_tokens_above_default():
    parsed = RequestParser(default_max_tokens=28528).parse(
        b'{"model":"m1","max_tokens":30000}', "chat/completions"
    )
    data = json.loads(parsed.body.decode())
    assert parsed.max_tokens == 30000
    assert data["max_tokens"] == 30000


def test_parser_does_not_bump_up_when_max_tokens_equal_default():
    parsed = RequestParser(default_max_tokens=28528).parse(
        b'{"model":"m1","max_tokens":28528}', "chat/completions"
    )
    data = json.loads(parsed.body.decode())
    assert parsed.max_tokens == 28528
    assert data["max_tokens"] == 28528
