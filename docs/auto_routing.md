# Auto Routing

`auto` routing lets the router choose a concrete target model at request time. It is implemented in `router/route_algorithm/auto.py` and runs before normal server selection.

There are two related behaviors:

- True auto model selection: requests whose input model is `auto`, or normal-port requests for a concrete model whose `models.auto = TRUE`, can be rewritten to another model.
- Small-request routing: normal-port requests with an estimated full body size below `3000` tokens can be sent to a configured routing model before true auto selection runs.

## Database Setup

Configure model rows with these fields:

| Field | Meaning |
|-------|---------|
| `is_routing_model` | Marks a model whose servers can classify auto requests and receive small requests. |
| `auto` | Marks a concrete model name as an auto-routing entry point on the normal port. It does not control target eligibility. |
| `complexity_min`, `complexity_max` | Inclusive 1-10 target range for text auto routing. Both must be set for the model to be a text target. |
| `multimodal` | Marks the model as eligible for image-containing auto requests. |

Configure server rows with:

| Field | Meaning |
|-------|---------|
| `model_id` | The model served by this upstream. |
| `context_window` | Optional per-server context-window ceiling. Not used to pre-filter servers. When an upstream rejects a request with an overflow error that contains this value, the router retries on a larger-window server of the same model (or the fallback model). `NULL` means unlimited. |
| `vip` | Routing-classifier calls and small-request routing use non-VIP servers. VIP-channel concrete model requests do not enter auto selection. |

## Configuration

Optional `router` configuration keys:

```yaml
router:
  fallback_model: DeepSeek-V4-Flash
  system_prompt_path: router/assets/router_system_prompt.md
  auto_concurrent_limit: 6
```

- `fallback_model` is used when the routing LLM cannot produce a unique complexity target.
- `system_prompt_path` points to the classifier prompt. If it cannot be read, a built-in compact JSON classifier prompt is used.
- `auto_concurrent_limit` is the admission-control limit base for requests whose input model is exactly `auto`.

## Entry Conditions

The router parses the request body first. `model: "auto"` is case-insensitive.

Auto selection starts when either condition is true:

- The parsed model name is `auto`.
- The request uses the normal port and the concrete requested model has `models.auto = TRUE`.

Auto selection does not start for concrete model requests on the VIP port. Unknown concrete model names are rejected before auto routing, even if a routing model exists.

## Selection Sequence

For each accepted proxy request, the router creates a `requests` row in `processing` state, then starts model-choice timing when the request is eligible for true auto selection or small-request routing.

1. Record the original model name.

   This value prefixes the final `requests.router_result`, for example `auto:complexity:5` or `source-model:small_request_routing`.

2. Try small-request routing.

   This step runs first for normal-port requests whose estimated full body token count is below `3000`.

   The router scans `models.is_routing_model = TRUE` rows by ascending `id` and asks for non-VIP servers for each routing model. Candidate servers must be online, routable by circuit-breaker state, not soft-deleted, and have `context_window IS NULL OR context_window >= estimate_tokens`.

   If a routing-model server exists, the request is rewritten to that routing model, `chat_template_kwargs.enable_thinking` is set to `false`, the processing row's `model_id` is updated, and `router_result` becomes `small_request_routing`. True auto selection is skipped.

   If no routing-model server exists, the router continues. A non-auto request keeps its requested model; an auto request proceeds to true auto selection.

3. Detect multimodal auto requests.

   For true auto requests, the router checks the JSON `messages` list. If any message content is a list containing a part with `type: "image_url"` and a truthy `image_url`, the router selects the first active model with `multimodal = TRUE`, ordered by `id`.

   This bypasses the text complexity classifier. If no active multimodal model exists, the request continues through text auto selection.

4. Build the text target set.

   Text targets are models with:

   - `complexity_min IS NOT NULL`
   - `complexity_max IS NOT NULL`
   - `complexity_min >= 1`
   - `complexity_max <= 10`
   - `complexity_min <= complexity_max`

   `models.deprecation` is intentionally ignored here. A model with complexity bounds is an auto-routing target even when deprecated by name, so a deprecated model can still serve `auto` requests.

   `models.auto` is not required for target eligibility. A model can be an auto target with `auto = FALSE`.

   If there are no text targets, `router_result` records `routing_failed:missing_target_model:no auto-routing target model for auto request`. No fallback model is applied at this step.

5. Check prefix-cache model hits.

   When the active chooser supports `get_all_model_prefix_ratios`, the router checks Redis prefix-cache ratios for every text target model. This pre-check is skipped for requests with exactly one user message.

   A model is selected immediately only when exactly one text target has a prefix ratio greater than `0.7`. The result is `cache_hit`.

   Zero hits or multiple hits fall through to the routing LLM.

