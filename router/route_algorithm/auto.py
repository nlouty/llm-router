from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests

from router.config import APP_CONFIG
from router.repositories.models import ModelRepository
from router.repositories.requests import RequestRepository
from router.repositories.servers import ServerRepository
from router.route_algorithm.base import ServerSelectionContext
from router.route_algorithm.least_connection import LeastConnectionServerChooser
from router.services import proxy_logging, proxy_response
from router.utils.tokenizer_count import count_tokens_with_latency


@dataclass
class AutoRouteDecision:
    model: Any
    router_result: str | None = None


class AutoRouteAlgorithm:
    SMALL_REQUEST_ROUTING_TOKEN_LIMIT = 3000
    PREFIX_CACHE_AUTO_HIT_THRESHOLD = 0.7
    ROUTING_USER_PROMPT_CHAR_LIMIT = 500
    ROUTING_USER_PROMPT_COLLAPSE_MULTIPLIER = 3
    ROUTING_USER_PROMPT_MESSAGE_LIMIT = 20

    def __init__(self, chooser=None):
        self.chooser = chooser
        self.workload_chooser = LeastConnectionServerChooser.for_server_workload()
        self._router_system_prompt = None

    def should_auto_select(self, parsed, model, is_vip_channel: bool) -> bool:
        if ModelRepository.is_auto_model_name(parsed.model_name):
            return True
        if is_vip_channel:
            return False
        return ModelRepository.should_auto_select(model)

    def should_record_model_choice(
        self,
        parsed,
        is_vip_channel: bool,
        auto_model_selection: bool,
    ) -> bool:
        # Model-choice timing covers small-request routing and true auto
        # selection; both run only for auto requests (issue #227).
        return auto_model_selection

    def resolve(
        self,
        parsed,
        record,
        context: ServerSelectionContext,
        model,
        is_vip_channel: bool,
    ) -> AutoRouteDecision:
        origin_model_name = context.origin_model_name or parsed.model_name
        model, router_result = self._resolve_small_request_routing_model(
            parsed,
            record,
            context,
            model,
            is_vip_channel,
            origin_model_name,
        )
        if router_result is None:
            model, router_result = self._resolve_auto_model(
                parsed,
                record,
                context,
                model,
                context.auto_model_selection,
                origin_model_name,
            )
        context.router_result = router_result
        return AutoRouteDecision(model=model, router_result=router_result)

    @staticmethod
    def _router_result_with_origin(
        origin_model_name: str | None,
        router_result: str | None,
    ) -> str | None:
        # IMPORTANT: the origin model name is stored as the prefix of
        # router_result (everything before the first ":"). AdmissionService
        # reads this prefix to bucket in-flight requests by their entrance
        # model during concurrency checks. Do not remove or reorder this
        # prefix without updating check_concurrency.
        if not router_result or not origin_model_name:
            return router_result
        return f"{origin_model_name}:{router_result}"[:300]

    def _resolve_auto_model(
        self,
        parsed,
        record,
        context: ServerSelectionContext,
        model,
        auto_model_selection: bool,
        origin_model_name: str | None,
    ):
        if not auto_model_selection:
            return model, None

        model, router_result = self._get_auto_route_model(parsed.body, record, context)
        prefixed = self._router_result_with_origin(origin_model_name, router_result)
        if model:
            self._apply_resolved_model(parsed, record, context, model, prefixed)
            # The auto request was counted before any target was known (0); now
            # that a target model is resolved, count with its tokenizer.
            if getattr(model, "model_path", None):
                count, _, _ = count_tokens_with_latency(
                    model.model_path,
                    parsed.body.decode("utf-8", errors="replace"),
                )
                record.estimate_tokens = count
                record.save(update_fields=["estimate_tokens"])
        return model, prefixed

    def _resolve_small_request_routing_model(
        self,
        parsed,
        record,
        context: ServerSelectionContext,
        model,
        is_vip_channel: bool,
        origin_model_name: str | None,
    ):
        # Small-request routing only applies to auto requests (issue #227);
        # explicit model requests are never rewritten to the routing model.
        if (
            is_vip_channel
            or not context.auto_model_selection
            or not self.should_route_small_request(parsed)
        ):
            return model, None

        routing_model = self._get_small_request_routing_model()
        if routing_model is None:
            return model, None

        prefixed = self._router_result_with_origin(origin_model_name, "small_request_routing")
        self._apply_resolved_model(
            parsed,
            record,
            context,
            routing_model,
            prefixed,
            disable_thinking=True,
        )
        return routing_model, prefixed

    def should_route_small_request(self, parsed) -> bool:
        estimated = int(parsed.estimated_full_body_tokens or 0)
        # 0 means counting failed or no model_path: never send those requests to
        # the small-request routing model (issue #227).
        return 0 < estimated < self.SMALL_REQUEST_ROUTING_TOKEN_LIMIT

    @staticmethod
    def _get_small_request_routing_model():
        for routing_model in ModelRepository.get_routing_models():
            if ServerRepository.list_by_model_id(routing_model.id, vip=False):
                return routing_model
        return None

    def _apply_resolved_model(
        self,
        parsed,
        record,
        context: ServerSelectionContext,
        model,
        router_result: str | None = None,
        disable_thinking: bool = False,
    ) -> None:
        record.model_id = model.id
        record.router_result = router_result
        record.save(update_fields=["model_id", "router_result"])
        parsed.model_name = model.model_name
        parsed.body = self.update_body_model(
            parsed.body,
            model.model_name,
            disable_thinking=disable_thinking,
        )
        context.model_id = model.id
        context.model_name = model.model_name
        context.body = parsed.body

    def _get_auto_route_model(
        self,
        body: bytes,
        record: Any,
        context: ServerSelectionContext,
    ) -> tuple[Any, str | None]:
        if self._is_multimodal(body):
            model = ModelRepository.get_multimodal_model()
            if model:
                return model, "multimodal_bypass"

        auto_models = ModelRepository.list_auto_selectable_models()
        if not auto_models:
            return None, self._routing_unavailable_result(
                "missing_target_model",
                "no auto-routing target model for auto request",
            )

        model_names = [model.model_name for model in auto_models]

        cached_model = self._check_cache_hit(body, auto_models, model_names)
        if cached_model:
            return cached_model, "cache_hit"

        return self._query_routing_llm(
            body,
            record,
            context,
            auto_models,
            model_names,
        )

    def _is_multimodal(self, body: bytes) -> bool:
        try:
            data = json.loads(body.decode("utf-8"))
            for message in data.get("messages", []):
                if self._has_chat_image_part(message.get("content")):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _has_chat_image_part(content: Any) -> bool:
        if not isinstance(content, list):
            return False
        return any(
            isinstance(part, dict)
            and part.get("type") == "image_url"
            and bool(part.get("image_url"))
            for part in content
        )

    def _check_cache_hit(
        self,
        body: bytes,
        active_models: list[Any],
        model_names: list[str],
    ) -> Any | None:
        if self._user_prompt_count_from_body(body) == 1:
            return None

        chooser = self.chooser
        if hasattr(chooser, "get_all_model_prefix_ratios"):
            ratios = chooser.get_all_model_prefix_ratios(body, model_names)
            if ratios:
                cache_hits = []
                for model in active_models:
                    ratio = float(ratios.get(model.model_name) or 0.0)
                    if ratio > self.PREFIX_CACHE_AUTO_HIT_THRESHOLD:
                        cache_hits.append(model)
                if len(cache_hits) == 1:
                    return cache_hits[0]
        return None

    @staticmethod
    def _user_prompt_count_from_body(body: bytes) -> int:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 0
        if not isinstance(data, dict):
            return 0

        source_messages = data.get("messages")
        if not isinstance(source_messages, list):
            return 0

        return sum(
            1
            for message in source_messages
            if isinstance(message, dict) and message.get("role") == "user"
        )

    def _query_routing_llm(
        self,
        body: bytes,
        record: Any,
        context: ServerSelectionContext,
        active_models: list[Any],
        model_names: list[str],
    ) -> tuple[Any, str | None]:
        complexity, router_result = self._query_routing_complexity(
            body,
            record,
            context,
            model_names,
        )
        if complexity is None:
            return self._get_default_model(), router_result

        matched = self._models_for_complexity(active_models, complexity)
        if len(matched) == 1:
            return matched[0], router_result
        if len(matched) > 1:
            return (
                self._get_default_model(),
                self._multiple_models_for_complexity_result(complexity, matched),
            )

        return self._get_default_model(), self._no_model_for_complexity_result(
            complexity
        )

    def _query_routing_complexity(
        self,
        body: bytes,
        record: Any,
        context: ServerSelectionContext,
        model_names: list[str] | None = None,
    ) -> tuple[int | None, str | None]:
        routing_models = ModelRepository.get_routing_models()
        if not routing_models:
            return None, self._routing_unavailable_result(
                "missing_routing_model",
                "no routing model configured",
            )

        routing_servers = []
        model_id_to_name = {model.id: model.model_name for model in routing_models}
        for routing_model in routing_models:
            routing_servers.extend(
                ServerRepository.list_by_model_id(
                    routing_model.id,
                    vip=False,
                )
            )

        if not routing_servers:
            return None, self._routing_unavailable_result()

        server = self._choose_routing_server(routing_servers, context)

        self._ensure_system_prompt(model_names)
        routing_model_name = model_id_to_name.get(server.model_id, "router")

        payload = self._build_routing_payload(routing_model_name, body)
        if len(payload.get("messages", [])) <= 1:
            return None, "no_user_query"

        choosing_record = self._create_routing_request_record(server)
        url = self._build_url(server.base_url, "chat/completions", "")
        headers = {"Content-Type": "application/json"}
        csb_token = getattr(server, "csb_token", None)
        if csb_token:
            headers["csb-token"] = csb_token

        ServerRepository.increment_workload(server)
        try:
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
            except Exception as exc:
                self._finish_routing_request_record(
                    choosing_record,
                    502,
                    str(exc),
                    server,
                )
                router_result = self._routing_exception_result(exc)
                proxy_logging.safe_append_request_log(
                    record.id,
                    f"Routing LLM error: {str(exc)}",
                )
                return None, router_result

            if resp.status_code != 200:
                self._finish_routing_response_record(choosing_record, resp, server)
                return None, self._routing_response_error_result(resp)

            try:
                self._finish_routing_response_record(choosing_record, resp, server)
                result = (
                    resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
            except Exception as exc:
                router_result = self._routing_exception_result(
                    exc,
                    status_code=resp.status_code,
                )
                proxy_logging.safe_append_request_log(
                    record.id,
                    f"Routing LLM error: {str(exc)}",
                )
                return None, router_result

            complexity = self._routing_complexity(result)
            if complexity is None:
                return None, self._invalid_routing_result(result)

            return complexity, self._complexity_routing_result(complexity)
        finally:
            ServerRepository.decrement_workload(server)

    @staticmethod
    def _create_routing_request_record(server) -> Any:
        return RequestRepository.create_llm_choosing(
            model_id=server.model_id or 0,
            target_pod_ip=getattr(server, "base_url", None),
        )

    @staticmethod
    def _finish_routing_response_record(record, response, server) -> None:
        content = proxy_response.response_content_bytes(response)
        reason = proxy_response.response_reason(response)
        status_code = int(getattr(response, "status_code", 502) or 502)
        fail_reason = proxy_response.extract_fail_reason(
            content,
            reason or "routing request failed",
        )
        input_tokens, output_tokens, cached_tokens = proxy_response.parse_json_usage(
            content
        )
        RequestRepository.finish(
            record,
            status_code,
            fail_reason,
            input_tokens,
            output_tokens,
            getattr(server, "base_url", None),
            getattr(server, "model_id", None),
            attempt_count=1,
            final_prefix_cache=cached_tokens,
        )

    @staticmethod
    def _finish_routing_request_record(
        record,
        status_code: int,
        reason: str,
        server,
        task_status: str | None = None,
    ) -> None:
        RequestRepository.finish(
            record,
            status_code,
            reason,
            target_pod_ip=getattr(server, "base_url", None),
            model_id=getattr(server, "model_id", None),
            task_status=task_status,
            attempt_count=1,
        )

    def _choose_routing_server(self, routing_servers: list[Any], context):
        return self.workload_chooser.choose(routing_servers, context, set())

    def _routing_response_error_result(self, response) -> str:
        status_code = getattr(response, "status_code", None)
        content = proxy_response.response_content_bytes(response)
        reason = proxy_response.response_reason(response)
        message = proxy_response.extract_fail_reason(
            content,
            reason or "routing request failed",
        )
        return self._format_router_result("routing_failed", status_code, message)

    def _routing_exception_result(
        self,
        exc: Exception,
        status_code: int | None = None,
    ) -> str:
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
            f"complexity {complexity} has no matching auto-routing target model",
        )

    def _multiple_models_for_complexity_result(
        self,
        complexity: int,
        models: list[Any],
    ) -> str:
        model_names = ",".join(str(model.model_name) for model in models)
        return self._format_router_result(
            "routing_failed",
            "multiple_models_for_complexity",
            f"complexity {complexity} matched multiple auto-routing target models: {model_names}",
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
            model
            for model in models
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
    def _format_router_result(
        prefix: str,
        status_code: int | str | None,
        message: str,
    ) -> str:
        code = str(status_code) if status_code is not None else "exception"
        detail = AutoRouteAlgorithm._compact_router_message(message)
        return f"{prefix}:{code}:{detail}"[:300]

    @staticmethod
    def _compact_router_message(message: Any) -> str:
        return " ".join(str(message or "").split())

    def _ensure_system_prompt(self, model_names: list[str] | None = None) -> None:
        if self._router_system_prompt is not None:
            return

        prompt_path = APP_CONFIG.get("router", {}).get(
            "system_prompt_path",
            "router/assets/router_system_prompt.md",
        )
        try:
            with open(prompt_path, "r") as prompt_file:
                self._router_system_prompt = prompt_file.read()
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
            "response_format": self._routing_response_format(),
        }
        self.disable_thinking(payload)
        return payload

    @staticmethod
    def _routing_response_format() -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "routing_complexity",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "complexity": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                        }
                    },
                    "required": ["complexity"],
                    "additionalProperties": False,
                },
            },
        }

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

        user_contents: list[str] = []
        for message in source_messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                user_contents.append(content)
            elif isinstance(content, list):
                text_parts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                if text_parts:
                    user_contents.append(" ".join(text_parts))

        selected = user_contents[-AutoRouteAlgorithm.ROUTING_USER_PROMPT_MESSAGE_LIMIT :]
        formatted_messages: list[dict[str, Any]] = []
        ordinals = ["1st", "2nd", "3rd"]

        for index, content in enumerate(selected):
            ordinal = ordinals[index] if index < len(ordinals) else f"{index + 1}th"
            content = AutoRouteAlgorithm._truncate_routing_user_prompt(content)
            formatted_messages.append(
                {
                    "role": "user",
                    "content": f"Here is the user's {ordinal} message:\n```\n{content}\n```\n",
                }
            )
        return formatted_messages

    @classmethod
    def _truncate_routing_user_prompt(cls, content: str) -> str:
        collapse_threshold = (
            cls.ROUTING_USER_PROMPT_CHAR_LIMIT
            * cls.ROUTING_USER_PROMPT_COLLAPSE_MULTIPLIER
        )
        if len(content) <= collapse_threshold:
            return content
        limit = cls.ROUTING_USER_PROMPT_CHAR_LIMIT
        omitted = len(content) - 2 * limit
        return (
            content[:limit]
            + f" ... collapsed {omitted} chars ... "
            + content[-limit:]
        )

    def _get_default_model(self) -> Any:
        return ModelRepository.get_by_name(self.fallback_model_name())

    @staticmethod
    def fallback_model_name() -> str:
        return APP_CONFIG.get("router", {}).get("fallback_model", "DeepSeek-V4-Flash")

    @staticmethod
    def check_context_overflow(status_code: int, context_window: int | None, fail_reason: str) -> bool:
        # The upstream server reports its own configured context window in the
        # 400 error body when a request exceeds it. A NULL context window means
        # the server advertises no limit, so it never signals overflow.
        return status_code == 400 and bool(context_window) and str(context_window) in fail_reason

    @staticmethod
    def update_body_model(
        body: bytes,
        model_name: str,
        disable_thinking: bool = False,
    ) -> bytes:
        try:
            body_data = json.loads(body.decode("utf-8"))
            body_data["model"] = model_name
            if disable_thinking:
                AutoRouteAlgorithm.disable_thinking(body_data)
            return json.dumps(
                body_data,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except Exception:
            return body

    @staticmethod
    def disable_thinking(body_data: dict[str, Any]) -> None:
        chat_template_kwargs = body_data.get("chat_template_kwargs")
        if not isinstance(chat_template_kwargs, dict):
            chat_template_kwargs = {}
        chat_template_kwargs["enable_thinking"] = False
        body_data["chat_template_kwargs"] = chat_template_kwargs

    @staticmethod
    def _build_url(base_url: str, path: str, query_string: str) -> str:
        url = base_url.rstrip("/") + "/" + path
        if query_string:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query_string}"
        return url
