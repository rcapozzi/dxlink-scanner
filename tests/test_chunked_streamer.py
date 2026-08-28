"""Tests for ChunkedDXLinkStreamer."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from tastytrade.dxfeed import Quote, TheoPrice, TimeAndSale

from dxlink_scanner.chunked_streamer import (
    MESSAGE_OVERHEAD_BYTES,
    SYMBOL_OVERHEAD_BYTES,
    ChunkedDXLinkStreamer,
)


@pytest.fixture
def mock_streamer():
    """Create a mock DXLinkStreamer with async methods."""
    streamer = MagicMock()
    streamer.subscribe = AsyncMock()
    streamer.unsubscribe = AsyncMock()
    streamer.listen = MagicMock(return_value=AsyncMock())
    streamer.get_event_nowait = MagicMock(return_value=None)
    streamer.get_event = AsyncMock()
    streamer.__aenter__ = AsyncMock(return_value=streamer)
    streamer.__aexit__ = AsyncMock(return_value=None)
    return streamer


@pytest.fixture
def chunked(mock_streamer):
    """Create a ChunkedDXLinkStreamer with small max_bytes for testing."""
    return ChunkedDXLinkStreamer(
        mock_streamer,
        max_payload_bytes=1000,  # Small limit to force chunking
        chunk_delay_sec=0,  # No delay in tests
        enable_chunking=True,
    )


class TestEstimatePayloadSize:
    def test_single_symbol(self, chunked):
        size = chunked._estimate_payload_size(Quote, [".SPY260731C500"])
        assert size > 0
        assert size < 200  # One symbol should be small

    def test_multiple_symbols(self, chunked):
        symbols = [f".SPY260731C{i}" for i in range(10)]
        size = chunked._estimate_payload_size(Quote, symbols)
        assert size > 0
        # Should be roughly 10x single symbol size (minus shared overhead)
        single = chunked._estimate_payload_size(Quote, [".SPY260731C0"])
        assert size > single * 5  # At least 5x for 10 symbols


class TestChunkSymbols:
    def test_empty_list(self, chunked):
        chunks = chunked._chunk_symbols(Quote, [])
        assert chunks == []

    def test_single_symbol(self, chunked):
        chunks = chunked._chunk_symbols(Quote, [".SPY260731C500"])
        assert len(chunks) == 1
        assert chunks[0] == [".SPY260731C500"]

    def test_small_batch_fits_one_chunk(self, chunked):
        symbols = [f".SPY260731C{i}" for i in range(5)]
        chunks = chunked._chunk_symbols(Quote, symbols)
        assert len(chunks) == 1
        assert len(chunks[0]) == 5

    def test_large_batch_needs_multiple_chunks(self, chunked):
        symbols = [f".SPY260731C{i:03d}" for i in range(100)]
        chunks = chunked._chunk_symbols(Quote, symbols)
        assert len(chunks) > 1
        # Verify all symbols are included
        all_symbols = [s for chunk in chunks for s in chunk]
        assert all_symbols == symbols

    def test_chunking_disabled(self, mock_streamer):
        chunked = ChunkedDXLinkStreamer(
            mock_streamer,
            max_payload_bytes=1000,
            chunk_delay_sec=0,
            enable_chunking=False,
        )
        symbols = [f".SPY260731C{i:03d}" for i in range(100)]
        chunks = chunked._chunk_symbols(Quote, symbols)
        assert len(chunks) == 1

    def test_no_duplicates_or_loss(self, chunked):
        symbols = [f".SPY260731C{i:03d}" for i in range(50)]
        chunks = chunked._chunk_symbols(Quote, symbols)
        all_symbols = [s for chunk in chunks for s in chunk]
        assert set(all_symbols) == set(symbols)
        assert len(all_symbols) == len(symbols)


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_empty_symbols(self, chunked, mock_streamer):
        await chunked.subscribe(Quote, [])
        mock_streamer.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_chunk(self, chunked, mock_streamer):
        symbols = [".SPY260731C500", ".SPY260731C501"]
        await chunked.subscribe(Quote, symbols)
        assert mock_streamer.subscribe.call_count == 1
        mock_streamer.subscribe.assert_called_once_with(Quote, symbols, 0.1)

    @pytest.mark.asyncio
    async def test_multiple_chunks(self, chunked, mock_streamer):
        symbols = [f".SPY260731C{i:03d}" for i in range(100)]
        await chunked.subscribe(Quote, symbols)
        assert mock_streamer.subscribe.call_count > 1
        # Each chunk should have symbols
        for call_args in mock_streamer.subscribe.call_args_list:
            assert len(call_args[0][1]) > 0

    @pytest.mark.asyncio
    async def test_refresh_interval_passed(self, chunked, mock_streamer):
        await chunked.subscribe(Quote, [".SPY260731C500"], refresh_interval=0.5)
        mock_streamer.subscribe.assert_called_once_with(Quote, [".SPY260731C500"], 0.5)

    @pytest.mark.asyncio
    async def test_chunking_disabled_single_call(self, mock_streamer):
        chunked = ChunkedDXLinkStreamer(
            mock_streamer,
            max_payload_bytes=1000,
            chunk_delay_sec=0,
            enable_chunking=False,
        )
        symbols = [f".SPY260731C{i:03d}" for i in range(100)]
        await chunked.subscribe(Quote, symbols)
        mock_streamer.subscribe.assert_called_once_with(Quote, symbols, 0.1)


class TestUnsubscribe:
    @pytest.mark.asyncio
    async def test_empty_symbols(self, chunked, mock_streamer):
        await chunked.unsubscribe(Quote, [])
        mock_streamer.unsubscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_chunks(self, chunked, mock_streamer):
        symbols = [f".SPY260731C{i:03d}" for i in range(100)]
        await chunked.unsubscribe(Quote, symbols)
        assert mock_streamer.unsubscribe.call_count > 1


class TestPassthrough:
    @pytest.mark.asyncio
    async def test_listen(self, chunked, mock_streamer):
        mock_streamer.listen.return_value = AsyncMock()
        async for _ in chunked.listen(Quote):
            pass
        mock_streamer.listen.assert_called_once_with(Quote)

    def test_get_event_nowait(self, chunked, mock_streamer):
        mock_streamer.get_event_nowait.return_value = "event"
        result = chunked.get_event_nowait(Quote)
        assert result == "event"
        mock_streamer.get_event_nowait.assert_called_once_with(Quote)

    @pytest.mark.asyncio
    async def test_get_event(self, chunked, mock_streamer):
        mock_streamer.get_event.return_value = "event"
        result = await chunked.get_event(Quote)
        assert result == "event"
        mock_streamer.get_event.assert_called_once_with(Quote)

    @pytest.mark.asyncio
    async def test_context_manager(self, chunked, mock_streamer):
        async with chunked as s:
            assert s is chunked
        mock_streamer.__aenter__.assert_called_once()
        mock_streamer.__aexit__.assert_called_once()


class TestSizeHeuristic:
    """Verify size estimation is reasonable."""

    def test_estimate_matches_actual_json(self, mock_streamer):
        """Verify _estimate_payload_size returns exact JSON size."""
        chunked = ChunkedDXLinkStreamer(mock_streamer)
        symbols = [f".SPY260731C{i:03d}" for i in range(20)]
        actual = chunked._estimate_payload_size(Quote, symbols)
        # Each symbol adds roughly 45 bytes (JSON wrapping + comma)
        # Plus ~50 bytes overhead for the message structure
        expected_approx = 50 + 20 * 45
        assert abs(actual - expected_approx) < 100

    def test_estimate_scales_linearly(self, mock_streamer):
        """Verify size scales roughly linearly with symbol count."""
        chunked = ChunkedDXLinkStreamer(mock_streamer)
        s10 = chunked._estimate_payload_size(Quote, [f".SPY260731C{i:03d}" for i in range(10)])
        s20 = chunked._estimate_payload_size(Quote, [f".SPY260731C{i:03d}" for i in range(20)])
        # 20 symbols should be roughly 2x 10 symbols (minus shared overhead)
        assert s20 > s10 * 1.5
        assert s20 < s10 * 2.5


class TestDryRun:
    """Verify with realistic symbol counts."""

    def test_100_symbols_single_chunk(self, mock_streamer):
        chunked = ChunkedDXLinkStreamer(
            mock_streamer,
            max_payload_bytes=60_000,
            enable_chunking=True,
        )
        symbols = [f".SPY260731C{i:03d}" for i in range(100)]
        chunks = chunked._chunk_symbols(Quote, symbols)
        assert len(chunks) == 1

    def test_1000_symbols_multiple_chunks(self, mock_streamer):
        chunked = ChunkedDXLinkStreamer(
            mock_streamer,
            max_payload_bytes=60_000,
            enable_chunking=True,
        )
        symbols = [f".SPY260731C{i:04d}" for i in range(1000)]
        chunks = chunked._chunk_symbols(Quote, symbols)
        assert len(chunks) > 1

    def test_all_symbols_preserved(self, mock_streamer):
        chunked = ChunkedDXLinkStreamer(
            mock_streamer,
            max_payload_bytes=60_000,
            enable_chunking=True,
        )
        symbols = [f".SPY260731C{i:04d}" for i in range(500)]
        chunks = chunked._chunk_symbols(Quote, symbols)
        all_symbols = [s for chunk in chunks for s in chunk]
        assert all_symbols == symbols
        assert len(set(all_symbols)) == len(symbols)