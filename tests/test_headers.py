from types import SimpleNamespace

from router.utils.headers import build_upstream_headers, filter_request_headers


def test_filter_request_headers_removes_hop_by_hop_and_proxy_invalid_headers():
    headers = {
        "Host": "example.com",
        "Connection": "keep-alive",
        "Authorization": "Bearer token",
        "Content-Length": "10",
        "Content-Encoding": "gzip",
        "Content-Type": "application/json",
    }
    filtered = filter_request_headers(headers, "POST")
    assert filtered == {"Authorization": "Bearer token", "Content-Type": "application/json"}


def test_filter_request_headers_removes_content_type_for_get():
    filtered = filter_request_headers({"Content-Type": "application/json", "Accept": "application/json"}, "GET")
    assert filtered == {"Accept": "application/json"}


def test_build_upstream_headers_replaces_client_auth_with_server_api_key():
    server = SimpleNamespace(csb_token=None, api_key="sk-server-secret")
    headers = {
        "Authorization": "Bearer sk-user-key",
        "x-api-key": "sk-user-key",
        "API-KEY": "sk-user-key",
        "Content-Type": "application/json",
    }
    built = build_upstream_headers(headers, server)
    assert built["Authorization"] == "Bearer sk-server-secret"
    assert "sk-user-key" not in str(built.values())
    assert not any(k.lower() in ("x-api-key", "api-key") for k in built)
    assert built["Content-Type"] == "application/json"


def test_build_upstream_headers_without_api_key_forwards_client_auth():
    server = SimpleNamespace(csb_token=None, api_key=None)
    headers = {"Authorization": "Bearer sk-user-key", "Content-Type": "application/json"}
    assert build_upstream_headers(headers, server) == headers


def test_build_upstream_headers_injects_csb_token_alongside_api_key():
    server = SimpleNamespace(csb_token="csb-tok", api_key="sk-server-secret")
    built = build_upstream_headers({"Authorization": "Bearer sk-user-key"}, server)
    assert built == {"Authorization": "Bearer sk-server-secret", "csb-token": "csb-tok"}


def test_build_upstream_headers_injects_csb_token_only():
    server = SimpleNamespace(csb_token="csb-tok", api_key=None)
    built = build_upstream_headers({"Authorization": "Bearer sk-user-key"}, server)
    assert built == {"Authorization": "Bearer sk-user-key", "csb-token": "csb-tok"}


def test_build_upstream_headers_tolerates_server_without_credential_attributes():
    built = build_upstream_headers({"Authorization": "Bearer sk-user-key"}, SimpleNamespace())
    assert built == {"Authorization": "Bearer sk-user-key"}
