"""Shared-secret checks for the HTTP fleet host."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

TOKEN_HEADER = "X-Devicefleet-Token"
AGENT_HEADER = "X-Devicefleet-Agent"
SESSION_HEADER = "X-Devicefleet-Session"


def extract_bearer_or_header(request: Request) -> str | None:
    """Read `Authorization: Bearer` or `X-Devicefleet-Token`."""
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    header = request.headers.get(TOKEN_HEADER)
    if header and header.strip():
        return header.strip()
    return None


def tokens_match(provided: str, expected: str) -> bool:
    """Constant-time compare for fleet tokens."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def require_fleet_token(request: Request) -> None:
    """Reject protected routes when a token is configured and missing/wrong."""
    expected = request.app.state.fleet.settings.token
    if not expected:
        return
    provided = extract_bearer_or_header(request)
    if provided is None or not tokens_match(provided, expected):
        raise HTTPException(status_code=401, detail="invalid or missing fleet token")


def agent_from_headers(request: Request, fallback: str | None = None) -> str:
    """Agent label from `X-Devicefleet-Agent` (display / current-session key)."""
    header = request.headers.get(AGENT_HEADER)
    if header and header.strip():
        return header.strip()
    label = (fallback or "anonymous").strip()
    return label or "anonymous"


def agent_header_optional(request: Request) -> str | None:
    """Explicit agent header, or None when the caller omitted it."""
    header = request.headers.get(AGENT_HEADER)
    if header and header.strip():
        return header.strip()
    return None


def session_secret_from_headers(request: Request) -> str | None:
    """Capability token issued at session start (`X-Devicefleet-Session`)."""
    header = request.headers.get(SESSION_HEADER)
    if header and header.strip():
        return header.strip()
    return None


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def is_loopback_host(host: str) -> bool:
    """True when the bind address is local-only."""
    return host.strip().lower() in LOOPBACK_HOSTS


def validate_serve_bind(host: str, token: str | None) -> None:
    """Non-loopback binds (including 0.0.0.0) require an explicit token."""
    if is_loopback_host(host):
        return
    if not token or not token.strip():
        raise ValueError(
            f"binding to {host} requires DEVICEFLEET_TOKEN (or --token). "
            "Default bind is 127.0.0.1; only expose the host with a shared secret."
        )
