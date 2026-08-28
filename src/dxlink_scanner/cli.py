"""Typer CLI entry point for the options volume scanner."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
from collections.abc import Callable
from decimal import Decimal, getcontext
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
    AdaptiveTuner,
    BayesianGammaPoisson,
    CrossAssetHawkes,
    CrossSymbolPool,
    FlowMetrics,
    HawkesProcess,
    ModelSet,
    ModelStore,
    RegimeDetector,
    RollingStatsManager,
    TimeOfDaySeasonality,
    VolumeAtPrice,
    prior_elicitation,
)

# Drift logger for local vs DXLink delta comparison
DRIFT_LOGGER = logging.getLogger("dxlink_scanner.delta_drift")
DRIFT_LOGGER.setLevel(logging.INFO)

SinkType = StdoutSink | WebhookSink
RuleEngine = CELRuleEngine


def _black_scholes_delta(
    spot: Decimal,
    strike: Decimal,
    expiry_str: str,
    option_type: str,
    rate: Decimal = Decimal("0.045"),  # ~SOFR 1D
) -> Decimal | None:
    """Compute Black-Scholes delta for European option.

    Args:
        spot: Current underlying price
        strike: Option strike price
        expiry_str: Expiry date as ISO string (YYYY-MM-DD)
        option_type: "call" or "put"
        rate: Risk-free rate (default ~SOFR)

    Returns:
        Delta as Decimal, or None if calculation fails
    """
    getcontext().prec = 28

    try:
        expiry_date = dt.datetime.fromisoformat(expiry_str).date()
        today = dt.datetime.now(dt.UTC).date()
        dte = (expiry_date - today).days
        if dte < 0:
            return None
        if dte == 0:
            now_utc = dt.datetime.now(dt.UTC)
            market_close = now_utc.replace(hour=20, minute=0, second=0, microsecond=0)
            seconds_left = (market_close - now_utc).total_seconds()
            if seconds_left <= 0:
                return None
            t = Decimal(seconds_left) / Decimal(86400)
        else:
            t = Decimal(dte + 1)

        S = float(spot)
        K = float(strike)
        r = float(rate)
        T = float(t)

        if T <= 0 or S <= 0 or K <= 0:
            return None

        sigma = 0.15

        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))

        if option_type == "call":
            return Decimal(str(nd1))
        else:
            return Decimal(str(nd1 - 1))
    except Exception:
        return None


def _compute_local_delta(
    streamer_symbol: str,
    mid_price: Decimal,
) -> Decimal | None:
    """Compute local delta from streamer symbol and mid_price.

    Parses streamer symbol format like .SPY260731C500:
    - Root: SPY
    - Expiry: 260731 (YYMMDD)
    - Type: C/P
    - Strike: 500 (in points, need to divide by 1000 for actual price)
    """
    try:
        sym = streamer_symbol.lstrip(".")
        if ":" in sym:
            sym = sym.split(":")[0]

        type_idx = -1
        for i, ch in enumerate(sym):
            if ch in ("C", "P"):
                type_idx = i
        if type_idx == -1:
            return None

        option_type = "call" if sym[type_idx] == "C" else "put"
        root = sym[:type_idx]
        expiry_part = sym[type_idx + 1 : type_idx + 7]
        strike_part = sym[type_idx + 7 :]

        if len(expiry_part) != 6 or not strike_part:
            return None

        year = 2000 + int(expiry_part[:2])
        month = int(expiry_part[2:4])
        day = int(expiry_part[4:6])
        expiry_str = f"{year:04d}-{month:02d}-{day:02d}"

        strike_raw = int(strike_part)
        if root in ("SPY", "QQQ", "IWM", "SPX"):
            strike = Decimal(str(strike_raw)) / Decimal("1000")
        else:
            strike = Decimal(str(strike_raw)) / Decimal("1000")

        return _black_scholes_delta(mid_price, strike, expiry_str, option_type)
    except Exception:
        return None


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
        async for msg in streamer.listen(event_type):
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
    volume_at_price_models: dict[str, VolumeAtPrice] | None = None,
    flow_metrics: dict[str, FlowMetrics] | None = None,
    cross_asset_hawkes: CrossAssetHawkes | None = None,
    model_store: ModelStore | None = None,
    model_sets: dict[str, ModelSet] | None = None,
    adaptive_tuner: AdaptiveTuner | None = None,
) -> None:
    """Consumer: process events from the unified queue."""
    while True:
        event = await queue.get()
        store.ingest(event)

        if model_store and model_sets:
            model_store.maybe_checkpoint(
                _compile_model_sets(
                    model_sets,
                    bayesian_models,
                    hawkes_models,
                    seasonality_models,
                    cross_symbol_pool,
                    regime_detectors or {},
                )
            )

        if event.source_type == "TIME_AND_SALE":
            snap = store.get(event.symbol)
            dxlink_delta = snap.delta if snap and snap.delta is not None else None

            local_delta = None
            if snap and snap.bid_price is not None and snap.ask_price is not None:
                mid_price = (snap.bid_price + snap.ask_price) / 2
                local_delta = _compute_local_delta(event.symbol, mid_price)
                if local_delta is not None and dxlink_delta is not None:
                    drift = float(local_delta) - float(dxlink_delta)
                    DRIFT_LOGGER.info(
                        "delta_drift symbol=%s dxlink=%.4f local=%.4f diff=%.4f mid=%.2f",
                        event.symbol,
                        float(dxlink_delta),
                        float(local_delta),
                        drift,
                        float(mid_price),
                    )

            delta = local_delta if local_delta is not None else dxlink_delta

            underlying = snap.underlying_symbol if snap else event.symbol
            if underlying not in bayesian_models:
                underlying = "default"

            trade_time = dt.datetime.now(dt.UTC).timestamp()
            size = event.last_trade_size or 0

            bayesian_models[underlying].update(1)
            hawkes_models[underlying].add_event(trade_time)

            if event.last_trade_time:
                trade_dt = dt.datetime.fromtimestamp(event.last_trade_time / 1000, tz=dt.UTC)
                seasonality_models[underlying].add_observation(trade_dt, float(size))

            if cross_symbol_pool is not None:
                cross_symbol_pool.update_symbol(underlying, 1, exposure=1.0)

            vap = volume_at_price_models.get(underlying) if volume_at_price_models else None
            if vap:
                trade_price_float = float(event.last_trade_price) if event.last_trade_price else 0.0
                vap.add_trade(trade_price_float, size)

            flow = flow_metrics.get(underlying) if flow_metrics else None
            if flow and snap:
                flow.update(snap, float(event.last_trade_price) if event.last_trade_price else 0.0, size)

            if cross_asset_hawkes:
                trade_time = event.last_trade_time / 1000.0 if event.last_trade_time else 0.0
                cross_asset_hawkes.add_event(underlying, trade_time)

            if adaptive_tuner:
                adaptive_tuner.record_event(underlying)

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
                if adaptive_tuner:
                    adaptive_tuner.record_alert(alert.bayesian_decision or False, underlying)
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
    for symbol, model_set in model_sets.items():
        model_set.bayesian = bayesian_models.get(symbol, BayesianGammaPoisson())
        model_set.hawkes = hawkes_models.get(symbol, HawkesProcess())
        model_set.seasonality = seasonality_models.get(symbol, TimeOfDaySeasonality())
        model_set.regime = regime_detectors.get(symbol, RegimeDetector()) if regime_detectors else model_set.regime
        if cross_symbol_pool:
            model_set.pool = cross_symbol_pool
    return model_sets


def _get_normalize_fn(event_type: type) -> Callable[..., ConsolidatedEvent]:
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
    alert = rules.process(event)
    if alert:
        for sink in sinks:
            await sink.send(alert)


app = typer.Typer(help="Real-time options volume scanner using Tastytrade DXLink")

logger = logging.getLogger(__name__)


@app.command()
def main(
    config_path: Annotated[Path, typer.Option("--config", "-c", help="Path to config file")] = Path(
        "dxlink-scanner.yaml"
    ),
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
    setup_logging(verbose)
    if debug_messages:
        logging.getLogger("tastytrade.streamer").setLevel(logging.DEBUG)
    load_dotenv()
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
    session = auth.get_session()
    await session.refresh(force=True)
    logger.info("Authenticated with Tastytrade")

    significance_thresholds: dict[str, dict[str, float]] = {}
    thresholds_file = config.outputs.significance_thresholds_file
    thresholds_path = Path(thresholds_file) if thresholds_file else None
    if thresholds_path and thresholds_path.exists():
        import json as _json

        try:
            raw = _json.loads(thresholds_path.read_text())
            significance_thresholds = raw.get("symbols", raw.get("default", {}))
            logger.info(
                "Loaded %d significance threshold symbols from %s",
                len(significance_thresholds),
                thresholds_path,
            )
        except Exception as e:
            logger.warning("Failed to load significance thresholds: %s", e)

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
        if ticker.option_type == "futures" and underlying_info.symbol:
            price_symbol = underlying_info.symbol
        else:
            price_symbol = ticker.symbol
        underlying_price = await loader.get_underlying_price(price_symbol, ticker.option_type)
        if underlying_price is None:
            logger.warning(
                "Could not resolve underlying price for %s; using all %d strikes",
                ticker.symbol,
                len(strikes),
            )
        symbols = [s.symbol for s in strikes]
        count = len(symbols)
        all_symbols.extend(symbols)

        if ticker.option_type == "futures" and underlying_info.symbol:
            quote_symbol = InstrumentResolver.resolve_futures_streamer(ticker.symbol)
        elif ticker.option_type == "equity" and ticker.symbol in ("SPX", "NDX", "RUT"):
            quote_symbol = InstrumentResolver.resolve_index_option_streamer(ticker.symbol)
        else:
            quote_symbol = ticker.symbol

        underlying_symbols.append(quote_symbol)
        underlying_symbols_set.add(quote_symbol)

        for s in strikes:
            underlying_map[s.symbol] = quote_symbol
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

    stats_mgr = RollingStatsManager(config.detection)
    store = SnapshotStore(config.stream, persist=config.outputs.persist_events)
    store.set_underlying_map(underlying_map)
    for sym in underlying_symbols:
        store.bootstrap_snapshot(sym, underlying_map.get(sym, sym))

    data_dir = Path(config.outputs.data_dir)
    models_path = (
        Path(config.outputs.models_state_file) if config.outputs.models_state_file else data_dir / "models_meta.json"
    )
    model_store = ModelStore(data_dir=data_dir, checkpoint_interval_sec=600.0)
    model_store._models_path = models_path

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
    vap_models: dict[str, VolumeAtPrice] = {}
    flow_metrics: dict[str, FlowMetrics] = {}
    model_sets: dict[str, ModelSet] = {}

    tick_size = getattr(config.stream, "tick_size", None) or 0.01

    all_underlyings = list(underlying_symbols_set)
    all_underlyings.append("default")

    warm_models = model_store.warm_up(all_underlyings, hyperpriors)

    for underlying in all_underlyings:
        vap_models[underlying] = VolumeAtPrice(tick_size=tick_size)
        flow_metrics[underlying] = FlowMetrics(symbol=underlying)

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

    cross_asset_hawkes = CrossAssetHawkes(
        symbols=all_underlyings,
        mu=0.1,
        decay=1.0,
    )

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
        volume_at_price_models=vap_models,
        flow_metrics=flow_metrics,
        cross_asset_hawkes=cross_asset_hawkes,
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

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, underlying_symbols)
        await streamer.subscribe(DXTimeAndSale, underlying_symbols + all_symbols)
        if all_symbols:
            await streamer.subscribe(DXTheoPrice, all_symbols)
        logger.info(
            "Subscribed to Quote(%s) + TimeAndSale(%d) + TheoPrice(%d) symbols",
            ",".join(underlying_symbols),
            len(underlying_symbols) + len(all_symbols),
            len(all_symbols),
        )
        logger.info("Listening for volume anomalies...")

        queue: asyncio.Queue[ConsolidatedEvent] = asyncio.Queue(maxsize=config.stream.backpressure_queue_size)
        counter = [0]

        producers = []
        for event_type in (Quote, DXTimeAndSale, DXTheoPrice):
            normalize_fn = _get_normalize_fn(event_type)
            t = asyncio.create_task(_produce_events(streamer, event_type, normalize_fn, queue, counter))
            producers.append(t)

        consumer = asyncio.create_task(
            _consume_consolidated(
                queue,
                store,
                rules,
                sinks,
                bayesian_models,
                hawkes_models,
                seasonality_models,
                cross_symbol_pool=cross_symbol_pool,
                regime_detectors=regime_detectors,
                volume_at_price_models=vap_models,
                flow_metrics=flow_metrics,
                cross_asset_hawkes=cross_asset_hawkes,
                model_store=model_store,
                model_sets=model_sets,
                adaptive_tuner=AdaptiveTuner(config.detection, config_path=config.outputs.models_state_file),
            )
        )

        if store.persist:
            await store.start_flush_loop(data_dir)

        strike_mgr = DynamicStrikeManager(session, config.watchlist, rescan_interval_min=60)
        await strike_mgr.initial_scan()

        has_futures = any(t.option_type == "futures" for t in config.watchlist.tickers)

        async def _shutdown_monitor() -> None:
            while True:
                if shutdown_event is not None and shutdown_event.is_set():
                    logger.info("Shutdown signal received")
                    return
                now = dt.datetime.now(dt.UTC)
                now_et = now - dt.timedelta(hours=4)
                if has_futures:
                    if now_et.weekday() == 4 and now_et.hour >= 17:
                        logger.info("Futures weekend shutdown (Friday close) — exiting")
                        return
                else:
                    if now_et.hour >= 17:
                        logger.info("Market close reached (17:00 ET) — shutting down")
                        return
                await asyncio.sleep(10)

        monitor = asyncio.create_task(_shutdown_monitor())

        async def _rescan_loop() -> None:
            while True:
                await asyncio.sleep(60)
                delta = await strike_mgr.rescan()
                if delta and (delta.added or delta.removed):
                    if delta.added:
                        await streamer.subscribe(DXTimeAndSale, delta.added)
                        logger.info("Subscribed to %d new symbols", len(delta.added))
                    if delta.removed:
                        logger.info("Unsubscribed %d stale symbols", len(delta.removed))

        rescan = asyncio.create_task(_rescan_loop())

        async def _stats_logger() -> None:
            last_count = 0
            while True:
                await asyncio.sleep(30)
                current_count = counter[0]
                delta = current_count - last_count
                logger.info("Stats: %d events in last 30s (total=%d, queue=%d)", delta, current_count, queue.qsize())
                last_count = current_count

        stats_task = asyncio.create_task(_stats_logger())

        done, pending = await asyncio.wait([*producers, monitor], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if not isinstance(t, asyncio.Task):
                continue
            exc = t.exception()
            if exc:
                logger.error("Task exited with error: %s", exc)
        for t in pending:
            t.cancel()
        consumer.cancel()
        rescan.cancel()
        stats_task.cancel()

        if store.persist:
            await store.flush_remaining(data_dir)

        checkpoint_models = _compile_model_sets(
            model_sets,
            bayesian_models,
            hawkes_models,
            seasonality_models,
            cross_symbol_pool,
            regime_detectors or {},
        )
        model_store.save(checkpoint_models)

        logger.info("Scanner shutdown complete.")


if __name__ == "__main__":
    app()
