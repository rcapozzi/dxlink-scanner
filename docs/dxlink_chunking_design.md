# DXLink Payload Chunking Design

## Problem

The DXLink WebSocket protocol enforces a **64k message limit**; subscription payloads must not exceed **60k bytes** (with 4k headroom). The `tastytrade.DXLinkStreamer` sends all symbols in a single `FEED_SUBSCRIPTION` message:

```python
message = {
    "type": "FEED_SUBSCRIPTION",
    "channel": self._channels[cls_str],
    "add": [
        {"symbol": symbol, "type": cls_str}
        for symbol in intuitive_iterable(symbols)
    ],
}
await self._websocket.send_json(message)
```

With ~100+ option symbols per underlying and 3 event types (Quote, TimeAndSale, TheoPrice), the payload can exceed 60k. The SDK already detects this:

```python
except* WebSocketDisconnect as eg:
    if eg.subgroup(lambda e: getattr(e, "code", None) == 1009):
        raise TastytradeError(
            "Subscription message too long! Try reducing the number of symbols."
        )
```

## Current Usage Audit

| Location | Symbol Count | At Risk? |
|----------|-------------|----------|
| `cli.py:619` — `streamer.subscribe(Quote, underlying_symbols)` | 3-4 | No |
| `cli.py:620` — `streamer.subscribe(DXTimeAndSale, underlying_symbols + all_symbols)` | ~100-200 | **YES** |
| `cli.py:622` — `streamer.subscribe(DXTheoPrice, all_symbols)` | ~80-120 | **YES** |
| `cli.py:693` — `streamer.subscribe(DXTimeAndSale, delta.added)` | Varies | Possible |
| `dynamic_strikes.py:44` — `streamer.subscribe(TimeAndSale, delta.added)` | Varies | Possible |

## Design

### Approach: Wrapper with Transparent Chunking

Create a `ChunkedDXLinkStreamer` wrapper that intercepts `subscribe`/`unsubscribe` calls, estimates payload size, and splits into chunks when needed.

**Why a wrapper (not patching SDK):**
- Don't fork `tastytrade` — updates would conflict
- Transparent to existing code
- Can be toggled on/off via config

### Architecture

```
┌─────────────────────┐
│  Scanner (cli.py)   │
└─────────┬───────────┘
          │ subscribe(event_class, symbols)
          ▼
┌─────────────────────────────┐
│  ChunkedDXLinkStreamer      │
│  ┌─────────────────────────┐│
│  │ estimate_payload_size() ││
│  │ chunk_symbols()         ││
│  │ send_chunked()          ││
│  └─────────────────────────┘│
└─────────┬───────────────────┘
          │ (multiple smaller messages)
          ▼
┌─────────────────────┐
│  DXLinkStreamer (SDK)│
└─────────┬───────────┘
          │ WebSocket
          ▼
      DXLink Server
```

### Key Classes

#### `ChunkedDXLinkStreamer`

```python
class ChunkedDXLinkStreamer:
    """Wraps DXLinkStreamer to chunk subscription payloads under 60k bytes."""
    
    MAX_PAYLOAD_BYTES = 60_000
    SAFETY_MARGIN = 4_000  # headroom for JSON overhead
    
    def __init__(self, streamer: DXLinkStreamer, max_bytes: int = MAX_PAYLOAD_BYTES):
        self._streamer = streamer
        self._max_bytes = max_bytes
    
    async def subscribe(
        self,
        event_class: type[Event],
        symbols: Iterable[str],
        refresh_interval: float = 0.1,
    ) -> None:
        """Subscribe with automatic chunking."""
        symbol_list = list(symbols)
        chunks = self._chunk_symbols(event_class, symbol_list)
        for chunk in chunks:
            await self._streamer.subscribe(event_class, chunk, refresh_interval)
            # Small delay between chunks to avoid rate limiting
            await asyncio.sleep(0.1)
    
    def _chunk_symbols(
        self, event_class: type[Event], symbols: list[str]
    ) -> list[list[str]]:
        """Split symbols into chunks that fit under max_bytes."""
        chunks = []
        current_chunk = []
        current_size = self._estimate_overhead(event_class)
        
        for symbol in symbols:
            symbol_size = len(symbol) + 50  # {"symbol": "...", "type": "..."}
            if current_size + symbol_size > self._max_bytes - self.SAFETY_MARGIN:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = [symbol]
                current_size = self._estimate_overhead(event_class) + symbol_size
            else:
                current_chunk.append(symbol)
                current_size += symbol_size
        
        if current_chunk:
            chunks.append(current_chunk)
        return chunks
    
    def _estimate_overhead(self, event_class: type[Event]) -> int:
        """Estimate fixed JSON overhead for FEED_SUBSCRIPTION message."""
        return len(json.dumps({
            "type": "FEED_SUBSCRIPTION",
            "channel": 1,  # varies by event type
            "add": [],     # empty placeholder
        }))
    
    def _estimate_payload_size(self, event_class: type[Event], symbols: list[str]) -> int:
        """Estimate total payload size for a subscription message."""
        return len(json.dumps({
            "type": "FEED_SUBSCRIPTION",
            "channel": 1,
            "add": [{"symbol": s, "type": event_class.__name__} for s in symbols],
        }))
```

