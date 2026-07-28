"""
Test input_token and output_token APIs with time range > 2 months
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


# ============= input_token API tests =============

def test_input_token_hourly_for_short_range():
    """Time range <= 2 days should return hourly data"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests within 1 day
    _request(model, "2026-01-01 10:00:00", input_tokens=100, output_tokens=50, cache_hit=20)
    _request(model, "2026-01-01 11:00:00", input_tokens=200, output_tokens=100, cache_hit=40)
    
    response = client.get(
        "/api/input_token",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-01-01 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return stats array with hourly buckets
    assert "stats" in data
    assert isinstance(data["stats"], list)
    # Check time format for hour granularity
    assert "2026-01-01 10:00:00" in [s["time"] for s in data["stats"]]


def test_input_token_daily_for_medium_range():
    """Time range 2-31 days should return daily data"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests across 5 days
    _request(model, "2026-01-01 10:00:00", input_tokens=100, output_tokens=50, cache_hit=20)
    _request(model, "2026-01-03 10:00:00", input_tokens=200, output_tokens=100, cache_hit=80)
    
    response = client.get(
        "/api/input_token",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-01-05 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return stats array with daily buckets
    assert "stats" in data
    assert isinstance(data["stats"], list)
    assert len(data["stats"]) == 5  # 5 days
    
    # Check time format for day granularity (YYYY-MM-DD)
    day1 = data["stats"][0]
    assert day1["time"] == "2026-01-01"
    assert day1["total_input_tokens"] == 100
    assert day1["cache_hit"] == 20
    assert day1["cache_miss"] == 80


def test_input_token_monthly_for_long_range():
    """Time range > 31 days should return monthly data"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests across 3 months
    _request(model, "2026-01-15 10:00:00", input_tokens=1000, output_tokens=500, cache_hit=200)
    _request(model, "2026-02-15 10:00:00", input_tokens=2000, output_tokens=1000, cache_hit=600)
    _request(model, "2026-03-15 10:00:00", input_tokens=3000, output_tokens=1500, cache_hit=1200)
    
    response = client.get(
        "/api/input_token",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-03-31 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return stats array with monthly buckets
    assert "stats" in data
    assert isinstance(data["stats"], list)
    assert len(data["stats"]) == 3  # 3 months
    
    # Check time format for month granularity (YYYY-MM)
    jan = data["stats"][0]
    assert jan["time"] == "2026-01"
    assert jan["total_input_tokens"] == 1000
    assert jan["cache_hit"] == 200
    assert jan["cache_miss"] == 800
    
    feb = data["stats"][1]
    assert feb["time"] == "2026-02"
    assert feb["total_input_tokens"] == 2000
    assert feb["cache_hit"] == 600
    assert feb["cache_miss"] == 1400
    
    mar = data["stats"][2]
    assert mar["time"] == "2026-03"
    assert mar["total_input_tokens"] == 3000
    assert mar["cache_hit"] == 1200
    assert mar["cache_miss"] == 1800


def test_input_token_with_model_filter_monthly():
    """input_token with model filter should work for monthly data"""
    client = Client()
    model_a = Model.objects.create(model_name="model-a", concurrent_limit=3)
    model_b = Model.objects.create(model_name="model-b", concurrent_limit=3)
    
    # Create requests for both models across 3 months
    _request(model_a, "2026-01-15 10:00:00", input_tokens=100, output_tokens=50, cache_hit=20)
    _request(model_b, "2026-01-15 10:00:00", input_tokens=500, output_tokens=250, cache_hit=100)
    _request(model_a, "2026-02-15 10:00:00", input_tokens=200, output_tokens=100, cache_hit=60)
    
    response = client.get(
        "/api/input_token",
        {
            "start_time": "2026-01-01 00:00:00",
            "end_time": "2026-02-28 23:59:59",
            "model_name": "model-a"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["model_name"] == "model-a"
    
    # Should only count model-a requests
    jan = data["stats"][0]
    assert jan["total_input_tokens"] == 100
    assert jan["cache_hit"] == 20
    
    feb = data["stats"][1]
    assert feb["total_input_tokens"] == 200
    assert feb["cache_hit"] == 60


# ============= output_token API tests =============

def test_output_token_hourly_for_short_range():
    """Time range <= 2 days should return hourly data"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests within 1 day
    _request(model, "2026-01-01 10:00:00", output_tokens=50)
    _request(model, "2026-01-01 11:00:00", output_tokens=100)
    
    response = client.get(
        "/api/output_token",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-01-01 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return stats array with hourly buckets
    assert "stats" in data
    assert isinstance(data["stats"], list)
    # Check time format for hour granularity
    times = [s["time"] for s in data["stats"]]
    assert "2026-01-01 10:00:00" in times
    assert "2026-01-01 11:00:00" in times


def test_output_token_daily_for_medium_range():
    """Time range 2-31 days should return daily data"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests across 5 days
    _request(model, "2026-01-01 10:00:00", output_tokens=50)
    _request(model, "2026-01-03 10:00:00", output_tokens=100)
    
    response = client.get(
        "/api/output_token",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-01-05 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return stats array with daily buckets
    assert "stats" in data
    assert isinstance(data["stats"], list)
    assert len(data["stats"]) == 5  # 5 days
    
    # Check time format for day granularity (YYYY-MM-DD)
    day1 = data["stats"][0]
    assert day1["time"] == "2026-01-01"
    assert day1["total_output_tokens"] == 50


def test_output_token_monthly_for_long_range():
    """Time range > 31 days should return monthly data"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests across 3 months
    _request(model, "2026-01-15 10:00:00", output_tokens=500)
    _request(model, "2026-02-15 10:00:00", output_tokens=1000)
    _request(model, "2026-03-15 10:00:00", output_tokens=1500)
    
    response = client.get(
        "/api/output_token",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-03-31 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return stats array with monthly buckets
    assert "stats" in data
    assert isinstance(data["stats"], list)
    assert len(data["stats"]) == 3  # 3 months
    
    # Check time format for month granularity (YYYY-MM)
    jan = data["stats"][0]
    assert jan["time"] == "2026-01"
    assert jan["total_output_tokens"] == 500
    
    feb = data["stats"][1]
    assert feb["time"] == "2026-02"
    assert feb["total_output_tokens"] == 1000
    
    mar = data["stats"][2]
    assert mar["time"] == "2026-03"
    assert mar["total_output_tokens"] == 1500


def test_output_token_with_model_filter_monthly():
    """output_token with model filter should work for monthly data"""
    client = Client()
    model_a = Model.objects.create(model_name="model-a", concurrent_limit=3)
    model_b = Model.objects.create(model_name="model-b", concurrent_limit=3)
    
    # Create requests for both models across 2 months
    _request(model_a, "2026-01-15 10:00:00", output_tokens=50)
    _request(model_b, "2026-01-15 10:00:00", output_tokens=250)
    _request(model_a, "2026-02-15 10:00:00", output_tokens=100)
    
    response = client.get(
        "/api/output_token",
        {
            "start_time": "2026-01-01 00:00:00",
            "end_time": "2026-02-28 23:59:59",
            "model_name": "model-a"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["model_name"] == "model-a"
    
    # Should only count model-a requests
    jan = data["stats"][0]
    assert jan["total_output_tokens"] == 50
    
    feb = data["stats"][1]
    assert feb["total_output_tokens"] == 100


def test_output_token_handles_zero_values():
    """output_token should handle months with no data"""
    client = Client()
    model = Model.objects.create(model_name="model-a", concurrent_limit=3)
    
    # Create requests only on month 1 and month 3
    _request(model, "2026-01-15 10:00:00", output_tokens=50)
    _request(model, "2026-03-15 10:00:00", output_tokens=100)
    
    response = client.get(
        "/api/output_token",
        {"start_time": "2026-01-01 00:00:00", "end_time": "2026-03-31 23:59:59"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert len(data["stats"]) == 3
    
    # Month 2 should have zero values
    feb = data["stats"][1]
    assert feb["time"] == "2026-02"
    assert feb["total_output_tokens"] == 0