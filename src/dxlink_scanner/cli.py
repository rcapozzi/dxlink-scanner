"""Typer CLI entry point for the options volume scanner."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from tastytrade.dxfeed import Quote
from tastytrade.dxfeed import TimeAndSale as DXTimeAndSale
from tastytrade.streamer import DXLinkStreamer

from dxlink_scanner.auth import TastyTradeAuth
from dxlink_scanner.bootstrap import ChainLoader, InstrumentResolver
from dxlink_scanner.config import CelAlertRule, ScannerConfig, load_config
from dxlink_scanner.dynamic_strikes import DynamicStrikeManager
from dxlink_scanner.models import (
    ConsolidatedEvent,
    TimeAndSaleEvent,
    normalize_quote,
    normalize_timeandsale,
)
from dxlink_scanner.rules import CELRuleEngine
from dxlink_scanner.sinks import StdoutSink, WebhookSink
from dxlink_scanner.snapshot_store import SnapshotStore
from dxlink_scanner.stats import RollingStatsManager

SinkType = StdoutSink | WebhookSink
RuleEngine = CELRuleEngine

app = typer.Typer(help="Real-time options volume scanner using Tastytrade DXLink")

logger = logging.getLogger(__name__)


def _timeandsale_to_event(tas: DXTimeAndSale) -> TimeAndSaleEvent:
    """Adapter: convert SDK TimeAndSale model to scanner TimeAndSaleEvent."""
    event_time = tas.event_time
    if isinstance(event_time, str):
        timestamp = dt.datetime.fromisoformat(event_time)
    elif isinstance(event_time, (int, float)):
        timestamp = dt.datetime.fromtimestamp(event_time / 1000, tz=dt.UTC)
    else:
        timestamp = event_time or dt.datetime.now(dt.UTC)

    return TimeAndSaleEvent(
        symbol=tas.event_symbol,
        price=Decimal(str(tas.price)) if tas.price else Decimal("0"),
        size=int(tas.size) if tas.size else 0,
        timestamp=timestamp,
        event_type="TimeAndSale",
        bid_price=Decimal(str(tas.bid_price)) if tas.bid_price else None,
        ask_price=Decimal(str(tas.ask_price)) if tas.ask_price else None,
        trade_type=tas.type if tas.type else None,
    )


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Suppress DEBUG messages from the tastytrade library by default
    logging.getLogger("tastytrade").setLevel(logging.ERROR)


async def _produce_events(
    streamer: DXLinkStreamer,
    event_type: type,
    normalize_fn: Callable[..., ConsolidatedEvent],
    queue: asyncio.Queue[ConsolidatedEvent],
    counter: list[int],
) -> None:
    """Producer: listen to one DXLink event type and push normalized events to queue."""
    try:
        async for msg in streamer.listen(event_type):  # type: ignore[var-annotated]
            counter[0] += 1
            event: ConsolidatedEvent = normalize_fn(msg, counter[0])
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Queue full, dropping %s event for %s", event.source_type, event.symbol)
    except Exception as e:
        logger.error("Producer for %s exited: %s", event_type.__name__, e)
        raise


async def _consume_consolidated(
    queue: asyncio.Queue[ConsolidatedEvent],
    store: SnapshotStore,
    rules: RuleEngine,
    sinks: list[SinkType],
) -> None:
    """Consumer: process events from the unified queue."""
    while True:
        event = await queue.get()
        store.ingest(event)

        # Only TAS events go to rule engine + sinks
        if event.source_type == "TIME_AND_SALE":
            tas_event = TimeAndSaleEvent(
                symbol=event.symbol,
                price=event.last_trade_price or Decimal("0"),
                size=event.last_trade_size or 0,
                timestamp=(
                    dt.datetime.fromtimestamp(event.last_trade_time / 1000, tz=dt.UTC)
                    if event.last_trade_time
                    else dt.datetime.now(dt.UTC)
                ),
                event_type="TimeAndSale",
                bid_price=event.bid_price,
                ask_price=event.ask_price,
                trade_type=event.last_trade_type,
            )
            alert = rules.process(tas_event)
            if alert:
                for sink in sinks:
                    await sink.send(alert)
        queue.task_done()


def _get_normalize_fn(event_type: type) -> Callable[..., ConsolidatedEvent]:
    """Return the appropriate normalize function for a DXLink event type."""
    if event_type is Quote:
        return normalize_quote
    if event_type is DXTimeAndSale:
        return normalize_timeandsale
    raise ValueError(f"Unknown event type: {event_type}")


async def _on_event(
    event: TimeAndSaleEvent,
    rules: RuleEngine,
    sinks: list[SinkType],
) -> None:
    """Callback for processing TAS events."""
    alert = rules.process(event)
    if alert:
        for sink in sinks:
            await sink.send(alert)


@app.command()
def main(
    config_path: Annotated[Path, typer.Option(
        "--config", "-c", help="Path to config file"
    )] = Path("dxlink-scanner.yaml"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging")] = False,
    debug_messages: Annotated[
        bool,
        typer.Option(
            "--debug-messages",
            "--dm",
            help="Log every raw DXLink WebSocket message",
        ),
    ] = False,
) -> None:
    """Run the real-time options volume scanner."""
    setup_logging(verbose)
    # Enable tastytrade SDK debug logging only when --debug-messages is passed
    if debug_messages:
        logging.getLogger("tastytrade.streamer").setLevel(logging.DEBUG)
    load_dotenv()  # Load .env before config (env vars are used for interpolation)
    config = load_config(config_path)
    auth = TastyTradeAuth(
        client_id=config.tastytrade.client_id,
        client_secret=config.tastytrade.client_secret,
        refresh_token=config.tastytrade.refresh_token,
        sandbox=config.tastytrade.sandbox,
    )

    import signal

    shutdown_event = asyncio.Event()

    def _handle_sigterm(signum: int, frame: object) -> None:
        logger.info("Received signal %d — initiating graceful shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    try:
        asyncio.run(_run_scanner(auth, config, debug_messages, shutdown_event))
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down.")


async def _run_scanner(
    auth: TastyTradeAuth,
    config: ScannerConfig,
    debug_messages: bool = False,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Main async scanner loop.

    Fetches option chains for all configured tickers (each with per-ticker
    strike count and expiration filter), collects streamer symbols,
    subscribes them all to a single DXLink connection, and evaluates
    incoming TimeAndSale events for volume anomalies.

    Uses a unified event consumer pattern: two producer tasks (one per
    DXLink message type) feed into a bounded asyncio.Queue. A single
    consumer normalizes events, updates the SnapshotStore, and routes
    TimeAndSale events to the alert rule engine.
    """
    session = auth.get_session()
    await session.refresh(force=True)
    logger.info("Authenticated with Tastytrade")

    # Bootstrap: fetch option chains for each ticker
    loader = ChainLoader(session=session)
    all_symbols: list[str] = []
    underlying_symbols: list[str] = []
    underlying_symbols_set: set[str] = set()
    underlying_map: dict[str, str] = {}

    for ticker in config.watchlist.tickers:
        chains = await loader.get_nested_chain(ticker.symbol, ticker.option_type)
        underlying_info, strikes = loader.parse_chain(
            chains,
            ticker.expiration_filter,
            is_future=ticker.option_type == "futures",
        )
        # For futures, use the underlying_symbol from the option chain (e.g. "ESU6")
        # rather than the root symbol (e.g. "ES") for market data price lookup
        if ticker.option_type == "futures" and underlying_info.symbol:
            price_symbol = underlying_info.symbol
        else:
            price_symbol = ticker.symbol
        # Fetch underlying price for ATM-based strike selection
        underlying_price = await loader.get_underlying_price(price_symbol, ticker.option_type)
        if underlying_price is None:
            logger.warning(
                "Could not resolve underlying price for %s; using first %d strikes",
                ticker.symbol,
                ticker.strikes_around_atm,
            )
            count = min(len(strikes), ticker.strikes_around_atm)
            symbols = [s.symbol for s in strikes[:count]]
        else:
            symbols_info = ChainLoader.select_atm_strikes(strikes, underlying_price, ticker.strikes_around_atm)
            symbols = [s.symbol for s in symbols_info]
            count = len(symbols_info)
        all_symbols.extend(symbols)

        # Determine the streamer symbol for Quote subscription
        if ticker.option_type == "futures" and underlying_info.symbol:
            # For futures, use the streamer-root-symbol (e.g. /ES:XCME) for
            # Quote subscription. This is the DXLink-valid symbol that
            # produces Quote events with bid/ask for the underlying.
            quote_symbol = InstrumentResolver.resolve_futures_streamer(ticker.symbol)
        elif ticker.option_type == "equity" and ticker.symbol in ("SPX", "NDX", "RUT"):
            # Index options: Quote works without exchange suffix
            quote_symbol = InstrumentResolver.resolve_index_option_streamer(ticker.symbol)
        else:
            quote_symbol = ticker.symbol

        underlying_symbols.append(quote_symbol)
        underlying_symbols_set.add(quote_symbol)

        # Build underlying_map for snapshot store and rule engine
        # Maps option symbols → quote_symbol (streamer-root-symbol, e.g. /ES:XCME)
        # so the engine can resolve underlying_symbol when looking up
        # underlying_price (derived from Quote mid_price on the underlying)
        for s in strikes:
            underlying_map[s.symbol] = quote_symbol
        # Map the quote_symbol → itself (identity) so Quote events on the
        # underlying create a snapshot with matching underlying_symbol
        underlying_map[quote_symbol] = quote_symbol

        logger.info(
            "Found %d strikes for %s (using %d, filter=%s, underlying_price=%s)",
            len(strikes),
            ticker.symbol,
            count,
            ticker.expiration_filter,
            underlying_price,
        )

    logger.info(
        "Watching %d option streamer symbols across %d tickers",
        len(all_symbols),
        len(config.watchlist.tickers),
    )

    # Setup components
    stats_mgr = RollingStatsManager(config.detection)
    # Create SnapshotStore before rules engine (rules engine needs store reference)
    store = SnapshotStore(config.stream, persist=config.outputs.persist_events)
    store.set_underlying_map(underlying_map)
    # Pre-create snapshots for all underlying streamer symbols so that
    # when Quote events arrive, the snapshot already has the correct
    # underlying_symbol set. This ensures the engine can look up
    # underlying_price via store.get(snap.underlying_symbol).mid_price
    for sym in underlying_symbols:
        store.bootstrap_snapshot(sym, underlying_map.get(sym, sym))
    # CEL-based rule engine: collect per-symbol and default rules from config
    per_symbol_rules: dict[str, list[CelAlertRule]] = {}
    for ticker in config.watchlist.tickers:
        if ticker.alert_rules:
            per_symbol_rules[ticker.symbol] = ticker.alert_rules
    default_rules = config.watchlist.default_alert_rules
    rules: RuleEngine = CELRuleEngine(
        config.detection,
        config.watchlist,
        stats_mgr,
        per_symbol_rules=per_symbol_rules,
        default_rules=default_rules,
        underlying_symbols=underlying_symbols_set,
        underlying_symbol_map=underlying_map,
        snapshot_store=store,
    )
    logger.info("Using CEL rule engine")
    sinks: list[SinkType] = []
    if config.outputs.stdout:
        sinks.append(StdoutSink())
    if config.outputs.webhook.enabled and config.outputs.webhook.url:
        sinks.append(
            WebhookSink(
                url=config.outputs.webhook.url,
                timeout=config.outputs.webhook.timeout_seconds,
                max_retries=config.outputs.webhook.max_retries,
            )
        )

    # Connect to DXLink via SDK's DXLinkStreamer
    async with DXLinkStreamer(session) as streamer:
        # Quote on underlying symbols (bid/ask for the underlying itself;
        # mid_price is used as underlying_price in alerts)
        await streamer.subscribe(Quote, underlying_symbols)
        # TimeAndSale on both the underlying AND all option symbols
        await streamer.subscribe(DXTimeAndSale, underlying_symbols + all_symbols)
        logger.info(
            "Subscribed to Quote(%s) + TimeAndSale(%d) symbols",
            ','.join(underlying_symbols),
            len(underlying_symbols) + len(all_symbols),
        )
        logger.info("Listening for volume anomalies...")

        # Unified consumer: two producers → bounded queue → single consumer
        queue: asyncio.Queue[ConsolidatedEvent] = asyncio.Queue(maxsize=config.stream.backpressure_queue_size)
        counter = [0]  # shared event counter

        producers = []
        for event_type in (Quote, DXTimeAndSale):
            normalize_fn = _get_normalize_fn(event_type)
            t = asyncio.create_task(_produce_events(streamer, event_type, normalize_fn, queue, counter))
            producers.append(t)

        consumer = asyncio.create_task(_consume_consolidated(queue, store, rules, sinks))

        # Also start parquet flush loop if persistence enabled
        data_dir = Path(config.outputs.data_dir)
        if store.persist:
            await store.start_flush_loop(data_dir)

        # Dynamic strike manager for intraday chain updates
        strike_mgr = DynamicStrikeManager(session, config.watchlist, rescan_interval_min=60)
        await strike_mgr.initial_scan()

        # Shutdown monitor: checks both signal event and daily cutoff
        async def _shutdown_monitor() -> None:
            """Wait for SIGTERM or 17:00 ET market close."""
            now = dt.datetime.now(dt.UTC)
            # 17:00 ET = 21:00 UTC (or 22:00 UTC during DST)
            # Check if we're past market close
            market_close_hour = 21 if now.month in (3, 4, 5, 6, 7, 8, 9, 10, 11, 12) else 22
            while True:
                if shutdown_event is not None and shutdown_event.is_set():
                    logger.info("Shutdown signal received")
                    return
                if dt.datetime.now(dt.UTC).hour >= market_close_hour:
                    logger.info("Market close reached (%d:00 UTC) — shutting down", market_close_hour)
                    return
                await asyncio.sleep(10)

        monitor = asyncio.create_task(_shutdown_monitor())

        # Periodic rescan task
        async def _rescan_loop() -> None:
            while True:
                await asyncio.sleep(60)
                delta = await strike_mgr.rescan()
                if delta and (delta.added or delta.removed):
                    if delta.added:
                        await streamer.subscribe(DXTimeAndSale, delta.added)
                        logger.info("Subscribed to %d new symbols", len(delta.added))
                    if delta.removed:
                        # Note: tastytrade SDK may not support unsubscribe;
                        # stale symbols simply stop receiving matching events
                        logger.info("Unsubscribed %d stale symbols", len(delta.removed))

        rescan = asyncio.create_task(_rescan_loop())

        # Periodic stats logging task
        async def _stats_logger() -> None:
            """Log throughput stats every 5 seconds."""
            last_count = 0
            while True:
                await asyncio.sleep(30)
                current_count = counter[0]
                delta = current_count - last_count
                logger.info("Stats: %d events in last 30s (total=%d, queue=%d)", delta, current_count, queue.qsize())
                last_count = current_count

        stats_task = asyncio.create_task(_stats_logger())

        # Wait for shutdown signal or any producer exit
        done, pending = await asyncio.wait([*producers, monitor], return_when=asyncio.FIRST_COMPLETED)
        # Log any exceptions
        for t in done:
            if not isinstance(t, asyncio.Task):
                continue
            exc = t.exception()
            if exc:
                logger.error("Task exited with error: %s", exc)
        # Cancel all remaining tasks
        for t in pending:
            t.cancel()
        consumer.cancel()
        rescan.cancel()
        stats_task.cancel()

        # Flush remaining events to parquet
        if store.persist:
            await store.flush_remaining(data_dir)

        logger.info("Scanner shutdown complete.")


if __name__ == "__main__":
    app()
