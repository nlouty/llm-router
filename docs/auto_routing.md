# Auto Routing Algorithm

The `AutoRouteAlgorithm` is a core feature of the LLM Router designed to intelligently dispatch requests to the most appropriate upstream model when the client requests the `auto` model or a model flagged with `models.auto = TRUE`. The algorithm employs a multi-step sequence to balance performance, cost, and request complexity.

## LLM Choosing Sequence

When a request is eligible for auto-routing, the router performs the following sequence to determine the target model:

1.  **Small Request Routing (Fast Path for Text)**
    *   **Threshold Check:** The router estimates the total tokens (input + output). If the estimate is strictly less than `SMALL_REQUEST_ROUTING_TOKEN_LIMIT` (default 3000) and the request is not on the VIP channel, it qualifies as a small request.
    *   **Model Selection:** It scans the active models flagged as routing models (`models.is_routing_model = TRUE`). If any routing model has online servers with available capacity, it selects the first available routing model as the target.
    *   **Modifications:** The request is modified to disable thinking (`enable_thinking=False` in `chat_template_kwargs`) to ensure fast response times for simple queries.

2.  **Multimodal Bypass (Fast Path for Images)**
    *   **Image Detection:** If the request body contains a `messages` array where any user message has a part with `type: "image_url"`, the request is classified as multimodal.
    *   **Model Selection:** The router immediately dispatches the request to the designated multimodal model (`models.multimodal = TRUE`).

3.  **Prefix Cache Hit (Cost Optimization)**
    *   **Condition:** This step is only evaluated if the request contains exactly one `user` prompt message.
    *   **Cache Query:** The router queries the Redis-backed prefix cache (`ServerChooser`) to find the cache match ratio for all active auto-selectable models (`models.auto_selectable = TRUE`).
    *   **Hit Threshold:** If exactly one active model has a prefix match ratio exceeding `PREFIX_CACHE_AUTO_HIT_THRESHOLD` (default 0.7), that model is selected to capitalize on cache locality.

4.  **LLM-based Complexity Classification (Intelligent Routing)**
    *   **Querying the Routing LLM:** If no fast path or cache hit applies, the router queries an online server of one of the configured routing models (`is_routing_model = TRUE`).
    *   **Payload Construction:** The router builds a lightweight classification prompt:
        *   It uses a system prompt defined by `router.system_prompt_path` (defaulting to `router/assets/router_system_prompt.md`).
        *   It extracts up to the last three `user` messages from the request, truncating each to `ROUTING_USER_PROMPT_CHAR_LIMIT` (default 500) characters.
        *   It forces the routing LLM to return structured JSON (`response_format: { "type": "json_schema" ... }`) containing a single integer `complexity` score between 1 and 10.
        *   Thinking is explicitly disabled to minimize latency.
    *   **Workload Management:** The router increments the routing server's workload during the classification request and logs the sub-request lifecycle to the database (`requests` table) with the `model_id` of the routing model.
    *   **Target Matching:** The resulting `complexity` score (1-10) is compared against the `complexity_min` and `complexity_max` bounds of all active auto-selectable models. If exactly one model's bounds encompass the score, it is selected.

5.  **Fallback Mechanism**
    *   **Conditions for Fallback:** The fallback model (defined by `router.fallback_model`, default `DeepSeek-V4-Flash`) is selected if:
        *   The routing LLM request fails or returns an invalid complexity.
        *   The complexity score matches zero or multiple auto-selectable models.
    *   **Context Overflow Switch:** During request execution, if the target model returns an HTTP 400 error indicating a context window overflow (e.g., the failure reason contains the model's `max_context_window`), the router automatically modifies the request to target the fallback model and retries.

## Request Lifecycle Modifications

Throughout the sequence, the router actively modifies the request body to match the selected model's requirements:
*   The `model` field in the JSON body is overwritten with the chosen `model_name`.
*   The `RequestRepository` record is updated with the chosen `model_id` and the routing logic outcome (e.g., `small_request_routing`, `cache_hit`, `complexity:X`).
*   The routing sub-request is recorded as an independent `Request` row, linking the user's IP to the routing model's usage for accurate workload accounting.