"""Real tokenizer-based token counting (issue #227).

Counts request tokens with the model's tokenizer loaded from ``models.model_path``.
Any failure (no path, load error, encode error) yields ``0`` so the router never
blocks on tokenization; the elapsed encode time is returned for the per-request
log, and a human-readable failure ``reason`` is returned so the request log can
explain why a count is 0 instead of failing silently.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_tokenizers: dict[str, Any] = {}
_failed_paths: set[str] = set()
_load_errors: dict[str, str] = {}
_import_failed = False
_import_error: str | None = None


def count_tokens_with_latency(model_path: str | None, text: str) -> tuple[int, int, str | None]:
    """Return ``(token_count, elapsed_ms, reason)`` for *text* via the tokenizer at *model_path*.

    The ``reason`` is ``None`` on success and a short diagnostic string whenever
    the count is 0, so callers can record *why* tokenization did not run in the
    per-request log. Tokenizer loading is cached per path and not timed; only the
    encode call contributes to *elapsed_ms*.
    """
    if not model_path:
        return 0, 0, "no model_path configured for model"
    if not text:
        return 0, 0, "empty request body"
    tokenizer = _get_tokenizer(model_path)
    if tokenizer is None:
        if _import_failed:
            return 0, 0, f"transformers unavailable: {_import_error or 'unknown error'}"
        detail = _load_errors.get(model_path, "unknown error")
        return 0, 0, f"tokenizer load failed for model_path={model_path}: {detail}"
    start = time.monotonic()
    try:
        if getattr(tokenizer, "is_fast", False):
            # Rust-backed fast tokenizers are thread-safe for concurrent encode.
            count = len(tokenizer.encode(text, add_special_tokens=True))
        else:
            # Slow (Python) tokenizers mutate internal state during encode.
            with _lock:
                count = len(tokenizer.encode(text, add_special_tokens=True))
    except Exception as exc:
        logger.warning("tokenizer encode failed for model_path=%s: %s", model_path, exc)
        return 0, 0, f"tokenizer encode failed for model_path={model_path}: {exc}"
    latency_ms = int((time.monotonic() - start) * 1000)
    return count, latency_ms, None


def _get_tokenizer(model_path: str):
    """Load and cache the tokenizer for *model_path*; fast first, slow fallback."""
    global _import_failed, _import_error
    if model_path in _tokenizers or model_path in _failed_paths or _import_failed:
        return _tokenizers.get(model_path)
    with _lock:
        if model_path in _tokenizers:
            return _tokenizers[model_path]
        if model_path in _failed_paths or _import_failed:
            return None
        try:
            from transformers import AutoTokenizer  # lazy: app boots without it
        except Exception as exc:
            _import_failed = True
            _import_error = str(exc) or exc.__class__.__name__
            logger.warning("transformers unavailable; tokenizer counting disabled: %s", exc)
            return None
        tokenizer = None
        last_error = None
        for use_fast in (True, False):
            try:
                tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=use_fast)
                break
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                continue
        if tokenizer is None:
            _failed_paths.add(model_path)
            _load_errors[model_path] = last_error or "unknown error"
            logger.warning(
                "no tokenizer loaded for model_path=%s (%s); counting 0",
                model_path,
                last_error or "unknown error",
            )
            return None
        _load_errors.pop(model_path, None)
        _tokenizers[model_path] = tokenizer
        return tokenizer
