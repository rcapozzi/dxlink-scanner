"""Tests for the webhook sink."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from dxlink_scanner.models import Alert
from dxlink_scanner.sinks.webhook_sink import WebhookSink


@pytest.fixture
def alert() -> Alert:
    return Alert(
        symbol="SPY  260731C00450000:EQ",
        price=Decimal("1.23"),
        size=500,
        timestamp_ms=1722355200000,
        median_size=50.0,
        ratio=10.0,
        rule_name="size_mult",
    )


@pytest.mark.asyncio
async def test_webhook_success(alert: Alert):
    """Test successful webhook delivery."""
    sink = WebhookSink(url="http://localhost:8080/alerts")
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("dxlink_scanner.sinks.webhook_sink.httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__aenter__.return_value
        mock_client.post = AsyncMock(return_value=mock_response)

        await sink.send(alert)

        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://localhost:8080/alerts"
        assert call_args[1]["json"]["symbol"] == "SPY  260731C00450000:EQ"
        assert call_args[1]["json"]["size"] == 500


@pytest.mark.asyncio
async def test_webhook_retry_on_failure(alert: Alert):
    """Test that webhook retries on failure."""
    sink = WebhookSink(url="http://localhost:8080/alerts", max_retries=3)

    with patch("dxlink_scanner.sinks.webhook_sink.httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__aenter__.return_value
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("Connection error"))

        with patch("dxlink_scanner.sinks.webhook_sink.asyncio.sleep", new=AsyncMock()):
            await sink.send(alert)

        assert mock_client.post.await_count == 3
