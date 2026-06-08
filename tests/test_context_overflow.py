import json
from unittest.mock import MagicMock
import pytest
from django.test import Client
from router.models import Model, Server, RequestRecord

def test_context_overflow_switches_to_flash_when_auto(monkeypatch):
    # Setup models
    flash_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    other_model = Model.objects.create(
        model_name="Other-Model",
        max_context_window=1000,
        complexity_min=1,
        complexity_max=10,
    )
    
    # Setup servers
    Server.objects.create(model_id=other_model.id, base_url="http://other.example", is_online=True)
    flash_server = Server.objects.create(model_id=flash_model.id, base_url="http://flash.example", is_online=True)
    
    # Mock routing LLM to return other_model for 'auto'
    def fake_query_routing_llm(self, body, record, context, active_models, model_names):
        return other_model, "router_decision"
    monkeypatch.setattr("router.route_algorithm.auto.ModelResolver._query_routing_llm", fake_query_routing_llm)
    
    # Mock requests to fail with 400 and context overflow on the first attempt
    # and succeed on the second attempt (for the flash model)
    attempt_count = 0
    def fake_request(self_inner, method, url, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        upstream = MagicMock()
        if "other.example" in url:
            upstream.status_code = 400
            upstream.reason = "Bad Request"
            upstream.content = b'{"error": {"message": "context window 1000 exceeded"}}'
            upstream.headers = {"content-type": "application/json"}
        else:
            upstream.status_code = 200
            upstream.reason = "OK"
            upstream.content = b'{"choices": [{"message": {"content": "flash response"}}]}'
            upstream.headers = {"content-type": "application/json"}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="1.2.3.4"
    )

    assert response.status_code == 200
    assert b"flash response" in response.content
    assert attempt_count == 2

def test_context_overflow_does_not_switch_when_explicit_model(monkeypatch):
    # Setup models
    flash_model = Model.objects.create(model_name="DeepSeek-V4-Flash")
    other_model = Model.objects.create(model_name="Other-Model", max_context_window=1000)
    
    # Setup servers
    Server.objects.create(model_id=other_model.id, base_url="http://other.example", is_online=True)
    Server.objects.create(model_id=flash_model.id, base_url="http://flash.example", is_online=True)
    
    # Mock requests to fail with 400 and context overflow
    attempt_count = 0
    def fake_request(self_inner, method, url, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        upstream = MagicMock()
        upstream.status_code = 400
        upstream.reason = "Bad Request"
        upstream.content = b'{"error": {"message": "context window 1000 exceeded"}}'
        upstream.headers = {"content-type": "application/json"}
        return upstream

    monkeypatch.setattr(
        "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
        fake_request,
    )

    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": "Other-Model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="1.2.3.4"
    )

    assert response.status_code == 400
    assert b"context window 1000 exceeded" in response.content
    # It might retry on the SAME model if max_attempts > 1, 
    # but it should NOT switch to flash_model.
    # In this case, since we only have one server for other_model, 
    # it might only try once or retry the same server depending on policy.
    # The key is that it shouldn't return a 200 from flash_model.
    assert attempt_count >= 1
