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

        # vLLM resolves max_tokens and max_completion_tokens into one effective
        # generation limit, with max_completion_tokens taking precedence. The
        # router must govern the same field the server will actually honor, so
        # admission, body defaults and PD decode budgets all follow vLLM.
        effective_token_key = (
            "max_completion_tokens"
            if self._safe_int(data.get("max_completion_tokens")) is not None
            else "max_tokens"
        )

        # max_tokens and stream_options are chat-completions parameters, so only
        # inject these defaults there.
        if self._is_chat_completions_path(path):
            if stream:
                options = data.get("stream_options")
                if not isinstance(options, dict):
                    options = {}
                options["include_usage"] = True
                data["stream_options"] = options

            if data.get(effective_token_key) is None:
                data[effective_token_key] = self.default_max_tokens
            elif not is_vip:
                existing = self._safe_int(data.get(effective_token_key))
                if existing is not None and existing < self.default_max_tokens:
                    data[effective_token_key] = self.default_max_tokens

        max_tokens = self._safe_int(data.get(effective_token_key))

        model_name = data.get("model") if isinstance(data.get("model"), str) else None
        # Fast estimate (no model needed) gives a value for the estimate_tokens
        # column and small-request routing at parse time. The slow, real
        # tokenizer runs later in ProxyService after a model is selected and only
        # when tokenizer.enabled is on.
        estimated_full_body_tokens = fast_estimate_tokens(body_str)

        new_body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return ParsedRequest(
            body=new_body,
            model_name=model_name,
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
