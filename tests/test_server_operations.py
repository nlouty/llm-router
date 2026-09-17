import json
from unittest.mock import patch, MagicMock
from django.test import Client
from router.models import Server, ServerOperation, Model

def test_add_server_single_success():
    client = Client()
    payload = {
        "base_url": "http://test-server/v1",
        "model_name": "gpt-3.5-turbo"
    }
    
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [{"id": "gpt-3.5-turbo"}]
        }
        mock_get.return_value = mock_resp
        
        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")
        
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert data["data"]["base_url"] == "http://test-server/v1"
    
    # Check Server record
    assert Server.objects.filter(base_url="http://test-server/v1").exists()
    
    # Check ServerOperation record
    op = ServerOperation.objects.get(operation_type="add_server")
    assert op.status == "success"
    assert op.server_id is not None
    assert op.request_data == payload

def test_add_server_multiple_success():
    client = Client()
    payload = [
        {"base_url": "http://s1/v1", "model_name": "m1"},
        {"base_url": "http://s2/v1", "model_name": "m1"}
    ]
    
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [{"id": "m1"}]
        }
        mock_get.return_value = mock_resp
        
        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")
        
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 2
    assert data[0]["base_url"] == "http://s1/v1"
    assert data[1]["base_url"] == "http://s2/v1"
    
    assert ServerOperation.objects.filter(status="success").count() == 2

def test_add_server_duplicate_in_request():
    client = Client()
    payload = [
        {"base_url": "http://s1/v1", "model_name": "m1"},
        {"base_url": "http://s1/v1", "model_name": "m1"}
    ]

    response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert response.json()["error"] == "duplicate (model_name, base_url, api_key) in request"
    assert ServerOperation.objects.count() == 0


def test_add_server_batch_same_base_url_different_api_keys():
    client = Client()
    payload = [
        {"base_url": "http://a/v1", "model_name": "m1", "api_key": "sk-1"},
        {"base_url": "http://a/v1", "model_name": "m1", "api_key": "sk-2"},
    ]

    captured_headers = []

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "m1"}]}

        def capture(url, **kwargs):
            captured_headers.append(kwargs.get("headers"))
            return mock_resp

        mock_get.side_effect = capture

        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 2
    assert Server.objects.filter(base_url="http://a/v1").count() == 2
    assert set(Server.objects.filter(base_url="http://a/v1").values_list("api_key", flat=True)) == {"sk-1", "sk-2"}

    # Each row is verified with its own credential.
    assert captured_headers == [
        {"Authorization": "Bearer sk-1"},
        {"Authorization": "Bearer sk-2"},
    ]


def test_add_server_batch_same_base_url_same_key_rejected():
    client = Client()
    payload = [
        {"base_url": "http://a/v1", "model_name": "m1", "api_key": "sk-1"},
        {"base_url": "http://a/v1", "model_name": "m1", "api_key": "sk-1"},
    ]

    response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "duplicate" in response.json()["error"]
    assert ServerOperation.objects.count() == 0


def test_add_server_rejects_existing_tuple():
    model = Model.objects.create(model_name="m1")
    Server.objects.create(model_id=model.id, base_url="http://a/v1", api_key="sk-1")
    client = Client()
    payload = {"base_url": "http://a/v1", "model_name": "m1", "api_key": "sk-1"}

    response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert response.json()["error"] == "server with this model/base_url/api_key already exists"
    assert Server.objects.filter(base_url="http://a/v1").count() == 1


def test_add_server_rejects_keyless_duplicate_of_keyless_row():
    model = Model.objects.create(model_name="m1")
    Server.objects.create(model_id=model.id, base_url="http://a/v1", api_key=None)
    client = Client()
    payload = {"base_url": "http://a/v1", "model_name": "m1"}

    response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert response.json()["error"] == "server with this model/base_url/api_key already exists"


def test_add_server_allows_same_tuple_under_different_model():
    model = Model.objects.create(model_name="m1")
    Server.objects.create(model_id=model.id, base_url="http://a/v1", api_key="sk-1")
    client = Client()
    payload = {"base_url": "http://a/v1", "model_name": "m2", "api_key": "sk-1"}

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "m2"}]}
        mock_get.return_value = mock_resp

        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert Server.objects.filter(base_url="http://a/v1").count() == 2

