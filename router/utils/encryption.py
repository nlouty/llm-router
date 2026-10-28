from __future__ import annotations

import logging

from cryptography.fernet import Fernet

from router.config import APP_CONFIG

logger = logging.getLogger(__name__)


def get_cipher() -> Fernet | None:
    """Build a Fernet cipher from the shared ``encryption.emp_token_secret_key``.

    Returns ``None`` when the key is unset or malformed — a server
    misconfiguration the caller must surface, not silently work around.
    """
    key = str(APP_CONFIG.get("encryption", {}).get("emp_token_secret_key") or "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except (TypeError, ValueError):
        logger.error(
            "encryption.emp_token_secret_key is not a valid Fernet key; generate one with "
            '"python -c \'from cryptography.fernet import Fernet; print(Fernet.generate_key())\'"'
        )
        return None
