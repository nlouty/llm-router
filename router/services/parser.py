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
    estimated_full_body_tokens: int = 0


class RequestParser:
    def __init__(self, default_max_tokens: int = 28528):
        self.default_max_tokens = default_max_tokens

    def parse(self, body: bytes, path: str = "", *, is_vip: bool = False) -> ParsedRequest:
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
            return ParsedRequest(body=body, model_name=None, stream=False, max_tokens=None, is_json=False, estimated_full_body_tokens=est_tokens)

        if not isinstance(data, dict):
            return ParsedRequest(body=body, model_name=None, stream=False, max_tokens=None, is_json=True)

        stream = bool(data.get("stream"))

        # max_tokens and stream_options are chat-completions parameters, so only
        # inject these defaults there.
        if self._is_chat_completions_path(path):
            if stream:
                options = data.get("stream_options")
                if not isinstance(options, dict):
                    options = {}
                options["include_usage"] = True
                data["stream_options"] = options

            if data.get("max_tokens") is None:
                data["max_tokens"] = self.default_max_tokens
            elif not is_vip:
                existing = self._safe_int(data.get("max_tokens"))
                if existing is not None and existing < self.default_max_tokens:
                    data["max_tokens"] = self.default_max_tokens

        max_tokens = self._safe_int(data.get("max_tokens"))

        estimated_full_body_tokens = fast_estimate_tokens(body_str)

        new_body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return ParsedRequest(
            body=new_body,
            model_name=data.get("model") if isinstance(data.get("model"), str) else None,
            stream=stream,
            max_tokens=max_tokens,
            is_json=True,
            estimated_full_body_tokens=estimated_full_body_tokens,
        )

    @staticmethod
    def _is_chat_completions_path(path: str) -> bool:
        return path.rstrip("/").lower() == "chat/completions"

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
