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
from tastytrade.dxfeed import TheoPrice as DXTheoPrice
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
    normalize_theoprice,
    normalize_timeandsale,
)
from dxlink_scanner.rules import CELRuleEngine
from dxlink_scanner.sinks import StdoutSink, WebhookSink
from dxlink_scanner.snapshot_store import SnapshotStore
from dxlink_scanner.stats import (
    BayesianGammaPoisson,
    CrossSymbolPool,
    HawkesProcess,
    ModelSet,
    ModelStore,
    RegimeDetector,
    RollingStatsManager,
    TimeOfDaySeasonality,
    prior_elicitation,
)

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
    bayesian_models: dict[str, BayesianGammaPoisson],
    hawkes_models: dict[str, HawkesProcess],
    seasonality_models: dict[str, TimeOfDaySeasonality],
    cross_symbol_pool: CrossSymbolPool | None = None,
    regime_detectors: dict[str, RegimeDetector] | None = None,
    model_store: ModelStore | None = None,
    model_sets: dict[str, ModelSet] | None = None,
) -> None:
    """Consumer: process events from the unified queue."""
    while True:
        event = await queue.get()
        store.ingest(event)

        # Periodic checkpoint if model store is configured
        if model_store and model_sets:
            model_store.maybe_checkpoint(
                _compile_model_sets(model_sets, bayesian_models, hawkes_models,
                                    seasonality_models, cross_symbol_pool,
                                    regime_detectors or {})
            )

        # Only TAS events go to rule engine + sinks
        if event.source_type == "TIME_AND_SALE":
            # Enrich TAS event with delta from the latest TheoPrice snapshot
            snap = store.get(event.symbol)
            delta = snap.delta if snap and snap.delta is not None else None

            # Determine underlying for statistical models
            underlying = snap.underlying_symbol if snap else event.symbol
            if underlying not in bayesian_models:
                underlying = "default"

            # Update statistical models with this trade
            trade_time = dt.datetime.now(dt.UTC).timestamp()
            size = event.last_trade_size or 0

            # Bayesian Gamma-Poisson (count of trades)
            bayesian_models[underlying].update(1)  # count of 1 trade
            # Also update for size (treating size as count in a separate model could work too)

            # Hawkes process (event timing)
            hawkes_models[underlying].add_event(trade_time)

            # Time-of-day seasonality
            if event.last_trade_time:
                trade_dt = dt.datetime.fromtimestamp(event.last_trade_time / 1000, tz=dt.UTC)
                seasonality_models[underlying].add_observation(trade_dt, float(size))

            # Cross-symbol pooling (share information across underlyings)
            if cross_symbol_pool is not None:
                cross_symbol_pool.update_symbol(underlying, 1, exposure=1.0)

            tas_event = TimeAndSaleEvent(
                symbol=event.symbol,
                price=event.last_trade_price or Decimal("0"),
                size=size,
                timestamp=(
                    dt.datetime.fromtimestamp(event.last_trade_time / 1000, tz=dt.UTC)
                    if event.last_trade_time
                    else dt.datetime.now(dt.UTC)
                ),
                event_type="TimeAndSale",
                bid_price=event.bid_price,
                ask_price=event.ask_price,
                trade_type=event.last_trade_type,
                delta=delta,
            )
            alert = rules.process(tas_event)
            if alert:
                for sink in sinks:
                    await sink.send(alert)
        queue.task_done()


def _compile_model_sets(
    model_sets: dict[str, ModelSet],
    bayesian_models: dict[str, BayesianGammaPoisson],
    hawkes_models: dict[str, HawkesProcess],
    seasonality_models: dict[str, TimeOfDaySeasonality],
    cross_symbol_pool: CrossSymbolPool | None = None,
    regime_detectors: dict[str, RegimeDetector] | None = None,
) -> dict[str, ModelSet]:
    """Sync the individual model dicts into ModelSet objects for checkpointing."""
    for symbol, model_set in model_sets.items():
        model_set.bayesian = bayesian_models.get(symbol, BayesianGammaPoisson())
        model_set.hawkes = hawkes_models.get(symbol, HawkesProcess())
        model_set.seasonality = seasonality_models.get(symbol, TimeOfDaySeasonality())
        model_set.regime = regime_detectors.get(symbol, RegimeDetector()) if regime_detectors else model_set.regime
        if cross_symbol_pool:
            model_set.pool = cross_symbol_pool
    return model_sets


