import json

import pytest

from router.route_algorithm.prefix_cache_preble import PrefixCachePrebleServerChooser


@pytest.fixture(autouse=True)
def _restore_redis_client():
    saved = PrefixCachePrebleServerChooser._redis_client
    yield
    PrefixCachePrebleServerChooser._redis_client = saved


def _chooser(prefix_block_chars=128):
    return PrefixCachePrebleServerChooser(
        count_provider=lambda targets: {},
        prefix_block_chars=prefix_block_chars,
    )


def _body(**extra):
    return json.dumps(extra, ensure_ascii=False).encode("utf-8")


def _text(**extra):
    return PrefixCachePrebleServerChooser._text_from_body(_body(**extra))


def _common_prefix_chars(chooser, text_a, text_b) -> int:
    hashes_a = chooser._get_prefix_hashes(text_a)
    hashes_b = chooser._get_prefix_hashes(text_b)
    common = 0
    for (digest_a, end_a), (digest_b, _) in zip(hashes_a, hashes_b):
        if digest_a != digest_b:
            break
        common = end_a
    return common


def test_messages_only_body_matches_legacy_format():
    text = _text(messages=[
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "hello"},
    ])
    assert text == "system: sys prompt\nuser: hello"


def test_tools_section_changes_text_and_first_block():
    messages = [{"role": "user", "content": "hello"}]
    tools_a = [{"type": "function", "function": {"name": "read", "parameters": {"path": "str"}}}]
    tools_b = [{"type": "function", "function": {"name": "write", "parameters": {"path": "str"}}}]

    text_a = _text(messages=messages, tools=tools_a)
    text_b = _text(messages=messages, tools=tools_b)

    assert text_a != text_b
    chooser = _chooser()
    assert chooser._get_prefix_hashes(text_a)[0][0] != chooser._get_prefix_hashes(text_b)[0][0]


def test_tool_drift_breaks_prefix_instead_of_reporting_full_match():
    # Same messages, one tool description differs -> the old hasher saw two
    # identical texts (ratio 1.0); now the divergence must land inside the
    # hashed text, so the chained prefix breaks before the end.
    chooser = _chooser()
    system = "You are a coding agent. " * 20
    tools_a = [
        {"type": "function", "function": {"name": f"tool_{i}", "description": f"tool number {i} " + "x" * 80}}
        for i in range(3)
    ]
    tools_b = json.loads(json.dumps(tools_a))
    tools_b[1]["function"]["description"] = "changed description " + "y" * 80
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "please continue the session"},
    ]

    text_a = _text(messages=messages, tools=tools_a)
    text_b = _text(messages=messages, tools=tools_b)

    assert chooser._get_prefix_hashes(text_a)[0][0] == chooser._get_prefix_hashes(text_b)[0][0]
    common = _common_prefix_chars(chooser, text_a, text_b)
    assert common < len(text_b)

    # Sanity: identical bodies still share the whole prefix.
    assert _common_prefix_chars(chooser, text_a, text_a) == len(text_a)


def test_empty_content_assistant_tool_calls_are_hashed():
    with_calls = _text(messages=[
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "list", "arguments": {"path": "."}}},
        ]},
    ])
    without_calls = _text(messages=[
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": None},
    ])
    assert "assistant: <tool_call>list({\"path\": \".\"})" in with_calls
    assert with_calls != without_calls


def test_tool_call_argument_drift_changes_text():
    base = {"role": "assistant", "content": None, "tool_calls": [
        {"function": {"name": "read", "arguments": {"path": "a.py"}}},
    ]}
    other = {"role": "assistant", "content": None, "tool_calls": [
        {"function": {"name": "read", "arguments": {"path": "b.py"}}},
    ]}
    assert _text(messages=[base]) != _text(messages=[other])


def test_mcp_servers_and_tool_choice_are_ignored():
    messages = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "read"}}]
    plain = _text(messages=messages, tools=tools)
    with_extras = _text(
        messages=messages,
        tools=tools,
        mcp_servers=["weather"],
        tool_choice="auto",
        parallel_tool_calls=False,
        temperature=0.3,
    )
    assert plain == with_extras


def test_reasoning_effort_changes_text():
    messages = [{"role": "user", "content": "hi"}]
    assert _text(messages=messages) != _text(messages=messages, reasoning_effort="high")


def test_response_format_changes_text():
    messages = [{"role": "user", "content": "hi"}]
    fmt = {"type": "json_object"}
    assert _text(messages=messages) != _text(messages=messages, response_format=fmt)


def test_non_text_content_part_is_marked():
    with_image = _text(messages=[{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "http://x"}},
    ]}])
    text_only = _text(messages=[{"role": "user", "content": [{"type": "text", "text": "look"}]}])
    assert "<part:image_url>" in with_image
    assert with_image != text_only


def test_empty_tools_list_matches_absent_tools():
    messages = [{"role": "user", "content": "hi"}]
    assert _text(messages=messages, tools=[]) == _text(messages=messages)


def test_tool_key_order_matters():
    messages = [{"role": "user", "content": "hi"}]
    tools_a = [{"function": {"name": "read", "description": "d"}}]
    tools_b = [{"function": {"description": "d", "name": "read"}}]
    # Templates render tool.items() in client key order, so both orders hash
    # differently: the hasher must not sort keys.
    assert _text(messages=messages, tools=tools_a) != _text(messages=messages, tools=tools_b)


def test_prompt_field_still_supported():
    assert _text(prompt="abcdef") == "abcdef"


def test_non_json_body_falls_back_to_raw_text():
    assert PrefixCachePrebleServerChooser._text_from_body(b"raw prompt") == "raw prompt"