def test_add_server_partial_failure():
    client = Client()
    payload = [
        {"base_url": "http://s1/v1", "model_name": "m1"},
        {"base_url": "http://s2/v1", "model_name": "m2"}
    ]
    
    with patch("requests.get") as mock_get:
        def side_effect(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if "s1" in url:
                mock_resp.json.return_value = {"data": [{"id": "m1"}]}
            else:
                mock_resp.json.return_value = {"data": []} # m2 not found
            return mock_resp
        
        mock_get.side_effect = side_effect
        
        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")
        
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 2
    assert "id" in data[0]
    assert "error" in data[1]
    
    assert ServerOperation.objects.filter(status="success").count() == 1
    assert ServerOperation.objects.filter(status="failed").count() == 1


def test_add_server_with_model_path():
    client = Client()
    payload = {
        "base_url": "http://test-server/v1",
        "model_name": "gpt-3.5-turbo",
        "model_path": "/models/gpt-3.5-turbo",
    }

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "gpt-3.5-turbo"}]}
        mock_get.return_value = mock_resp

        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["data"]["model_path"] == "/models/gpt-3.5-turbo"
    model = Model.objects.get(model_name="gpt-3.5-turbo")
    assert model.model_path == "/models/gpt-3.5-turbo"


def test_add_server_updates_existing_model_path():
    Model.objects.create(model_name="m1")
    client = Client()
    payload = {
        "base_url": "http://s1/v1",
        "model_name": "m1",
        "model_path": "/models/m1",
    }

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "m1"}]}
        mock_get.return_value = mock_resp

        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert Model.objects.get(model_name="m1").model_path == "/models/m1"


def test_add_server_prefix_prefiller_success():
    client = Client()
    payload = {
        "base_url": "http://pp/v1",
        "model_name": "m1",
        "role": "prefix-prefiller",
        "group_id": "g1",
    }

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "m1"}]}
        mock_get.return_value = mock_resp

        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["role"] == "prefix-prefiller"
    assert data["group_id"] == "g1"

    server = Server.objects.get(base_url="http://pp/v1")
    assert server.role == "prefix-prefiller"
    assert server.group_id == "g1"


def test_add_server_prefix_prefiller_requires_group_id():
    client = Client()
    payload = {
        "base_url": "http://pp-ng/v1",
        "model_name": "m1",
        "role": "prefix-prefiller",
    }

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "m1"}]}
        mock_get.return_value = mock_resp

        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "group_id is required" in response.json()["error"]


def test_add_server_rejects_unknown_role():
    client = Client()
    payload = {
        "base_url": "http://bad-role/v1",
        "model_name": "m1",
        "role": "p-prefiller",  # the issue's shorthand is not a valid role
        "group_id": "g1",
    }

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "m1"}]}
        mock_get.return_value = mock_resp

        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "role must be one of" in response.json()["error"]


def test_add_server_with_api_key_stores_masks_and_verifies_with_it():
    client = Client()
    payload = {
        "base_url": "http://keyed/v1",
        "model_name": "m1",
        "api_key": "sk-server-managed-key-123456",
    }

    captured = {}

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "m1"}]}

        def capture(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return mock_resp

        mock_get.side_effect = capture

        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["api_key"] == "sk-s…3456"
    assert "sk-server-managed-key-123456" not in json.dumps(data)

    server = Server.objects.get(base_url="http://keyed/v1")
    assert server.api_key == "sk-server-managed-key-123456"

    # The verification GET must authenticate with the server's key.
    assert captured["url"] == "http://keyed/v1/models"
    assert captured["headers"] == {"Authorization": "Bearer sk-server-managed-key-123456"}


def test_add_server_without_api_key_verifies_without_headers():
    client = Client()
    payload = {"base_url": "http://unkeyed/v1", "model_name": "m1"}

    captured = {}

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "m1"}]}

        def capture(url, **kwargs):
            captured["headers"] = kwargs.get("headers")
            return mock_resp

        mock_get.side_effect = capture

        response = client.post("/api/add_server", json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert captured["headers"] is None
    assert Server.objects.get(base_url="http://unkeyed/v1").api_key is None
