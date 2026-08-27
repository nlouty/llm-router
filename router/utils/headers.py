from __future__ import annotations

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

REQUEST_STRIP_HEADERS = HOP_BY_HOP_HEADERS | {"content-length", "host", "content-encoding"}
BODYLESS_METHODS = {"GET", "HEAD", "OPTIONS", "DELETE"}

# Headers carrying client credentials; replaced by the server's api_key when set.
AUTH_HEADERS = ("authorization", "x-api-key", "api-key")


def build_upstream_headers(headers: dict[str, str], server) -> dict[str, str]:
    """Copy filtered client headers, then apply per-server credentials.

    Injects ``csb-token`` when the server has one, and when the server has an
    ``api_key`` (for gateways that only accept a specific key) strips any
    client auth headers and sends ``Authorization: Bearer <api_key>`` instead.
    """
    req_headers = {**headers}
    csb_token = getattr(server, "csb_token", None)
    if csb_token:
        req_headers["csb-token"] = csb_token
    api_key = getattr(server, "api_key", None)
    if api_key:
        for key in [k for k in req_headers if k.lower() in AUTH_HEADERS]:
            req_headers.pop(key)
        req_headers["Authorization"] = f"Bearer {api_key}"
    return req_headers


def filter_request_headers(headers: dict[str, str], method: str) -> dict[str, str]:
    strip = set(REQUEST_STRIP_HEADERS)
    if method.upper() in BODYLESS_METHODS:
        strip.add("content-type")
    return {key: value for key, value in headers.items() if key.lower() not in strip}


def filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    strip = HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}
    return {key: value for key, value in headers.items() if key.lower() not in strip}