#### Size Estimation Strategy

**Two options:**

| Method | Accuracy | Cost | Recommendation |
|--------|----------|------|----------------|
| **Dry-run JSON encode** | Exact | O(n) per symbol | Use for small batches (<500) |
| **Heuristic** (symbol_len + fixed overhead) | ~95% | O(1) per symbol | Use for large batches |

The heuristic is preferred:
- Each symbol adds `len(symbol) + ~50` bytes (JSON wrapping)
- Fixed overhead: ~100 bytes (type, channel, add key)
- Total ≈ `100 + sum(len(s) + 50 for s in symbols)`

**Average symbol length**: `.SPY260731C500` = ~16 chars → ~66 bytes per symbol
**Chunk size**: `(60000 - 10000 - 100) / 66` ≈ **750 symbols per chunk**

With 100-200 option symbols, most batches fit in one chunk. But TheoPrice across all underlyings can hit 200+.

### Integration Point

Replace direct `DXLinkStreamer` usage in `cli.py`:

```python
# Before:
async with DXLinkStreamer(session) as streamer:
    await streamer.subscribe(Quote, underlying_symbols)
    await streamer.subscribe(DXTimeAndSale, underlying_symbols + all_symbols)

# After:
async with DXLinkStreamer(session) as base_streamer:
    streamer = ChunkedDXLinkStreamer(base_streamer)
    await streamer.subscribe(Quote, underlying_symbols)
    await streamer.subscribe(DXTimeAndSale, underlying_symbols + all_symbols)
```

### Configuration

```yaml
# production.yaml
dxlink:
  max_payload_bytes: 60000  # < 64k limit
  chunk_delay_sec: 0.1      # delay between chunks
  enable_chunking: true     # toggle for debugging
```

### Edge Cases

| Scenario | Handling |
|----------|----------|
| Single symbol > 60k | Impossible (symbol < 100 chars) |
| Channel not opened yet | ChunkedStreamer delegates to SDK `_channel_request` first |
| Unsubscribe | Same chunking logic applies |
| Race condition between chunks | 100ms delay between chunks; SDK serializes on WebSocket |
| Dynamic strike rescan | `delta.added` typically < 10 symbols, but chunk if needed |

### Testing Strategy

| Test | Description |
|------|-------------|
| `test_chunk_small_batch` | 10 symbols → 1 chunk |
| `test_chunk_large_batch` | 1000 symbols → multiple chunks |
| `test_chunk_boundary` | Exact 60k boundary → split correctly |
| `test_empty_symbols` | Empty list → no-op |
| `test_payload_size_estimation` | Verify heuristic within 5% of actual |
| `test_integration_with_sdk` | Mock DXLinkStreamer, verify chunked calls |

---

## Sprint Plan

### Sprint 1: Core Chunking Logic (2 days)

**Goal**: Implement `ChunkedDXLinkStreamer` with unit tests.

