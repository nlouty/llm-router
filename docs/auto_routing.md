# Auto Routing (LLM Choosing Algorithm)

Auto routing lets a client send `model: "auto"` (or any model flagged `auto = TRUE`) and have the router pick a concrete target model per request. The decision is made by `AutoRouteAlgorithm` (`router/route_algorithm/auto.py`), driven by request size, prefix-cache reuse, multimodal detection, and a complexity classification call to a dedicated **routing model**.

This document describes the exact sequence the algorithm follows on every routed request.

## Entry Conditions

`AutoRouteAlgorithm.should_auto_select` decides whether a request enters auto routing:

- `parsed.model_name` equals `auto` (case-insensitive) → always routed, on any port.
- VIP channel → never auto-routed by model flag (a concrete model on the VIP port is served as requested).
- Normal port → routed when the requested model row has `models.auto = TRUE`.

The chosen concrete target is applied to the request body, the `requests` row, and the selection context before backend server selection runs.

## The `resolve` Sequence

`AutoRouteAlgorithm.resolve` runs two stages in order. The first stage that produces a decision wins.

```
resolve()
  ├── _resolve_small_request_routing_model()   (stage 1: size shortcut)
  └── _resolve_auto_model()                    (stage 2: full choosing)
```

The final `router_result` is prefixed with the originally requested model name, e.g. `auto:complexity:7` or `glm-5:routing_failed:no_model_for_complexity:...`, and truncated to 300 characters.

### Stage 1 — Small Request Routing

Applies only on the normal port. If the estimated request size is below `SMALL_REQUEST_ROUTING_TOKEN_LIMIT` (`3000` estimated tokens), the router short-circuits and sends the request directly to a **routing model** as the actual inference backend — no complexity classification.

1. `should_route_small_request` — true when `parsed.estimated_full_body_tokens < 3000`.
2. `_get_small_request_routing_model` — iterates `ModelRepository.get_routing_models()` (models with `is_routing_model = TRUE`) in id order and returns the first that has at least one non-VIP online server (`ServerRepository.list_by_model_id(..., vip=False)`).
3. `_apply_resolved_model` rewrites the body to the routing model name and **disables thinking** (`chat_template_kwargs.enable_thinking = False`), since the routing model is acting as a fast responder.
4. Result string: `small_request_routing`.

If no routing model has an available server, stage 1 yields no decision and stage 2 runs.

### Stage 2 — Auto Model Selection (`_get_auto_route_model`)

Runs only when `auto_model_selection` is true. This stage chooses among **auto-selectable target models** (not routing models) and produces a `router_result`.

The branches, in order:

#### 2a. Multimodal Bypass

`_is_multimodal` scans `messages[].content` for any `image_url` part. If found, the router skips text complexity classification and selects the active model with `multimodal = TRUE` (`ModelRepository.get_multimodal_model`). Result string: `multimodal_bypass`.

#### 2b. Build the Candidate Pool

`ModelRepository.list_auto_selectable_models` returns active (non-deprecated) models whose `complexity_min` and `complexity_max` are both set, in range 1–10, and where `complexity_min <= complexity_max`. If this list is empty, routing fails with `routing_failed:missing_target_model:...`.

#### 2c. Prefix-Cache Hit

`_check_cache_hit` reuses a previous decision for the same conversation, so multi-turn chats do not re-classify on every turn.

- Skipped when there is exactly one user message (single-shot prompt — nothing to cache against).
- Asks the prefix-cache chooser for per-model match ratios (`get_all_model_prefix_ratios`).
- A model qualifies when its ratio exceeds `PREFIX_CACHE_AUTO_HIT_THRESHOLD` (`0.7`).
- Only an **unambiguous single hit** is trusted. Zero or multiple hits fall through to the LLM call.
- Result string: `cache_hit`.

#### 2d. Query the Routing LLM

This is the core classification step. `_query_routing_complexity` calls a routing model to score the request, then `_query_routing_llm` maps the score to a target model.

**Selecting the routing server**

1. `ModelRepository.get_routing_models()` — all models with `is_routing_model = TRUE`. If none, result `routing_failed:missing_routing_model:...`.
2. Collect online non-VIP servers across all routing models. If none, result `routing_failed:missing_routing_server:...` (the default failure code).
3. `_choose_routing_server` picks one via `LeastConnectionServerChooser.for_server_workload()` — the routing server with the lowest `servers.workload` counter, ties broken at random.

**Building the payload** (`_build_routing_payload`)

