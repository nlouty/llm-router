from __future__ import annotations

# Fixed priority order across the supported agents. Headers are matched
# case-insensitively; the raw session id is stored without an agent prefix
# because the user-agent column already identifies the agent.
SESSION_HEADERS = (
    "Chrys-Session-Id",
    "X-Session-Affinity",
    "x-session-id",
    "X-Snap-Traceid",
)


def extract_session_id(headers: dict | None) -> str | None:
    if not headers:
        return None
    lowered = {str(key).lower(): value for key, value in headers.items()}
    for header in SESSION_HEADERS:
        value = lowered.get(header.lower())
        if value is None:
            continue
        session_id = str(value).strip()
        if session_id:
            return session_id[:255]
    return None
