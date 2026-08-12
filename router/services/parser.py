from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from router.repositories.models import ModelRepository
from router.utils.tokenizer_count import count_tokens_with_latency


@dataclass
class ParsedRequest:
    body: bytes
    model_name: str | None
    stream: bool
    max_tokens: int | None
    is_json: bool
    estimated_full_body_tokens: int = 0
    tokenizer_latency_ms: int = 0
    tokenizer_error: str | None = None


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
            return ParsedRequest(body=body, model_name=None, stream=False, max_tokens=None, is_json=False)

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

        model_name = data.get("model") if isinstance(data.get("model"), str) else None
        model_path = ModelRepository.get_model_path(model_name)
        estimated_full_body_tokens, tokenizer_latency_ms, tokenizer_error = count_tokens_with_latency(
            model_path, body_str
        )

        new_body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return ParsedRequest(
            body=new_body,
            model_name=model_name,
            stream=stream,
            max_tokens=max_tokens,
            is_json=True,
            estimated_full_body_tokens=estimated_full_body_tokens,
            tokenizer_latency_ms=tokenizer_latency_ms,
            tokenizer_error=tokenizer_error,
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
