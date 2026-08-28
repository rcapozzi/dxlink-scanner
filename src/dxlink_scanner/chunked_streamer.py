"""Chunked DXLink streamer wrapper.

Wraps the tastytrade DXLinkStreamer to automatically chunk subscription
payloads that would exceed the 64k WebSocket message limit.

The DXLink protocol enforces a hard 64k limit on incoming messages.
The SDK sends all symbols in a single FEED_SUBSCRIPTION message,
which can exceed this limit when subscribing to 200+ symbols.

This wrapper splits large symbol lists into multiple smaller messages,
each staying under the configured max_payload_bytes limit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Iterable, TypeVar, cast

from tastytrade.dxfeed import Event
from tastytrade.streamer import DXLinkStreamer

logger = logging.getLogger(__name__)

U = TypeVar("U", bound=Event)

# JSON overhead estimates for payload size heuristic
MESSAGE_OVERHEAD_BYTES = 100  # {"type": "FEED_SUBSCRIPTION", "channel": N, "add": []}
SYMBOL_OVERHEAD_BYTES = 50  # {"symbol": "...", "type": "..."} JSON wrapping


class ChunkedDXLinkStreamer:
    """Wraps DXLinkStreamer to chunk subscription payloads under max_bytes.

    All subscription messages are size-estimated before sending. If the
    payload would exceed max_payload_bytes, the symbol list is split into
    multiple chunks, each sent as a separate FEED_SUBSCRIPTION message.

    Pass-through methods (listen, get_event, get_event_nowait) delegate
    directly to the underlying streamer.
    """

    def __init__(
        self,
        streamer: DXLinkStreamer,
        max_payload_bytes: int = 60_000,
        chunk_delay_sec: float = 0.1,
        enable_chunking: bool = True,
    ) -> None:
        self._streamer = streamer
        self._max_bytes = max_payload_bytes
        self._chunk_delay = chunk_delay_sec
        self._enable_chunking = enable_chunking

    def _estimate_payload_size(self, event_class: type[Event], symbols: list[str]) -> int:
        """Estimate JSON payload size for a FEED_SUBSCRIPTION message."""
        return len(
            json.dumps(
                {
                    "type": "FEED_SUBSCRIPTION",
                    "channel": 1,  # varies by event type, but close enough
                    "add": [
                        {"symbol": s, "type": event_class.__name__} for s in symbols
                    ],
                }
            )
        )

    def _chunk_symbols(
        self, event_class: type[Event], symbols: list[str]
    ) -> list[list[str]]:
        """Split symbols into chunks that fit under max_bytes.

        Uses a running size estimate with per-symbol overhead to avoid
        exceeding the payload limit.
        """
        chunks: list[list[str]] = []
        current: list[str] = []
        size = MESSAGE_OVERHEAD_BYTES

        for sym in symbols:
            sym_size = len(sym) + SYMBOL_OVERHEAD_BYTES
            if (
                self._enable_chunking
                and size + sym_size > self._max_bytes
                and current
            ):
                chunks.append(current)
                current = [sym]
                size = MESSAGE_OVERHEAD_BYTES + sym_size
            else:
                current.append(sym)
                size += sym_size

        if current:
            chunks.append(current)
        return chunks

    async def subscribe(
        self,
        event_class: type[Event],
        symbols: Iterable[str],
        refresh_interval: float = 0.1,
    ) -> None:
        """Subscribe to events, chunking if payload would exceed max_bytes."""
        symbol_list = list(symbols)
        if not symbol_list:
            return

        chunks = self._chunk_symbols(event_class, symbol_list)

        if len(chunks) > 1:
            logger.info(
                "Chunking %s subscription: %d symbols -> %d chunks (max_bytes=%d)",
                event_class.__name__,
                len(symbol_list),
                len(chunks),
                self._max_bytes,
            )

        for i, chunk in enumerate(chunks):
            # Verify estimate against actual JSON size
            actual_size = self._estimate_payload_size(event_class, chunk)
            if actual_size > self._max_bytes:
                logger.warning(
                    "Chunk %d/%d for %s still exceeds limit: %d > %d bytes",
                    i + 1,
                    len(chunks),
                    event_class.__name__,
                    actual_size,
                    self._max_bytes,
                )

            await self._streamer.subscribe(event_class, chunk, refresh_interval)

            # Delay between chunks to avoid rate limiting
            if i < len(chunks) - 1:
                await asyncio.sleep(self._chunk_delay)

    async def unsubscribe(
        self,
        event_class: type[Event],
        symbols: Iterable[str],
    ) -> None:
        """Unsubscribe from events, chunking if payload would exceed max_bytes."""
        symbol_list = list(symbols)
        if not symbol_list:
            return

        chunks = self._chunk_symbols(event_class, symbol_list)

        if len(chunks) > 1:
            logger.info(
                "Chunking %s unsubscription: %d symbols -> %d chunks",
                event_class.__name__,
                len(symbol_list),
                len(chunks),
            )

        for i, chunk in enumerate(chunks):
            await self._streamer.unsubscribe(event_class, chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(self._chunk_delay)

    # Pass-through methods

    async def listen(self, event_class: type[U]) -> AsyncIterator[U]:
        """Listen for events of the given type."""
        async for event in self._streamer.listen(event_class):
            yield event

    def get_event_nowait(self, event_class: type[U]) -> U | None:
        """Get next event without waiting."""
        return self._streamer.get_event_nowait(event_class)

    async def get_event(self, event_class: type[U]) -> U:
        """Get next event, waiting if necessary."""
        return await self._streamer.get_event(event_class)

    async def __aenter__(self) -> ChunkedDXLinkStreamer:
        """Enter context manager."""
        await self._streamer.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit context manager."""
        await self._streamer.__aexit__(*args)