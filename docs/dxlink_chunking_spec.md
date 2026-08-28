# DXLink Payload Chunking — Implementation Spec

## Goal

Ensure all DXLink `FEED_SUBSCRIPTION` messages stay under 60k bytes by chunking symbol lists when needed.

## Requirements

| ID | Requirement | Acceptance Criteria |
|----|-------------|---------------------|
| R1 | Chunk `subscribe()` when payload > 60k | No 1009 disconnect with 200+ symbols |
| R2 | Chunk `unsubscribe()` when payload > 60k | Symmetrical behavior |
| R3 | Pass-through `listen()`, `get_event()`, `get_event_nowait()` | No behavior change |
| R4 | Configurable max payload + chunk delay | YAML config works |
| R5 | Log chunk operations | `dxlink_scanner.chunk` logger shows count + size |
| R6 | Backward compatible when chunking disabled | Toggle in config |
| R7 | No SDK patching | Wrapper only; external dep unchanged |

## API

### ChunkedDXLinkStreamer

```python
class ChunkedDXLinkStreamer:
    def __init__(
        self,
        streamer: DXLinkStreamer,
        max_payload_bytes: int = 60_000,
        chunk_delay_sec: float = 0.1,
        enable_chunking: bool = True,
    ) -> None: ...

    async def subscribe(
        self,
        event_class: type[Event],
        symbols: Iterable[str],
        refresh_interval: float = 0.1,
    ) -> None: ...

    async def unsubscribe(
        self,
        event_class: type[Event],
        symbols: Iterable[str],
    ) -> None: ...

    async def listen(self, event_class: type[U]) -> AsyncIterator[U]: ...
    def get_event_nowait(self, event_class: type[U]) -> U | None: ...
    async def get_event(self, event_class: type[U]) -> U: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *args) -> None: ...
```

### Config Schema

```python
class DXLinkConfig(BaseModel):
    max_payload_bytes: int = Field(default=60_000, ge=10_000, le=64_000)
    chunk_delay_sec: float = Field(default=0.1, ge=0, le=5.0)
    enable_chunking: bool = True
```

## Implementation Notes

### Size Estimation (Heuristic)

```python
SYMBOL_OVERHEAD_BYTES = 50  # {"symbol": "...", "type": "..."} JSON wrapping
MESSAGE_OVERHEAD_BYTES = 100  # {"type": "FEED_SUBSCRIPTION", "channel": N, "add": []}

def estimate_payload_size(symbol_count: int, avg_symbol_len: int = 16) -> int:
    return MESSAGE_OVERHEAD_BYTES + symbol_count * (avg_symbol_len + SYMBOL_OVERHEAD_BYTES)
```

**Calibration**: For 100 symbols at 16 chars each: `100 + 100 * 66 = 6700 bytes` → 1 chunk
**Threshold**: ~870 symbols at 16 chars → 60k

### Chunking Algorithm

```python
def _chunk_symbols(self, symbols: list[str]) -> list[list[str]]:
    chunks = []
    current = []
    size = MESSAGE_OVERHEAD_BYTES
    for sym in symbols:
        sym_size = len(sym) + SYMBOL_OVERHEAD_BYTES
        if size + sym_size > self._max_bytes and current:
            chunks.append(current)
            current = [sym]
            size = MESSAGE_OVERHEAD_BYTES + sym_size
        else:
            current.append(sym)
            size += sym_size
    if current:
        chunks.append(current)
    return chunks
```

### Integration in cli.py

```python
# _run_scanner():
dxlink_config = config.dxlink
async with DXLinkStreamer(session) as base_streamer:
    streamer = ChunkedDXLinkStreamer(
        base_streamer,
        max_payload_bytes=dxlink_config.max_payload_bytes,
        chunk_delay_sec=dxlink_config.chunk_delay_sec,
        enable_chunking=dxlink_config.enable_chunking,
    )
    await streamer.subscribe(Quote, underlying_symbols)
    await streamer.subscribe(DXTimeAndSale, underlying_symbols + all_symbols)
    if all_symbols:
        await streamer.subscribe(DXTheoPrice, all_symbols)
```

## Sprint Plan

### Sprint 1: Core Implementation (Days 1-2)

#### Day 1: ChunkedDXLinkStreamer + Unit Tests

| Task | File | Test | Est |
|------|------|------|-----|
| Create `ChunkedDXLinkStreamer` class | `chunked_streamer.py` | — | 3h |
| Implement `_chunk_symbols()` heuristic | `chunked_streamer.py` | `test_chunk_symbols_*` | 2h |
| Implement `subscribe()` with chunking | `chunked_streamer.py` | `test_subscribe_chunked` | 2h |
| Implement `unsubscribe()` with chunking | `chunked_streamer.py` | `test_unsubscribe_chunked` | 1h |
| Pass-through methods (`listen`, `get_event`, `__aenter__`, `__aexit__`) | `chunked_streamer.py` | `test_passthrough` | 1h |
| Edge cases: empty list, single symbol, boundary | `chunked_streamer.py` | `test_edge_cases` | 1h |

