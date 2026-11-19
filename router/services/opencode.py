from __future__ import annotations

import re

from packaging.version import Version, InvalidVersion

from router.config import APP_CONFIG

OPENCODE_UA_RE = re.compile(r"opencode/(\d+\.\d+\.\d+)")


class OpencodeVersionService:
    @staticmethod
    def _config() -> dict:
        return APP_CONFIG.get("opencode", {})

    @classmethod
    def _enabled(cls) -> bool:
        return bool(cls._config().get("enabled", True))

    @classmethod
    def _block_max_version(cls) -> Version:
        return Version(str(cls._config().get("block_max_version", "1.19.6")))

    @staticmethod
    def is_opencode(user_agent: str | None) -> bool:
        return bool(user_agent) and OPENCODE_UA_RE.search(user_agent) is not None

    @staticmethod
    def extract_version(user_agent: str | None) -> Version | None:
        if not user_agent:
            return None
        match = OPENCODE_UA_RE.search(user_agent)
        if not match:
            return None
        try:
            return Version(match.group(1))
        except InvalidVersion:
            return None

    @classmethod
    def should_block(cls, user_agent: str | None) -> tuple[bool, str | None]:
        if not cls._enabled():
            return False, None
        version = cls.extract_version(user_agent)
        if version is not None and version <= cls._block_max_version():
            return True, str(version)
        return False, str(version) if version is not None else None

    @classmethod
    def should_delay_failure(cls, user_agent: str | None, status_code: int) -> bool:
        if not cls._enabled():
            return False
        return status_code >= 400 and cls.is_opencode(user_agent)
