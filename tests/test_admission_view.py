import json
import pytest
from django.test import Client
from router.models import Model, Server
from router.services.admission import AdmissionService

@pytest.mark.django_db
def test_deprecated_model_returns_400():
    Model.objects.create(
        model_name="deprecated-model",
        deprecation="This model is deprecated. Please use model-v2."
    )
    
    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": "deprecated-model"}),
        content_type="application/json"
    )
    
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["message"] == "This model is deprecated. Please use model-v2."
    assert data["error"]["type"] == "invalid_request_error"
    
    # Verify fail_reason in DB matches
    from router.models import RequestRecord
    record = RequestRecord.objects.last()
    assert record.fail_reason == data["error"]["message"]

@pytest.mark.django_db
def test_max_tokens_fail_reason_matches():
    Model.objects.create(model_name="expensive-model", max_tokens=10)
    
    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": "expensive-model", "max_tokens": 100}),
        content_type="application/json"
    )
    
    assert response.status_code == 400
    data = response.json()
    from router.models import RequestRecord
    record = RequestRecord.objects.last()
    assert record.fail_reason == data["error"]["message"]
    assert "too many tokens" in record.fail_reason


def test_auto_max_tokens_uses_global_limit():
    admission = AdmissionService()
    assert admission.check_max_tokens(40000, None, is_auto=True).allowed is True
    result = admission.check_max_tokens(40001, None, is_auto=True)
    assert result.allowed is False
    assert "Max allowed is 40000" in result.message


@pytest.mark.django_db
def test_auto_request_over_global_limit_returns_400():
    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "auto", "max_tokens": 50000}),
        content_type="application/json",
    )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["type"] == "invalid_request_error"
    assert "Max allowed is 40000" in data["error"]["message"]

    from router.models import RequestRecord
    record = RequestRecord.objects.last()
    assert record.fail_reason == data["error"]["message"]


@pytest.mark.django_db
def test_auto_request_within_global_limit_passes_max_tokens_check():
    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "auto", "max_tokens": 30000}),
        content_type="application/json",
    )

    # Max-token admission must pass; downstream auto routing may still fail.
    if response.status_code == 400:
        assert "too many tokens" not in response.json()["error"]["message"]


@pytest.mark.django_db
def test_auto_flagged_model_uses_global_limit_not_entrance_max_tokens():
    # The entrance model's own max_tokens must not cap auto-routed requests.
    Model.objects.create(model_name="source-model", auto=True, max_tokens=10)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "source-model", "max_tokens": 20000}),
        content_type="application/json",
    )

    # Max-token admission must pass; any later failure is unrelated to limits.
    if response.status_code == 400:
        assert "too many tokens" not in response.json()["error"]["message"]


@pytest.mark.django_db
def test_auto_flagged_model_over_global_limit_returns_400():
    Model.objects.create(model_name="source-model", auto=True, max_tokens=100000)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "source-model", "max_tokens": 50000}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "Max allowed is 40000" in response.json()["error"]["message"]


@pytest.mark.django_db
def test_auto_request_over_global_limit_via_max_completion_tokens_returns_400():
    # vLLM gives max_completion_tokens precedence, so it must trigger the
    # same admission rejection as an oversized max_tokens.
    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "auto", "max_completion_tokens": 100000}),
        content_type="application/json",
    )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["type"] == "invalid_request_error"
    assert "Max allowed is 40000" in data["error"]["message"]

    from router.models import RequestRecord
    record = RequestRecord.objects.last()
    assert record.fail_reason == data["error"]["message"]


@pytest.mark.django_db
def test_model_request_over_model_limit_via_max_completion_tokens_returns_400():
    Model.objects.create(model_name="capped-model", max_tokens=65536)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "capped-model", "max_completion_tokens": 100000}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "Max allowed is 65536" in response.json()["error"]["message"]


@pytest.mark.django_db
def test_unknown_model_returns_400():
    input_model_name = "user-requested-unknown-model"
    client = Client()
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": input_model_name}),
        content_type="application/json"
    )

    assert response.status_code == 400
    data = response.json()
    assert data["error"]["message"] == f"Model {input_model_name} is not supported."
    assert data["error"]["type"] == "invalid_request_error"

    from router.models import RequestRecord
    record = RequestRecord.objects.last()
    assert record.status == "400 Bad Request"
    assert record.fail_reason == data["error"]["message"]


@pytest.mark.django_db
def test_small_unknown_model_returns_400_even_with_routing_server():
    routing_model = Model.objects.create(model_name="router-model", is_routing_model=True)
    Server.objects.create(model_id=routing_model.id, base_url="http://router.example", is_online=True)

    response = Client().post(
        "/v1/chat/completions",
        data=json.dumps({"model": "unknown-model", "messages": [{"role": "user", "content": "hello"}]}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Model unknown-model is not supported."


@pytest.mark.django_db
def test_normal_model_not_blocked_by_deprecation():
    Model.objects.create(
        model_name="normal-model",
        deprecation=None
    )
    
    client = Client()
    # Mocking parser to return normal-model
    response = client.post(
        "/v1/chat/completions",
        data=json.dumps({"model": "normal-model"}),
        content_type="application/json"
    )
    
    # It should pass deprecation check. 
    # It might fail later due to missing servers or other things, but not with the deprecation message.
    if response.status_code == 400:
        assert response.json()["error"]["message"] != "This model is deprecated. Please use model-v2."
