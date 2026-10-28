from router.config import load_config


def test_prefix_cache_threshold_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("PREFIX_CACHE_PRIMARY_MATCH_THRESHOLD", "0.95")
    monkeypatch.setenv("PREFIX_CACHE_SECONDARY_MATCH_THRESHOLD", "0.45")
    monkeypatch.setenv("PREFIX_CACHE_MAX_PREFIX_CHARS", "4096")
    monkeypatch.setenv("PREFIX_CACHE_BLOCK_CHARS", "64")

    config = load_config()

    assert config["prefix_cache"]["primary_match_threshold"] == "0.95"
    assert config["prefix_cache"]["secondary_match_threshold"] == "0.45"
    assert config["prefix_cache"]["max_prefix_chars"] == 4096
    assert config["prefix_cache"]["prefix_block_chars"] == 64


def test_http_port_env_updates_server_bind(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("HTTP_PORT", "9000")

    config = load_config()

    assert config["server"]["bind"] == "0.0.0.0:9000"


def test_log_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("LLM_ROUTER_LOG_PATH", "/data/router_log")

    config = load_config()

    assert config["log_path"] == "/data/router_log"


def test_emp_token_secret_key_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("EMP_TOKEN_SECRET_KEY", "abc123")

    config = load_config()

    assert config["encryption"]["emp_token_secret_key"] == "abc123"
