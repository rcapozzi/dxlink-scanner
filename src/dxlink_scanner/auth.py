"""Tastytrade authentication using the official tastytrade SDK.

The SDK's Session class handles OAuth2 refresh-token flow, token lifecycle
management, and provides an authenticated httpx.AsyncClient for API calls.
This module wraps it with scanner-friendly initialization.
"""

from __future__ import annotations

import logging

from tastytrade.session import Session as TastyTradeSession

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://api.cert.tastyworks.com"
PRODUCTION_BASE = "https://api.tastyworks.com"


class TastyTradeAuth:
    """Manages Tastytrade authentication using the official SDK.

    The SDK's Session class handles:
    - OAuth2 refresh-token exchange (POST /oauth/token)
    - Access token lifecycle (auto-refresh via Session.refresh())
    - Base URL selection (sandbox vs production)
    - Bearer token headers on all API requests

    Attributes:
        client_id: Tastytrade username (used as OAuth client_id).
        client_secret: OAuth application client secret.
        refresh_token: OAuth2 refresh token from a Tastytrade Grant.
        sandbox: Whether to use the sandbox environment.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        sandbox: bool = False,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.sandbox = sandbox

    def get_session(self) -> TastyTradeSession:
        """Create and authenticate a Tastytrade SDK Session.

        The SDK session lazily authenticates on first API call.
        Call session.refresh(force=True) before use to ensure valid tokens.

        Returns:
            A TastyTradeSession instance (not yet authenticated — call
            session.refresh(force=True) to trigger OAuth2 exchange).
        """
        session = TastyTradeSession(
            provider_secret=self.client_secret,
            refresh_token=self.refresh_token,
            is_test=self.sandbox,
        )
        logger.info("Created Tastytrade session (sandbox=%s)", self.sandbox)
        return session

    @property
    def base_url(self) -> str:
        """Return the Tastytrade API base URL for this configuration."""
        return SANDBOX_BASE if self.sandbox else PRODUCTION_BASE
