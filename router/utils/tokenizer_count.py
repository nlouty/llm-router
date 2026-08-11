"""Real tokenizer-based token counting (issue #227).

Counts request tokens with the model's tokenizer loaded from ``models.model_path``.
Any failure (no path, load error, encode error) yields ``0`` so the router never
blocks on tokenization; the elapsed encode time is returned for the per-request log.
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
_import_failed = False


def count_tokens_with_latency(model_path: str | None, text: str) -> tuple[int, int]:
    """Return ``(token_count, elapsed_ms)`` for *text* via the tokenizer at *model_path*.

    Returns ``(0, 0)`` when the path is empty, the tokenizer cannot be loaded, or
    encoding fails. Tokenizer loading is cached per path and not timed; only the
    encode call contributes to *elapsed_ms*.
    """
    if not model_path or not text:
        return 0, 0
    tokenizer = _get_tokenizer(model_path)
    if tokenizer is None:
        return 0, 0
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
        return 0, 0
    latency_ms = int((time.monotonic() - start) * 1000)
    return count, latency_ms


def _get_tokenizer(model_path: str):
    """Load and cache the tokenizer for *model_path*; fast first, slow fallback."""
    global _import_failed
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
            logger.warning("transformers unavailable; tokenizer counting disabled: %s", exc)
            return None
        tokenizer = None
        for use_fast in (True, False):
            try:
                tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=use_fast)
                break
            except Exception:
                continue
        if tokenizer is None:
            _failed_paths.add(model_path)
            logger.warning("no tokenizer loaded for model_path=%s; counting 0", model_path)
            return None
        _tokenizers[model_path] = tokenizer
        return tokenizer
