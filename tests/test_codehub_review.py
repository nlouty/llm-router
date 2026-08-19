import pytest
import json
from django.test import Client
from router.models import CodehubReview


@pytest.mark.django_db
def test_create_codehub_review():
    client = Client()
    data = {
        "project_id": 1,
        "project_name": "demo-project",
        "branch_name": "main",
        "scan_commit_id": "abc123",
        "scan_date": "2026-01-01 10:00:00",
        "completion_date": "2026-01-02 10:00:00",
        "relative_path": "path/to/file",
        "line": 10,
        "issue_description": "body text",
        "severity": "high",
        "issue_category": "bug",
        "module": "core",
    }

    response = client.post("/api/codehub_review", data=json.dumps(data), content_type="application/json")
    assert response.status_code == 200
    assert response.json()["message"] == "created"

    review_id = response.json()["data"]["id"]
    review = CodehubReview.objects.get(id=review_id)
    assert review.project_id == 1
    assert review.project_name == "demo-project"
    assert review.branch_name == "main"
    assert review.scan_commit_id == "abc123"
    assert review.scan_date.strftime("%Y-%m-%d %H:%M:%S") == "2026-01-01 10:00:00"
    assert review.completion_date.strftime("%Y-%m-%d %H:%M:%S") == "2026-01-02 10:00:00"
    assert review.relative_path == "path/to/file"
    assert review.line == 10
    assert review.issue_description == "body text"
    assert review.severity == "high"
    assert review.issue_category == "bug"
    assert review.module == "core"
    # Timestamps and default applied by the view
    assert review.created_at is not None
    assert review.updated_at is not None
    assert review.is_modified_completed is False

    assert CodehubReview.objects.count() == 1


@pytest.mark.django_db
def test_create_codehub_review_invalid_fields():
    client = Client()
    data = {
        "project_id": 1,
        "branch_name": "main",
        "unknown_field": "some value"
    }
    response = client.post("/api/codehub_review", data=json.dumps(data), content_type="application/json")
    assert response.status_code == 400
    assert "invalid fields: unknown_field" in response.json()["error"]
    assert CodehubReview.objects.count() == 0


@pytest.mark.django_db
def test_create_codehub_review_invalid_date_format():
    client = Client()
    data = {
        "project_id": 1,
        "project_name": "demo-project",
        "branch_name": "main",
        "scan_commit_id": "abc123",
        "scan_date": "01/01/2026",
        "relative_path": "path/to/file",
        "line": 10,
        "issue_description": "body text",
        "severity": "high",
        "issue_category": "bug",
        "module": "core",
    }
    response = client.post("/api/codehub_review", data=json.dumps(data), content_type="application/json")
    assert response.status_code == 400
    assert "scan_date format invalid" in response.json()["error"]
    assert CodehubReview.objects.count() == 0
