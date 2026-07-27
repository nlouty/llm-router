"""
Test token_summary API with time range > 2 days
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import Client
from django.utils import timezone

from router.models import Model, RequestRecord


def _dt(value):
    return timezone.make_aware(datetime.strptime(value, "%Y-%m-%d %H:%M:%S"), ZoneInfo("Asia/Shanghai"))


def _request(model, send_time, input_tokens=100, output_tokens=50, cache_hit=0, task_status="success"):
    """Create a request record with token counts"""
    return RequestRecord.objects.create(
        user_ip_id=1,
        ip_id=1,
        send_time=_dt(send_time),
        end_time=_dt(send_time),
        latency=100,
        model_id=model.id,
        task_status=task_status,
        input_token_cnt=input_tokens,
        output_token_cnt=output_tokens,
        final_prefix_cache=cache_hit,
    )


def test_token_summary_returns_single_aggregate_for_short_range():
    """Time range <= 2 days should return single aggregate"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests within 1 day
    _request(model, "2026-01-01 10:00:00", input_tokens=100, output_tokens=50, cache_hit=20)
    _request(model, "2026-01-01 11:00:00", input_tokens=200, output_tokens=100, cache_hit=40)
    
    response = client.get(
        "/api/token_summary",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-01-01 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return single aggregate (not stats array)
    assert "input_token" in data
    assert "output_token" in data
    assert "cache_hit" in data
    assert "cache_miss" in data
    assert "hit_rate" in data
    assert "stats" not in data
    
    # Verify values
    assert data["input_token"] == 300
    assert data["output_token"] == 150
    assert data["cache_hit"] == 60
    assert data["cache_miss"] == 240
    assert data["hit_rate"] == 20.0  # 60/300 = 20%


def test_token_summary_returns_bucketed_data_for_long_range():
    """Time range > 2 days should return bucketed data (per day)"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests across 3 days
    _request(model, "2026-01-01 10:00:00", input_tokens=100, output_tokens=50, cache_hit=20)
    _request(model, "2026-01-02 10:00:00", input_tokens=200, output_tokens=100, cache_hit=80)
    _request(model, "2026-01-03 10:00:00", input_tokens=300, output_tokens=150, cache_hit=150)
    
    response = client.get(
        "/api/token_summary",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-01-03 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return stats array (bucketed by day)
    assert "stats" in data
    assert isinstance(data["stats"], list)
    assert len(data["stats"]) == 3  # 3 days
    
    # Check structure of each stat entry
    for stat in data["stats"]:
        assert "date" in stat
        assert "input_token" in stat
        assert "output_token" in stat
        assert "cache_hit" in stat
        assert "cache_miss" in stat
        assert "hit_rate" in stat
    
    # Verify day 1
    day1 = data["stats"][0]
    assert day1["date"] == "2026-01-01"
    assert day1["input_token"] == 100
    assert day1["output_token"] == 50
    assert day1["cache_hit"] == 20
    assert day1["cache_miss"] == 80
    assert day1["hit_rate"] == 20.0
    
    # Verify day 2
    day2 = data["stats"][1]
    assert day2["date"] == "2026-01-02"
    assert day2["input_token"] == 200
    assert day2["output_token"] == 100
    assert day2["cache_hit"] == 80
    assert day2["cache_miss"] == 120
    assert day2["hit_rate"] == 40.0
    
    # Verify day 3
    day3 = data["stats"][2]
    assert day3["date"] == "2026-01-03"
    assert day3["input_token"] == 300
    assert day3["output_token"] == 150
    assert day3["cache_hit"] == 150
    assert day3["cache_miss"] == 150
    assert day3["hit_rate"] == 50.0


def test_token_summary_with_model_filter():
    """Token summary with model filter should work for long range"""
    client = Client()
    model_a = Model.objects.create(model_name="model-a", concurrent_limit=3)
    model_b = Model.objects.create(model_name="model-b", concurrent_limit=3)
    
    # Create requests for both models across 3 days
    _request(model_a, "2026-01-01 10:00:00", input_tokens=100, output_tokens=50, cache_hit=20)
    _request(model_b, "2026-01-01 10:00:00", input_tokens=200, output_tokens=100, cache_hit=40)
    _request(model_a, "2026-01-02 10:00:00", input_tokens=150, output_tokens=75, cache_hit=30)
    
    response = client.get(
        "/api/token_summary",
        {
            "start_time": "2026-01-01 00:00:00",
            "end_time": "2026-01-03 23:59:59",
            "model_name": "model-a"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["model_name"] == "model-a"
    assert "stats" in data
    
    # Should only count model-a requests
    day1 = data["stats"][0]
    assert day1["input_token"] == 100
    assert day1["output_token"] == 50
    
    day2 = data["stats"][1]
    assert day2["input_token"] == 150
    assert day2["output_token"] == 75


def test_token_summary_handles_zero_values():
    """Token summary should handle days with no data"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests only on day 1 and day 3
    _request(model, "2026-01-01 10:00:00", input_tokens=100, output_tokens=50, cache_hit=20)
    _request(model, "2026-01-03 10:00:00", input_tokens=200, output_tokens=100, cache_hit=80)
    
    response = client.get(
        "/api/token_summary",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-01-03 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert len(data["stats"]) == 3
    
    # Day 2 should have zero values
    day2 = data["stats"][1]
    assert day2["date"] == "2026-01-02"
    assert day2["input_token"] == 0
    assert day2["output_token"] == 0
    assert day2["cache_hit"] == 0
    assert day2["cache_miss"] == 0
    assert day2["hit_rate"] == 0.0


def test_token_summary_hit_rate_calculation():
    """Verify hit_rate is calculated correctly"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # 50% hit rate: 100 input, 50 cache hit
    _request(model, "2026-01-01 10:00:00", input_tokens=100, output_tokens=50, cache_hit=50)
    
    response = client.get(
        "/api/token_summary",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-01-03 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    day1 = data["stats"][0]
    assert day1["hit_rate"] == 50.0  # 50/100 = 50%
    assert day1["cache_miss"] == 50  # 100 - 50