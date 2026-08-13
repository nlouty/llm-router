import json

from router.services.parser import RequestParser


def test_parser_injects_stream_options_and_default_max_tokens():
    parsed = RequestParser(default_max_tokens=8528).parse(b'{"model":"m1","stream":true}', "chat/completions")
    data = json.loads(parsed.body.decode())
    assert parsed.model_name == "m1"
    assert parsed.stream is True
    assert parsed.max_tokens == 8528
    # Fast estimate runs over the body; the stream body is short but nonzero.
    assert parsed.estimated_full_body_tokens > 0
    assert parsed.tokenizer_latency_ms == 0
    assert data["stream_options"] == {"include_usage": True}
    assert data["max_tokens"] == 8528


def test_parser_estimates_tokens_from_body():
    body = json.dumps({"model": "m1", "messages": [{"role": "user", "content": "Hello world, this is a test prompt to estimate tokens."}]}).encode()
    parsed = RequestParser().parse(body)
    # The fast heuristic estimates tokens from the full body (no model needed).
    assert parsed.estimated_full_body_tokens > 0


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
