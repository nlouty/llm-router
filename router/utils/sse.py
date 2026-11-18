from __future__ import annotations

import json


def find_sse_usage(chunks: bytes | list[bytes] | str) -> dict | None:
    """The last ``usage`` object carried by an SSE stream, or None when no
    data frame contained one. Issue #353 needs "reported zero output tokens"
    distinguishable from "no usage frame at all" (the latter is not
    evaluable, e.g. a server that ignores stream_options.include_usage)."""
    if isinstance(chunks, list):
        raw = b"".join(chunks).decode("utf-8", errors="ignore")
    elif isinstance(chunks, bytes):
        raw = chunks.decode("utf-8", errors="ignore")
    else:
        raw = chunks

    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            return obj["usage"]
    return None


def parse_sse_usage(chunks: bytes | list[bytes] | str) -> tuple[int, int, int]:
    usage = find_sse_usage(chunks)
    if not usage:
        return 0, 0, 0
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details")
    cached_tokens = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    return prompt_tokens, completion_tokens, cached_tokens