#### Day 2: Config + Integration in cli.py

| Task | File | Test | Est |
|------|------|------|-----|
| Add `DXLinkConfig` to config | `config/__init__.py` | `test_dxlink_config_defaults` | 1h |
| Replace `DXLinkStreamer` with chunked wrapper in `cli.py` | `cli.py` | — | 2h |
| Update `dynamic_strikes.py` to accept chunked streamer | `dynamic_strikes.py` | — | 1h |
| Update `debug_dxfeed.py` | `debug_dxfeed.py` | — | 1h |
| Integration test: mock base streamer, verify chunked calls | `test_chunked_streamer.py` | `test_integration_mock_sdk` | 3h |
| Full test suite | `pytest` | 285+ tests pass | 1h |

**Sprint 1 Exit**: All tests pass; scanner starts with chunked streamer.

---

### Sprint 2: Logging + Verification (Day 3)

#### Day 3: Observability + Dry-Run

| Task | File | Test | Est |
|------|------|------|-----|
| Add `dxlink_scanner.chunk` logger | `chunked_streamer.py` | Manual verify | 2h |
| Log: chunk count, symbol count, payload size per subscribe | `chunked_streamer.py` | — | incl |
| Dry-run test: 200 synthetic symbols, verify 0 chunks or N chunks correct | `test_chunked_streamer.py` | `test_dry_run_200_symbols` | 2h |
| Verify no 1009 disconnect in manual test | Manual | — | 1h |
| Update `README.md` with chunking docs | `README.md` | — | 1h |
| Update `docs/configuration.md` | `docs/configuration.md` | — | 1h |
| Final full test suite | `pytest` | 285+ tests pass | 1h |

**Sprint 2 Exit**: Full test suite passes; logging visible; docs updated.

---

## Test Plan

### Unit Tests (`tests/test_chunked_streamer.py`)

| Test | Description | Input | Expected |
|------|-------------|-------|----------|
| `test_chunk_small_batch` | 10 symbols fit in 1 chunk | 10 symbols | 1 chunk |
| `test_chunk_large_batch` | 1000 symbols need multiple chunks | 1000 symbols | N chunks |
| `test_chunk_exact_boundary` | Symbols exactly at 60k boundary | calc for boundary | 2 chunks |
| `test_chunk_empty` | Empty symbol list | [] | 0 chunks, no-op |
| `test_chunk_single_large` | Single symbol > 60k (impossible but guard) | huge string | 1 chunk (can't exceed) |
| `test_subscribe_single_chunk` | subscribe with <60k payload | 50 symbols | 1 SDK call |
| `test_subscribe_multi_chunk` | subscribe with >60k payload | 1000 symbols | N SDK calls, 100ms delay |
| `test_subscribe_disabled` | chunking disabled | any | 1 SDK call, no chunking |
| `test_unsubscribe_chunked` | unsubscribe with >60k payload | 1000 symbols | N SDK calls |
| `test_passthrough_listen` | listen() delegates | — | yields from base streamer |
| `test_passthrough_get_event` | get_event() delegates | — | returns from base streamer |
| `test_estimate_payload_size` | heuristic accuracy | various | within 5% of actual |
| `test_integration_mock_sdk` | mock DXLinkStreamer, verify calls | 500 symbols | N subscribe calls logged |

### Manual Verification

1. Start scanner with 200+ symbols (SPY + SPX + ES 0DTE)
2. Verify no `1009` disconnect in logs
3. Verify `dxlink_scanner.chunk` logs show chunk count
4. Verify events flow normally (Quote, TAS, TheoPrice)
5. Verify alerts trigger normally

---

## File Changes

| File | Type | Lines Changed (est) |
|------|------|---------------------|
| `src/dxlink_scanner/chunked_streamer.py` | NEW | ~150 |
| `src/dxlink_scanner/config/__init__.py` | MODIFIED | +15 |
| `src/dxlink_scanner/cli.py` | MODIFIED | +10 |
| `src/dxlink_scanner/dynamic_strikes.py` | MODIFIED | +3 |
| `src/dxlink_scanner/debug_dxfeed.py` | MODIFIED | +3 |
| `tests/test_chunked_streamer.py` | NEW | ~200 |
| `production.yaml` | MODIFIED | +5 |
| `README.md` | MODIFIED | +10 |
| `docs/configuration.md` | MODIFIED | +15 |

---

## Definition of Done

- [ ] `ChunkedDXLinkStreamer` passes all unit tests
- [ ] Full test suite passes (285+ tests)
- [ ] No 1009 disconnect with 200+ symbols
- [ ] Logging shows chunk operations
- [ ] Config toggle works (enable/disable chunking)
- [ ] Documentation updated
- [ ] Mypy clean
- [ ] Ruff clean