| Task | Deliverable | Owner | Time |
|------|-------------|-------|------|
| 1.1 | Create `src/dxlink_scanner/chunked_streamer.py` with core class | TDD | 4h |
| 1.2 | Implement `_chunk_symbols()` with heuristic sizing | TDD | 2h |
| 1.3 | Implement `subscribe()` with chunking | TDD | 2h |
| 1.4 | Implement `unsubscribe()` with chunking | TDD | 2h |
| 1.5 | Add `listen()` and `get_event()` pass-through | TDD | 1h |
| 1.6 | Unit tests: chunking logic | pytest | 3h |
| 1.7 | Unit tests: edge cases (empty, boundary, large) | pytest | 2h |

**Exit criteria**: All unit tests pass; chunking correctly splits 1000 symbols into N chunks.

---

### Sprint 2: Integration & Config (2 days)

**Goal**: Wire into scanner CLI; add config options.

| Task | Deliverable | Owner | Time |
|------|-------------|-------|------|
| 2.1 | Add `dxlink` section to `DetectionConfig` (or `ScannerConfig`) | config | 1h |
| 2.2 | Replace `DXLinkStreamer` with `ChunkedDXLinkStreamer` in `cli.py` | integration | 3h |
| 2.3 | Wire config values (max_payload_bytes, chunk_delay) | config | 2h |
| 2.4 | Update `dynamic_strikes.py` to use chunked streamer | integration | 1h |
| 2.5 | Update `debug_dxfeed.py` to use chunked streamer | integration | 1h |
| 2.6 | Integration test: mock SDK, verify chunked subscribe calls | pytest | 3h |

**Exit criteria**: Scanner starts, subscribes to all symbols, chunks transparently.

---

### Sprint 3: Verification & Monitoring (1 day)

**Goal**: Verify in production-like conditions; add monitoring.

| Task | Deliverable | Owner | Time |
|------|-------------|-------|------|
| 3.1 | Add `dxlink_scanner.chunk` logger: log chunk count + size per subscribe | logging | 2h |
| 3.2 | Add Prometheus-style metric: `dxlink_subscription_chunks_total` | metrics | 2h |
| 3.3 | Dry-run test: 200 synthetic symbols, verify no 1009 disconnect | test | 2h |
| 3.4 | Update `README.md` with chunking config docs | docs | 1h |
| 3.5 | Update `docs/configuration.md` | docs | 1h |

**Exit criteria**: Full test suite passes; chunking logs visible; no 1009 errors.

---

## File Manifest

```
src/dxlink_scanner/
├── chunked_streamer.py       # NEW: ChunkedDXLinkStreamer
├── cli.py                    # MODIFIED: use ChunkedDXLinkStreamer
├── config/
│   └── __init__.py           # MODIFIED: add dxlink config fields
├── debug_dxfeed.py           # MODIFIED: use chunked streamer
└── dynamic_strikes.py        # MODIFIED: use chunked streamer

tests/
├── test_chunked_streamer.py  # NEW: unit + integration tests
└── conftest.py               # MODIFIED: fixtures for mock streamer

docs/
├── configuration.md          # MODIFIED: add dxlink config section
└── filtering.md              # NEW: document design decisions

production.yaml               # MODIFIED: add dxlink config
```

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| SDK update breaks wrapper | High | Pin SDK version; integration tests catch drift |
| Heuristic underestimates payload | Medium | Use dry-run encode for safety; log warning if >90% |
| Chunk delay adds latency | Low | 100ms × 2 chunks = 200ms; negligible vs 60s TheoPrice |
| Rate limiting by DXLink | Low | Not documented; 100ms delay conservative |
| Race: chunk N+1 before chunk N ack | Low | SDK serializes; await each subscribe |

---

## Decision Record

**Why not patch `tastytrade` SDK directly?**
- External dependency; patches lost on update
- Wrapper is transparent; zero changes to SDK behavior when chunking not needed

**Why heuristic vs exact size calculation?**
- Exact: O(n) JSON encode per symbol for estimation
- Heuristic: O(1) per symbol; within 5% accuracy
- Tradeoff: 1000 symbols × 16 bytes = 16KB estimation cost vs negligible

**Why 60k not 64k?**
- 4k headroom for JSON escaping, SDK-internal headers, WebSocket frame overhead
- SDK already uses 1009 close code at 64k; stay well below