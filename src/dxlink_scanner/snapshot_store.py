"""In-memory snapshot store for consolidated DXLink events.

Provides real-time consolidated state per symbol with background
parquet persistence. Replaces two parallel consumer tasks with
a single unified consumer pattern.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from dxlink_scanner.config import StreamConfig
from dxlink_scanner.models import (
    ConsolidatedEvent,
    ConsolidatedSnapshot,
    merge_into_snapshot,
)
from dxlink_scanner.schemas.v1 import schema_v1

logger = logging.getLogger(__name__)


class SnapshotStore:
    """In-memory consolidated snapshot store with parquet persistence.

    Attributes:
        config: Stream config with backpressure/persistence settings.
        persist: Whether to write events to parquet.
        _snapshots: dict[str, ConsolidatedSnapshot] - latest state per symbol.
        _buffer: list[ConsolidatedEvent] - batch buffer for parquet flush.
        _event_counter: Monotonic event counter.
        _evict_map: dict[str, int] - symbol -> evict_at (epoch ms).
        _underlying_map: dict[str, str] - symbol -> underlying_symbol.
    """

    def __init__(self, config: StreamConfig, persist: bool = True) -> None:
        self.config = config
        self.persist = persist
        self._snapshots: dict[str, ConsolidatedSnapshot] = {}
        self._buffer: list[ConsolidatedEvent] = []
        self._event_counter = 0
        self._evict_map: dict[str, int] = {}
        self._underlying_map: dict[str, str] = {}
        self._flush_task: asyncio.Task[None] | None = None
        self._session_id = uuid.uuid4()
        self._output_dir: Path | None = None

    def set_evict_map(self, evicts: dict[str, int]) -> None:
        """Set the eviction map (symbol -> epoch ms)."""
        self._evict_map = evicts

    def set_underlying_map(self, mapping: dict[str, str]) -> None:
        """Set the symbol -> underlying_symbol cache."""
        self._underlying_map = mapping

    def bootstrap_snapshot(self, symbol: str, underlying_symbol: str) -> ConsolidatedSnapshot:
        """Pre-create a ConsolidatedSnapshot for a symbol during bootstrap.

        This ensures a snapshot exists for the underlying streamer-root-symbol
        (e.g. /ES:XCME for futures) so that when Quote events arrive
        and merge into it, the snapshot already has the correct
        underlying_symbol set. The engine can then look up the mid_price
        via store.get(snap.underlying_symbol).mid_price.

        Args:
            symbol: The streamer symbol (e.g. /ES:XCME).
            underlying_symbol: The underlying symbol to set on the snapshot.

        Returns:
            The created or existing ConsolidatedSnapshot.
        """
        snap = self._snapshots.get(symbol)
        if snap is None:
            snap = ConsolidatedSnapshot(
                symbol=symbol,
                underlying_symbol=underlying_symbol,
                updated_at=dt.datetime.now(dt.UTC),
            )
            self._snapshots[symbol] = snap
        return snap

    def get(self, symbol: str) -> ConsolidatedSnapshot | None:
        """Get the latest snapshot for a symbol."""
        return self._snapshots.get(symbol)

    def ingest(self, event: ConsolidatedEvent) -> None:
        """Ingest an event: update snapshot + buffer for parquet."""
        self._event_counter += 1
        snap = self._snapshots.get(event.symbol)
        if snap is None:
            underlying = self._underlying_map.get(event.symbol, event.symbol)
            snap = ConsolidatedSnapshot(
                symbol=event.symbol,
                underlying_symbol=underlying,
                updated_at=dt.datetime.now(dt.UTC),
            )
        merge_into_snapshot(snap, event)
        self._snapshots[event.symbol] = snap

        if self.persist:
            self._buffer.append(event)

    async def start_flush_loop(self, output_dir: Path) -> None:
        """Start background task to flush buffered events to parquet."""
        self._output_dir = output_dir
        self._flush_task = asyncio.create_task(self._flush_loop(output_dir))

    async def stop_flush_loop(self) -> None:
        """Stop the background flush task."""
        if self._flush_task:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None

    async def _flush_loop(self, output_dir: Path) -> None:
        """Periodically flush buffered events to parquet."""
        while True:
            await asyncio.sleep(self.config.flush_interval_sec)
            if self._buffer:
                await self._flush_to_parquet(output_dir)

    async def _flush_to_parquet(self, output_dir: Path) -> None:
        """Flush buffered events to a parquet file."""
        if not self._buffer:
            return

        batch = self._buffer[:]
        self._buffer.clear()

        try:
            # Convert events to dicts aligned with schema_v1
            rows = []
            for ev in batch:
                d: dict[str, object] = {}
                for field in schema_v1.names:
                    v = getattr(ev, field, None)
                    if isinstance(v, Decimal):
                        d[field] = str(v)
                    else:
                        d[field] = v
                rows.append(d)

            table = pa.Table.from_pylist(rows, schema=schema_v1)

            today = dt.date.today()
            out_dir = output_dir / str(today)
            out_dir.mkdir(parents=True, exist_ok=True)
            fname = f"events_v1_{self._session_id}_{int(dt.datetime.now().timestamp())}.parquet"
            out_path = out_dir / fname
            pq.write_table(table, str(out_path))

            # Write session metadata
            meta_path = out_dir / "session_meta.json"
            meta = {
                "session_id": str(self._session_id),
                "schema_version": "v1",
                "event_count": len(batch),
                "created_at": dt.datetime.now(dt.UTC).isoformat(),
            }
            if meta_path.exists():
                existing = json.loads(meta_path.read_text())
                existing["event_count"] = existing.get("event_count", 0) + len(batch)
                existing["files"] = existing.get("files", [])
                existing["files"].append(fname)
                meta = existing
            meta_path.write_text(json.dumps(meta, indent=2))

            logger.info("Flushed %d events to %s", len(batch), out_path)
        except Exception as e:
            logger.error("Failed to flush parquet: %s", e)
            self._buffer.extend(batch)
            raise

    async def flush_remaining(self, output_dir: Path) -> None:
        """Flush any remaining buffered events. Call on shutdown."""
        if self._buffer:
            await self._flush_to_parquet(output_dir)

    @property
    def snapshot_count(self) -> int:
        return len(self._snapshots)

    @property
    def buffer_count(self) -> int:
        return len(self._buffer)