def _get_normalize_fn(event_type: type) -> Callable[..., ConsolidatedEvent]:
    """Return the appropriate normalize function for a DXLink event type."""
    if event_type is Quote:
        return normalize_quote
    if event_type is DXTimeAndSale:
        return normalize_timeandsale
    if event_type is DXTheoPrice:
        return normalize_theoprice
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

    # Load significance thresholds if configured
    significance_thresholds: dict[str, dict[str, float]] = {}
    thresholds_file = config.outputs.significance_thresholds_file
    thresholds_path = Path(thresholds_file) if thresholds_file else None
    if thresholds_path and thresholds_path.exists():
        import json as _json
        try:
            raw = _json.loads(thresholds_path.read_text())
            significance_thresholds = raw.get(
                "symbols", raw.get("default", {})
            )
            logger.info(
                "Loaded %d significance threshold symbols from %s",
                len(significance_thresholds), thresholds_path,
            )
        except Exception as e:
            logger.warning("Failed to load significance thresholds: %s", e)

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

    # Initialize statistical models for enhanced analysis
    data_dir = Path(config.outputs.data_dir)
    models_path = (
        Path(config.outputs.models_state_file)
        if config.outputs.models_state_file
        else data_dir / "models_meta.json"
    )
    model_store = ModelStore(data_dir=data_dir, checkpoint_interval_sec=600.0)
    model_store._models_path = models_path

    # Prior elicitation: try to load hyperpriors from parquet history
    hyperpriors: dict[str, float] | None = None
    if config.outputs.persist_events and data_dir.exists():
        try:
            hyperpriors = prior_elicitation(data_dir, lookback_days=30)
        except Exception as e:
            logger.warning("Prior elicitation failed: %s; using defaults", e)

    cross_symbol_pool = CrossSymbolPool(
        global_alpha=hyperpriors.get("alpha", 1.0) if hyperpriors else 1.0,
        global_beta=hyperpriors.get("beta", 1.0) if hyperpriors else 1.0,
    )

    bayesian_models: dict[str, BayesianGammaPoisson] = {}
    hawkes_models: dict[str, HawkesProcess] = {}
    seasonality_models: dict[str, TimeOfDaySeasonality] = {}
    regime_detectors: dict[str, RegimeDetector] = {}
    model_sets: dict[str, ModelSet] = {}

    # Create models for each ticker's underlying and a default
    all_underlyings = list(underlying_symbols_set)
    all_underlyings.append("default")

    # Warm up: load saved model state or initialize from hyperpriors
    warm_models = model_store.warm_up(all_underlyings, hyperpriors)

    for underlying in all_underlyings:
        if underlying in warm_models:
            ms = warm_models[underlying]
            bayesian_models[underlying] = ms.bayesian
            hawkes_models[underlying] = ms.hawkes
            seasonality_models[underlying] = ms.seasonality
            regime_detectors[underlying] = RegimeDetector(
                vol_low=config.detection.vol_low,
                vol_high=config.detection.vol_high,
                vol_crash=config.detection.vol_crash,
            )
            model_sets[underlying] = ms
        else:
            alpha = hyperpriors.get("alpha", 1.0) if hyperpriors else 1.0
            beta = hyperpriors.get("beta", 1.0) if hyperpriors else 1.0
            bayesian_models[underlying] = BayesianGammaPoisson(alpha=alpha, beta=beta)
            hawkes_models[underlying] = HawkesProcess(mu=0.1, alpha=0.5, beta=1.0)
            seasonality_models[underlying] = TimeOfDaySeasonality()
            regime_detectors[underlying] = RegimeDetector(
                vol_low=config.detection.vol_low,
                vol_high=config.detection.vol_high,
                vol_crash=config.detection.vol_crash,
            )
            model_sets[underlying] = ModelSet(
                bayesian=bayesian_models[underlying],
                hawkes=hawkes_models[underlying],
                seasonality=seasonality_models[underlying],
                pool=cross_symbol_pool,
            )

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
        significance_thresholds=significance_thresholds,
        bayesian_models=bayesian_models,
        hawkes_models=hawkes_models,
        seasonality_models=seasonality_models,
        cross_symbol_pool=cross_symbol_pool,
        regime_detectors=regime_detectors,
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
        # TheoPrice on all option symbols (for delta & Greeks)
        if all_symbols:
            await streamer.subscribe(DXTheoPrice, all_symbols)
        logger.info(
            "Subscribed to Quote(%s) + TimeAndSale(%d) + TheoPrice(%d) symbols",
            ','.join(underlying_symbols),
            len(underlying_symbols) + len(all_symbols),
            len(all_symbols),
        )
        logger.info("Listening for volume anomalies...")

        # Unified consumer: three producers → bounded queue → single consumer
        queue: asyncio.Queue[ConsolidatedEvent] = asyncio.Queue(maxsize=config.stream.backpressure_queue_size)
        counter = [0]  # shared event counter

        producers = []
        for event_type in (Quote, DXTimeAndSale, DXTheoPrice):
            normalize_fn = _get_normalize_fn(event_type)
            t = asyncio.create_task(_produce_events(streamer, event_type, normalize_fn, queue, counter))
            producers.append(t)

        consumer = asyncio.create_task(
            _consume_consolidated(
                queue, store, rules, sinks,
                bayesian_models, hawkes_models, seasonality_models,
                cross_symbol_pool=cross_symbol_pool,
                regime_detectors=regime_detectors,
                model_store=model_store,
                model_sets=model_sets,
            )
        )

        # Also start parquet flush loop if persistence enabled
        data_dir = Path(config.outputs.data_dir)
        if store.persist:
            await store.start_flush_loop(data_dir)

        # Dynamic strike manager for intraday chain updates
        strike_mgr = DynamicStrikeManager(session, config.watchlist, rescan_interval_min=60)
        await strike_mgr.initial_scan()

        # Shutdown monitor: checks both signal event and daily cutoff
        # Futures (ES, NQ, etc.) trade overnight — only shut down at
        # market close if there are no futures contracts in the watchlist.
        has_futures = any(t.option_type == "futures" for t in config.watchlist.tickers)

        async def _shutdown_monitor() -> None:
            """Wait for SIGTERM or market close."""
            while True:
                if shutdown_event is not None and shutdown_event.is_set():
                    logger.info("Shutdown signal received")
                    return
                now = dt.datetime.now(dt.UTC)
                now_et = now - dt.timedelta(hours=4)  # approx ET (no DST handling for now)
                # For futures, markets close at 17:00 ET then reopen at 18:00 ET.
                # Only shut down if we're past the final close (17:00 ET next day)
                # — i.e., the overnight session has ended.
                # Simple heuristic: if it's Friday 17:30+ ET, shut down (weekend).
                # Otherwise, if has_futures, never auto-shutdown (futures trade overnight).
                if has_futures:
                    # Futures trade overnight; only auto-exit on weekends after 17:30 ET Friday
                    if now_et.weekday() == 4 and now_et.hour >= 17:  # Friday 17:00+ ET
                        logger.info("Futures weekend shutdown (Friday close) — exiting")
                        return
                else:
                    # Equity options only — shut down at 17:00 ET (21:00 UTC)
                    if now_et.hour >= 17:
                        logger.info("Market close reached (17:00 ET) — shutting down")
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

        # Save model state for next warm start
        checkpoint_models = _compile_model_sets(
            model_sets, bayesian_models, hawkes_models,
            seasonality_models, cross_symbol_pool,
            regime_detectors or {},
        )
        model_store.save(checkpoint_models)

        logger.info("Scanner shutdown complete.")


if __name__ == "__main__":
    app()
