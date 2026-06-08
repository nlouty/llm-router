import json

from router.services.parser import RequestParser
from router.utils.token_count import fast_estimate_tokens


def test_parser_injects_stream_options_and_default_max_tokens():
    parsed = RequestParser(default_max_tokens=8528).parse(b'{"model":"m1","stream":true}')
    data = json.loads(parsed.body.decode())
    assert parsed.model_name == "m1"
    assert parsed.stream is True
    assert parsed.max_tokens == 8528
    assert data["stream_options"] == {"include_usage": True}
    assert data["max_tokens"] == 8528


def test_parser_leaves_non_json_unchanged():
    parsed = RequestParser().parse(b"not-json")
    assert parsed.body == b"not-json"
    assert parsed.is_json is False
    assert parsed.model_name is None


def test_parser_estimates_tokens_from_full_json_body():
    body = json.dumps({
        "model": "user-model",
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "expensive_tool",
                            "arguments": "x" * 20000,
                        },
                    },
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "expensive_tool",
                    "description": "d" * 20000,
                },
            },
        ],
    }).encode("utf-8")

    parsed = RequestParser().parse(body)

    assert parsed.estimated_input_tokens == fast_estimate_tokens(parsed.body.decode("utf-8"))
    assert parsed.estimated_input_tokens > 3000
    assert fast_estimate_tokens("hi") < 3000
