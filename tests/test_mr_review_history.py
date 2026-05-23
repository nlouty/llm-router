import json
import pytest
from django.test import Client
from router.models import MrReviewHistory

@pytest.mark.django_db
def test_upsert_mr_review_history():
    client = Client()
    url = "/api/mr_review_history"
    
    payload = {
        "project_id": 123,
        "branch": "feature-1",
        "issue_hash": "hash_abc",
        "mr_hash": "mr_123",
        "file_path": "src/app.py",
        "line": 15,
        "body": "Issue body",
        "review_comment": "Fix this",
        "severity": "medium",
        "categories": "security",
        "fix_suggestion": "Apply check",
        "created_at": "2023-01-01T10:00:00Z",
        "confidence_score": "0.95",
        "issue_url": "https://issue.com/1"
    }
    
    # 1. Create new
    response = client.post(url, data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    assert response.json()["message"] == "created"
    assert MrReviewHistory.objects.count() == 1
    
    # 2. Skip (same issue_hash)
    response = client.post(url, data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    assert response.json()["message"] == "skipped"
    assert MrReviewHistory.objects.count() == 1

@pytest.mark.django_db
def test_upsert_mr_review_history_invalid_field():
    client = Client()
    url = "/api/mr_review_history"
    payload = {
        "issue_hash": "hash_invalid",
        "invalid_field": "val"
    }
    response = client.post(url, data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 400
    assert "invalid fields: invalid_field" in response.json()["error"]