- `model`: the chosen routing model's name.
- `messages`: the router system prompt (see below), followed by the user's **last 3** user-role messages extracted from the original body. Each message is truncated to `ROUTING_USER_PROMPT_CHAR_LIMIT` (`500` characters) and wrapped as `Here is the user's Nth message:` with a fenced block. Multimodal/text parts are flattened to text.
- `stream`: false.
- `response_format`: a strict `json_schema` requiring `{"complexity": <integer 1..10>}`. This is the structured-output contract the routing model must obey.
- `chat_template_kwargs.enable_thinking = False` (disabled thinking).
- If the payload has no user messages, result `no_user_query`.

The system prompt is loaded once (lazily) from `router.system_prompt_path` (default `router/assets/router_system_prompt.md`); a compact fallback prompt is used if the file cannot be read.

**Executing the call**

A `requests` row is created with `RequestRepository.create_llm_choosing` (`ip_id = 0`, `user_agent = "llm-choosing"`, `attempt_count = 1`) so the classification call is independently tracked. `servers.workload` is incremented before the call and decremented in a `finally` block. The HTTP POST has a **10-second timeout**.

| Outcome | Handling | Result string |
|---------|----------|---------------|
| Connection exception | finish choosing record as 502, log to request log | `routing_error:exception:<message>` |
| Non-200 response | finish choosing record with the response | `routing_failed:<status>:<message>` |
| 200 but unparseable body | finish choosing record as 200 | `routing_failed:invalid_routing_result:<detail>` |
| Valid 200 | finish choosing record, parse complexity | `complexity:<n>` |

On a successful response the complexity is parsed from the JSON content. Because `response_format` enforces the schema, the value normally comes from `choices[0].message.content` parsed as `{"complexity": N}`. A regex fallback (`_extract_complexity_number`) rescues free-text outputs by extracting the first standalone integer 1–10. Values outside 1–10 are rejected as invalid.

**Mapping complexity to a target model** (`_query_routing_llm`)

`_models_for_complexity` selects candidates whose inclusive `[complexity_min, complexity_max]` range contains the score:

- Exactly one match → that model is selected. The request body and record are rewritten to it.
- More than one match (overlapping ranges) → fall back to the default model, result `routing_failed:multiple_models_for_complexity:<names>`.
- No match → fall back to the default model, result `routing_failed:no_model_for_complexity:...`.
- No complexity obtained → fall back to the default model, carrying whatever result string the failure produced.

The default (fallback) model is `ModelRepository.get_by_name(router.fallback_model)`, default name `DeepSeek-V4-Flash`.

## Context-Overflow Fallback

After a target is chosen and the request is sent upstream, a 400 whose reason mentions the model's `max_context_window` triggers `context_overflow_switch`:

- Applies only to auto-routed requests (`auto_model_selection`).
- Skipped if the current model is already the fallback, or no fallback model row exists.
- Rewrites the body to the fallback model, updates the context, re-selects candidates, and retries.
- Logged via `log_context_overflow_switch`.

This keeps a too-large auto-routed request from failing outright when a larger-context fallback is configured.

## Recording and Observability

- **Model-choosing latency**: when `should_record_model_choice` is true (auto routing, or small-request routing on the normal port), `resolve` is timed and stored in `requests.model_choosing_latency` (ms).
- **Router result**: the `router_result` string is persisted on the original request row via `RequestRepository.finish`.
- **Choosing records**: each routing-LLM call gets its own `requests` row (`ip_id = 0`, `user_agent = "llm-choosing"`) with its own latency, token counts, and status, separate from the client request.

## Configuration

Auto routing reads two optional config keys (deep-merged onto `config.yaml`); neither appears in the shipped defaults:

```yaml
router:
  fallback_model: DeepSeek-V4-Flash          # used on ambiguous/failed classification and context overflow
  system_prompt_path: router/assets/router_system_prompt.md
```

Fixed algorithm constants in `AutoRouteAlgorithm`:

| Constant | Value | Meaning |
|----------|-------|---------|
| `SMALL_REQUEST_ROUTING_TOKEN_LIMIT` | 3000 | Estimated tokens below which stage 1 routes to a routing model directly |
| `PREFIX_CACHE_AUTO_HIT_THRESHOLD` | 0.7 | Min per-model prefix ratio to count as a cache hit |
| `ROUTING_USER_PROMPT_CHAR_LIMIT` | 500 | Per-message truncation length sent to the routing model |

## Related Schema

See [Database Schema](database_schema.md) for the supporting columns:

- `models.auto` — entry flag for auto routing (normal port, concrete models).
- `models.is_routing_model` — marks models usable as the classification backend and as a small-request target.
- `models.complexity_min` / `models.complexity_max` — target eligibility and complexity matching.
- `models.multimodal` — image-request target eligibility.
- `models.max_context_window` — drives the context-overflow fallback.
- `requests.router_result`, `requests.model_choosing_latency`, `requests.estimate_tokens` — decision trail.
