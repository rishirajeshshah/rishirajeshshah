"""
NetSuite OAuth 2.0 Client Credentials authentication.

Supports two modes:
1. OAuth 2.0 (Client Credentials) — preferred for external integrations
2. Token-Based Auth (TBA) — for backwards compat with older NS setups

In demo mode, auth is bypassed and no real API calls are made.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx


@dataclass
class NetSuiteCredentials:
    account_id: str           # e.g. "1234567" — your NS account number
    client_id: str
    client_secret: str
    # Optional TBA credentials (alternative to OAuth)
    consumer_key: str = ""
    consumer_secret: str = ""
    token_id: str = ""
    token_secret: str = ""


@dataclass
class AccessToken:
    token: str
    expires_at: float  # Unix timestamp


class NetSuiteAuth:
    """Manages OAuth 2.0 Client Credentials token lifecycle."""

    def __init__(self, credentials: NetSuiteCredentials):
        self._creds = credentials
        self._token: AccessToken | None = None
        self._base_url = (
            f"https://{credentials.account_id}.suitetalk.api.netsuite.com"
        )
        self._token_url = (
            f"https://{credentials.account_id}.suitetalk.api.netsuite.com"
            "/services/rest/auth/oauth2/v1/token"
        )

    @property
    def rest_base(self) -> str:
        return self._base_url

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if near expiry."""
        if self._token and time.time() < self._token.expires_at - 60:
            return self._token.token

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._creds.client_id,
                    "client_secret": self._creds.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

        self._token = AccessToken(
            token=data["access_token"],
            expires_at=time.time() + int(data.get("expires_in", 3600)),
        )
        return self._token.token
