import logging
import time

from router.services.disconnect import ClientDisconnectTracker

logger = logging.getLogger(__name__)


class ClientDisconnectMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.client_disconnect_tracker = ClientDisconnectTracker(request.META.get("gunicorn.socket"))
        return self.get_response(request)


class APITimingMiddleware:
    """记录API调用耗时的中间件"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # /v1/* 请求量大且有独立的请求日志，跳过中间件日志（issue #232）
        if request.path.startswith("/v1/"):
            return self.get_response(request)

        # 记录请求开始时间
        start_time = time.time()

        # 处理请求
        response = self.get_response(request)

        # 计算耗时（毫秒）
        duration_ms = (time.time() - start_time) * 1000

        # 记录日志：方法 路径 状态码 耗时
        logger.info(
            "%s %s %s %.2fms",
            request.method,
            request.path,
            response.status_code,
            duration_ms
        )

        return response
