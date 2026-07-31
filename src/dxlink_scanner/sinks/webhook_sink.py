"""Webhook sink: sends alerts to a webhook endpoint with retry logic."""

from __future__ import annotations

import asyncio
import logging

import httpx

from dxlink_scanner.models import Alert
from dxlink_scanner.sinks.stdout_sink import _alert_to_dict

logger = logging.getLogger(__name__)


class WebhookSink:
    """Send alerts to a webhook endpoint with retry logic.

    Attributes:
        _url: Webhook URL.
        _timeout: Request timeout in seconds.
        _max_retries: Maximum number of retry attempts on failure.
    """

    def __init__(
        self,
        url: str,
        timeout: float = 5.0,
        max_retries: int = 3,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._max_retries = max_retries

    async def send(self, alert: Alert) -> None:
        """Send an alert to the webhook, retrying on failure."""
        payload = _alert_to_dict(alert)

        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        self._url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    resp.raise_for_status()
                logger.debug("Alert sent to webhook: %s", resp.status_code)
                return
            except (httpx.HTTPError, httpx.RequestError) as e:
                logger.warning(
                    "Webhook attempt %d/%d failed: %s",
                    attempt + 1,
                    self._max_retries,
                    e,
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(2**attempt)  # exponential backoff
                else:
                    logger.error("Webhook delivery failed after %d attempts", self._max_retries)