6. Query the routing LLM.

   Routing servers are all non-VIP servers for models with `is_routing_model = TRUE`, filtered by online state, circuit-breaker state, and soft delete. The original request's context-window estimate is not applied to this classifier-server lookup because the classifier payload is bounded to a small prompt. Among those servers, the router chooses by `servers.workload`, with random tie breaking.

   The routing request is a non-streaming `/chat/completions` call with:

   - `model` set to the selected routing model name
   - the configured system prompt
   - only user-role messages from the original request
   - at most the last 20 user messages
   - each forwarded user message truncated to at most `1500` characters (3× the 500-char base). Messages up to `1500` characters are forwarded in full; longer messages keep the first `500` and last `500` characters with a `... collapsed <n> chars ...` marker between them.
   - `response_format` requiring JSON schema `{"complexity": <integer 1-10>}`
   - `chat_template_kwargs.enable_thinking = false`

   The routing call is logged as its own `requests` row with `ip_id = 0`, `user_agent = "llm-choosing"`, `is_stream = FALSE`, `attempt_count = 1`, and the routing server in `target_pod_ip`.

7. Parse complexity.

   The router accepts compact JSON, fenced JSON, a bare integer, or the first standalone integer from `1` to `10`. Invalid, missing, out-of-range, or boolean values are treated as routing failures.

8. Match complexity to a target model.

   The selected complexity must match exactly one text target range.

   - One match: select that model and record `complexity:<score>`.
   - No matches: use `router.fallback_model` and record `routing_failed:no_model_for_complexity:...`.
   - Multiple matches: use `router.fallback_model` and record `routing_failed:multiple_models_for_complexity:...`.
   - Routing LLM unavailable, non-200, exception, or invalid output: use `router.fallback_model` and record a `routing_failed` or `routing_error` result.

9. Rewrite the original request.

   When a concrete model is selected, the router updates:

   - the processing row's `model_id`
   - `parsed.model_name`
   - the request JSON `model`
   - `ServerSelectionContext.model_id`, `model_name`, and `body`

10. Choose an upstream server for the selected model.

    Normal server selection then runs with the configured chooser. The default `PrefixCachePrebleServerChooser` chooses among candidate servers for the selected model, records `prefix_cache` and `last_match`, and caches successful responses.

## Context-Overflow Handling

The router does not pre-filter servers by an estimated request size. A request is sent to the selected server and, if that server rejects it for exceeding its context window (HTTP 400 whose error body contains the server's own `servers.context_window` value), the router retries on a server of the **same model** whose `servers.context_window` is strictly larger (a `NULL` context window is treated as unlimited). The chooser skips servers already attempted for this request.

The router never switches to a different model on a context overflow (issue #224). If no larger-window same-model server exists, the original upstream overflow error is returned to the client.

## Request Records

The original client request row records:

- `model_id`: updated to the selected concrete model when one is chosen.
- `router_result`: original model prefix plus the route decision, capped at 300 characters. This is persisted during processing (together with `model_id`) as soon as a model is resolved, not only at request finish. `AdmissionService.check_concurrency` reads the prefix (everything before the first `:`) to bucket in-flight requests by their entrance model, so the origin prefix must not be removed or reordered.
- `estimate_tokens`: fast estimate from the original body.
- `model_choosing_latency`: elapsed milliseconds for small-request routing or true auto selection.
- `prefix_cache` and `last_match`: server-selection prefix-cache data for the final upstream attempt.

Routing LLM calls are separate internal request rows. Statistics APIs exclude rows with `ip_id = 0`.

## Concurrency Accounting

`AdmissionService.check_concurrency` limits in-flight requests per IP, bucketed by **entrance model** rather than by the model that ultimately serves the request:

- A literal `auto` request counts under the `auto` bucket (limit `router.auto_concurrent_limit`), both while unresolved (`model_id = 0`) and after resolution (prefix `auto`).
- A request for a concrete model by name counts under that model's bucket (limit `models.concurrent_limit`), whether or not the model is also `auto = TRUE`. While unresolved the record has a `NULL` `router_result` and its own `model_id`; after resolution it carries a `<name>:` prefix.
- Because the bucket follows the entrance, an `auto -> B` request never counts against `B`'s concurrency, and a `source-model -> B` request never counts against the resolved target. Each entrance model enforces its own limit independently.

VIP-channel requests are excluded from these counts (their records carry `vip = TRUE` and are filtered out), since VIP capacity is managed by VIP scaling. Occasional over-limit under concurrent arrivals is tolerated; the check intentionally uses no locking.

## Example

```sql
INSERT INTO models (model_name, is_routing_model)
VALUES ('router-model', true);

INSERT INTO models (model_name, complexity_min, complexity_max)
VALUES
  ('fast-model', 1, 3),
  ('balanced-model', 4, 7),
  ('reasoning-model', 8, 10);

INSERT INTO models (model_name, multimodal)
VALUES ('vision-model', true);
```

```bash
curl -i http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Plan a safe rollout for this migration"}]}'
```

The router classifies the user request, rewrites the body to the selected model, then forwards it to an online non-VIP server for that model.
