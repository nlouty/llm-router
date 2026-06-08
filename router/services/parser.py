from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from router.utils.token_count import fast_estimate_tokens


@dataclass
class ParsedRequest:
    body: bytes
    model_name: str | None
    stream: bool
    max_tokens: int | None
    is_json: bool
    estimated_input_tokens: int = 0


class RequestParser:
    def __init__(self, default_max_tokens: int = 8528):
        self.default_max_tokens = default_max_tokens

    def parse(self, body: bytes) -> ParsedRequest:
        if not body:
            return ParsedRequest(body=body, model_name=None, stream=False, max_tokens=None, is_json=False)
        try:
            body_str = body.decode("utf-8")
            data = json.loads(body_str)
        except (UnicodeDecodeError, json.JSONDecodeError):
            # For non-JSON or decode error, estimate from raw body if it's text-like
            est_tokens = 0
            try:
                est_tokens = fast_estimate_tokens(body.decode("utf-8"))
            except Exception:
                pass
            return ParsedRequest(body=body, model_name=None, stream=False, max_tokens=None, is_json=False, estimated_input_tokens=est_tokens)

        if not isinstance(data, dict):
            return ParsedRequest(
                body=body,
                model_name=None,
                stream=False,
                max_tokens=None,
                is_json=True,
                estimated_input_tokens=fast_estimate_tokens(body_str),
            )

        stream = bool(data.get("stream"))
        if stream:
            options = data.get("stream_options")
            if not isinstance(options, dict):
                options = {}
            options["include_usage"] = True
            data["stream_options"] = options

        if data.get("max_tokens") is None:
            data["max_tokens"] = self.default_max_tokens

        max_tokens = self._safe_int(data.get("max_tokens"))
        
        body_text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        estimated_input_tokens = fast_estimate_tokens(body_text)
        new_body = body_text.encode("utf-8")
        return ParsedRequest(
            body=new_body,
            model_name=data.get("model") if isinstance(data.get("model"), str) else None,
            stream=stream,
            max_tokens=max_tokens,
            is_json=True,
            estimated_input_tokens=estimated_input_tokens,
        )

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
