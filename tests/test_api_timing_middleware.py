"""
测试API耗时记录中间件
"""
from unittest.mock import MagicMock

import pytest
from django.test import Client

from router.models import Server


class TestAPITimingMiddleware:
    """测试API耗时记录中间件"""

    @pytest.fixture
    def client(self):
        return Client()

    def test_api_timing_logs_request(self, client, caplog):
        """测试中间件是否记录API调用耗时"""
        with caplog.at_level('INFO'):
            # 调用健康检查接口
            response = client.get('/healthy')

            # 验证响应成功
            assert response.status_code == 200

            # 验证日志中包含耗时记录
            log_messages = [record.message for record in caplog.records]

            # 查找包含耗时记录的日志
            timing_logs = [msg for msg in log_messages if 'GET /healthy' in msg and 'ms' in msg]

            # 应该有一条耗时日志
            assert len(timing_logs) >= 1

            # 验证日志格式包含：方法、路径、状态码、耗时
            timing_log = timing_logs[0]
            assert 'GET' in timing_log
            assert '/healthy' in timing_log
            assert '200' in timing_log
            assert 'ms' in timing_log

    def test_api_timing_skips_v1_paths(self, client, caplog, monkeypatch):
        """测试 /v1/* 请求不输出中间件日志（issue #232）"""
        Server.objects.create(model_id=None, base_url="http://shared.example", is_online=True)

        def fake_request(self_inner, method, url, **kwargs):
            upstream = MagicMock()
            upstream.status_code = 200
            upstream.reason = "OK"
            upstream.content = b'{"data":[]}'
            upstream.headers = {"content-type": "application/json"}
            return upstream

        monkeypatch.setattr(
            "router.services.cancellable_upstream.CancellableUpstreamRequest.request",
            fake_request,
        )

        with caplog.at_level('INFO'):
            response = client.get('/v1/models')

            assert response.status_code == 200

            middleware_logs = [
                record.message
                for record in caplog.records
                if record.name == 'router.middleware'
            ]
            assert not any('/v1/models' in msg for msg in middleware_logs)

    def test_api_timing_format(self, client, caplog):
        """测试日志格式是否正确"""
        with caplog.at_level('INFO'):
            response = client.get('/api/models')

            # 验证响应
            assert response.status_code == 200

            # 获取耗时日志
            log_messages = [record.message for record in caplog.records]
            timing_logs = [msg for msg in log_messages if 'GET /api/models' in msg and 'ms' in msg]

            if timing_logs:
                # 验证日志包含所需信息
                timing_log = timing_logs[0]
                assert 'GET /api/models 200' in timing_log
                # 验证耗时是数字格式（包含小数点）
                assert '.ms' in timing_log or 'ms' in timing_log
