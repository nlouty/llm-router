from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import requests

from router.config import APP_CONFIG
from router.repositories.models import ModelRepository
from router.repositories.servers import ServerRepository
from router.repositories.requests import RequestRepository
from router.services.request_logger import append_request_log
from router.route_algorithm.base import ServerSelectionContext


class ModelResolver:
    SMALL_REQUEST_ROUTING_TOKEN_LIMIT = 3000

    def __init__(self, chooser):
        self.chooser = chooser
        self._router_system_prompt = None

    def resolve(self, parsed, record, context: ServerSelectionContext, model: Any) -> tuple[Any, str | None]:
        # 1. Small request routing
        if self._should_route_small_request(parsed):
            routing_model = self._get_small_request_routing_model(parsed.estimated_full_body_tokens)
            if routing_model is not None:
                self._apply_resolved_model(parsed, record, context, routing_model, disable_thinking=True)
                return routing_model, "small_request_routing"

        # 2. Auto model resolution
        if parsed.model_name == "auto":
            model, router_result = self._get_auto_route_model(parsed.body, record, context)
            if model:
                self._apply_resolved_model(parsed, record, context, model)
            return model, router_result

        # 3. Non-auto routing result (complexity analysis for fixed model)
        if model is not None:
            cached_model = self._check_cache_hit(parsed.body, [model], [model.model_name])
            if cached_model:
                return model, "cache_hit"

            _, router_result = self._query_routing_complexity(
                parsed.body,
                record,
                context,
                [model.model_name],
            )
            return model, router_result

        return model, None

    def _should_route_small_request(self, parsed) -> bool:
        return int(parsed.estimated_full_body_tokens or 0) < self.SMALL_REQUEST_ROUTING_TOKEN_LIMIT

    def _get_small_request_routing_model(self, estimate_tokens: int = 0):
        for routing_model in ModelRepository.get_routing_models():
            candidates = ServerRepository.list_by_model_id(routing_model.id, vip=False, estimate_tokens=estimate_tokens)
            if candidates:
                return routing_model
        return None

    def _apply_resolved_model(self, parsed, record, context: ServerSelectionContext, model, disable_thinking: bool = False) -> None:
        record.model_id = model.id
        record.save(update_fields=["model_id"])
        parsed.model_name = model.model_name
        parsed.body = self._update_body_model(parsed.body, model.model_name, disable_thinking=disable_thinking)
        context.model_id = model.id
        context.model_name = model.model_name
        context.body = parsed.body

    def _update_body_model(self, body: bytes, model_name: str, disable_thinking: bool = False) -> bytes:
        try:
            body_data = json.loads(body.decode("utf-8"))
            body_data["model"] = model_name
            if disable_thinking:
                self._disable_thinking(body_data)
            return json.dumps(body_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except Exception:
            return body

    @staticmethod
    def _disable_thinking(body_data: dict[str, Any]) -> None:
        chat_template_kwargs = body_data.get("chat_template_kwargs")
        if not isinstance(chat_template_kwargs, dict):
            chat_template_kwargs = {}
        chat_template_kwargs["enable_thinking"] = False
        body_data["chat_template_kwargs"] = chat_template_kwargs

    def _get_auto_route_model(self, body: bytes, record: Any, context: ServerSelectionContext) -> tuple[Any, str | None]:
        auto_models = ModelRepository.list_auto_selectable_models()
        if not auto_models:
            return None, self._routing_unavailable_result(
                "missing_target_model",
                "no auto-selectable target model for auto request",
            )

        model_names = [m.model_name for m in auto_models]

        cached_model = self._check_cache_hit(body, auto_models, model_names)
        if cached_model:
            return cached_model, "cache_hit"

        return self._query_routing_llm(body, record, context, auto_models, model_names)

    def _check_cache_hit(self, body: bytes, active_models: list[Any], model_names: list[str]) -> Any | None:
        chooser = self.chooser
        if hasattr(chooser, "get_all_model_prefix_ratios"):
            ratios = chooser.get_all_model_prefix_ratios(body, model_names)
            if ratios:
                best_name = max(ratios, key=ratios.get)
                if ratios[best_name] > 0.9:
                    return next((m for m in active_models if m.model_name == best_name), None)
        return None

    def _query_routing_llm(self, body: bytes, record: Any, context: ServerSelectionContext, active_models: list[Any], model_names: list[str]) -> tuple[Any, str | None]:
        complexity, router_result = self._query_routing_complexity(body, record, context, model_names)
        if complexity is None:
            return self._get_default_model(), router_result

        matched = self._models_for_complexity(active_models, complexity)
        if len(matched) == 1:
            return matched[0], router_result
        if len(matched) > 1:
            return self._get_default_model(), self._multiple_models_for_complexity_result(complexity, matched)

        return self._get_default_model(), self._no_model_for_complexity_result(complexity)

    def _query_routing_complexity(self, body: bytes, record: Any, context: ServerSelectionContext, model_names: list[str] | None = None) -> tuple[int | None, str | None]:
        routing_models = ModelRepository.get_routing_models()
        if not routing_models:
            return None, self._routing_unavailable_result(
                "missing_routing_model",
                "no routing model configured",
            )

        routing_servers = []
        model_id_to_name = {rm.id: rm.model_name for rm in routing_models}
        for rm in routing_models:
            routing_servers.extend(ServerRepository.list_by_model_id(rm.id, vip=False, estimate_tokens=0))

        if not routing_servers:
            return None, self._routing_unavailable_result()

        server = self.chooser.choose(routing_servers, context, set()) or random.choice(routing_servers)

        self._ensure_system_prompt(model_names)
        routing_model_name = model_id_to_name.get(server.model_id, "router")

        payload = self._build_routing_payload(routing_model_name, body)

        url = server.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if server.csb_token:
            headers["csb-token"] = server.csb_token

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
        except Exception as e:
            router_result = self._routing_exception_result(e)
            self._safe_append_request_log(record.id, f"Routing LLM error: {str(e)}")
            return None, router_result

        if resp.status_code != 200:
            return None, self._routing_response_error_result(resp)

        try:
            result = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            router_result = self._routing_exception_result(e, status_code=resp.status_code)
            self._safe_append_request_log(record.id, f"Routing LLM error: {str(e)}")
            return None, router_result

        complexity = self._routing_complexity(result)
        if complexity is None:
            return None, self._invalid_routing_result(result)

        return complexity, self._complexity_routing_result(complexity)

    def _routing_response_error_result(self, response) -> str:
        status_code = getattr(response, "status_code", None)
        content = self._response_content_bytes(response)
        reason = self._response_reason(response)
        message = self._extract_fail_reason(content, reason or "routing request failed")
        return self._format_router_result("routing_failed", status_code, message)

    def _routing_exception_result(self, exc: Exception, status_code: int | None = None) -> str:
        return self._format_router_result("routing_error", status_code, str(exc))

    def _routing_unavailable_result(
        self,
        code: str = "missing_routing_server",
        message: str = "no available routing server",
    ) -> str:
        return self._format_router_result("routing_failed", code, message)

    def _invalid_routing_result(self, result: str) -> str:
        detail = self._compact_router_message(result) or "empty routing result"
        return self._format_router_result(
            "routing_failed",
            "invalid_routing_result",
            f"router returned no valid complexity: {detail}",
        )

    def _no_model_for_complexity_result(self, complexity: int) -> str:
        return self._format_router_result(
            "routing_failed",
            "no_model_for_complexity",
            f"complexity {complexity} has no matching auto-selectable model",
        )

    def _multiple_models_for_complexity_result(self, complexity: int, models: list[Any]) -> str:
        model_names = ",".join(str(model.model_name) for model in models)
        return self._format_router_result(
            "routing_failed",
            "multiple_models_for_complexity",
            f"complexity {complexity} matched multiple auto-selectable models: {model_names}",
        )

    @staticmethod
    def _complexity_routing_result(complexity: int) -> str:
        return f"complexity:{complexity}"

    @classmethod
    def _routing_complexity(cls, result: str) -> int | None:
        text = str(result or "")
        try:
            parsed = json.loads(cls._strip_json_fence(text))
        except (TypeError, json.JSONDecodeError):
            return cls._extract_complexity_number(text)

        value = parsed.get("complexity") if isinstance(parsed, dict) else parsed
        complexity = cls._complexity_from_value(value)
        if complexity is not None:
            return complexity
        return cls._extract_complexity_number(text)

    @staticmethod
    def _complexity_from_value(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            complexity = value
        elif isinstance(value, str) and value.strip().isdigit():
            complexity = int(value.strip())
        else:
            return None
        return complexity if 1 <= complexity <= 10 else None

    @staticmethod
    def _extract_complexity_number(text: str) -> int | None:
        for match in re.finditer(r"(?<![\d.])(10|[1-9])(?!\.\d)(?!\d)", str(text or "")):
            return int(match.group(1))
        return None

    @staticmethod
    def _models_for_complexity(models: list[Any], complexity: int) -> list[Any]:
        return [
            model for model in models
            if model.complexity_min is not None
            and model.complexity_max is not None
            and model.complexity_min <= complexity <= model.complexity_max
        ]

    @staticmethod
    def _strip_json_fence(result: str) -> str:
        text = str(result or "").strip()
        if not text.startswith("```"):
            return text
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @staticmethod
    def _format_router_result(prefix: str, status_code: int | str | None, message: str) -> str:
        code = str(status_code) if status_code is not None else "exception"
        detail = ModelResolver._compact_router_message(message)
        return f"{prefix}:{code}:{detail}"[:300]

    @staticmethod
    def _compact_router_message(message: Any) -> str:
        return " ".join(str(message or "").split())

    @staticmethod
    def _response_content_bytes(response) -> bytes:
        content = getattr(response, "content", b"")
        if isinstance(content, str):
            return content.encode("utf-8", errors="replace")
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        text = getattr(response, "text", "")
        if isinstance(text, str):
            return text.encode("utf-8", errors="replace")
        return b""

    @staticmethod
    def _response_reason(response) -> str:
        reason = getattr(response, "reason", "")
        if isinstance(reason, str):
            return reason
        text = getattr(response, "text", "")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _safe_append_request_log(request_id: int, message: str) -> None:
        try:
            append_request_log(request_id, message)
        except Exception:
            pass

    def _ensure_system_prompt(self, model_names: list[str]) -> None:
        if self._router_system_prompt is None:
            prompt_path = APP_CONFIG.get("router", {}).get("system_prompt_path", "router/assets/router_system_prompt.md")
            try:
                with open(prompt_path, "r") as f:
                    self._router_system_prompt = f.read()
            except Exception:
                self._router_system_prompt = (
                    "You are an LLM request complexity classifier. "
                    'Return only compact JSON like {"complexity":5}, '
                    "where complexity is an integer from 1 to 10."
                )

    def _build_routing_payload(self, model_name: str, body: bytes) -> dict[str, Any]:
        payload = {
            "model": model_name,
            "messages": self._routing_messages_from_body(body),
            "stream": False,
        }
        self._disable_thinking(payload)
        return payload

    def _routing_messages_from_body(self, body: bytes) -> list[dict[str, Any]]:
        messages = [{"role": "system", "content": self._router_system_prompt}]
        messages.extend(self._user_messages_from_body(body))
        return messages

    @staticmethod
    def _user_messages_from_body(body: bytes) -> list[dict[str, Any]]:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict):
            return []

        source_messages = data.get("messages")
        if not isinstance(source_messages, list):
            return []

        user_messages: list[dict[str, Any]] = []
        for message in source_messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            if "content" not in message:
                continue
            user_messages.append({"role": "user", "content": message["content"]})
        return user_messages

    def _get_default_model(self) -> Any:
        name = APP_CONFIG.get("router", {}).get("fallback_model", "DeepSeek-V4-Flash")
        return ModelRepository.get_by_name(name)

    @staticmethod
    def _extract_fail_reason(content: bytes, http_reason: str) -> str:
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return http_reason
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                msg = error.get("message", "")
                err_type = error.get("type", "")
                if msg:
                    return f"{err_type}: {msg}" if err_type else msg
        return http_reason

    def context_overflow_switch(self, record, context, body, model, status_code, fail_reason) -> tuple[Any | None, bytes]:
        if context.origin_model_name != "auto":
            return None, body
        if not model or model.model_name == "DeepSeek-V4-Flash":
            return None, body
        if not self._check_context_overflow(status_code, model, fail_reason):
            return None, body

        flash_model = ModelRepository.get_by_name("DeepSeek-V4-Flash")
        if not flash_model:
            return None, body

        append_request_log(record.id, f"Context overflow detected ({fail_reason}), switching to DeepSeek-V4-Flash")
        body = self._update_body_model(body, flash_model.model_name)
        context.model_id = flash_model.id
        context.model_name = flash_model.model_name
        context.body = body
        return flash_model, body

    def _check_context_overflow(self, status_code: int, model: Any, fail_reason: str) -> bool:
        if status_code == 400 and model and model.max_context_window:
            return str(model.max_context_window) in fail_reason
        return False
