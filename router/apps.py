from django.apps import AppConfig


class RouterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "router"

    def ready(self):
        from router.config import APP_CONFIG
        proxy_config = APP_CONFIG.get("proxy", {})
        stale_min = int(proxy_config.get("stale_processing_minutes", 66))
        timeout_sec = float(proxy_config.get("stream_total_timeout_seconds", 3660))
        
        # Guarantee: cleanup threshold must be at least 5 minutes longer than max
        # request timeout (issue #305), so in-flight requests are never reaped as
        # stale while still running.
        if stale_min * 60 < timeout_sec + 300:
            raise RuntimeError(
                f"Configuration Error: stale_processing_minutes ({stale_min}) must be at least 5 minutes "
                f"greater than stream_total_timeout_seconds ({timeout_sec}s)."
            )
