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
    assert response.json()["error"] == "duplicate base_url in request"
    assert ServerOperation.objects.count() == 0

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
