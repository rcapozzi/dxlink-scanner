"""Tests for the CEL-based rule engine."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from dxlink_scanner.config import CelAlertRule, DetectionConfig, TickerConfig, WatchlistConfig
from dxlink_scanner.models import TimeAndSaleEvent
from dxlink_scanner.rules.cel_engine import CELRuleEngine
from dxlink_scanner.stats import RollingStatsManager


def make_event(
    symbol: str = "SYM",
    price: float = 2.50,
    size: int = 100,
) -> TimeAndSaleEvent:
    return TimeAndSaleEvent(
        symbol=symbol,
        price=Decimal(str(price)),
        size=size,
        timestamp=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
    )


def test_cel_engine_is_option():
    """Test a simple size threshold rule."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {"SYMC090": [CelAlertRule(name="large_print", expression="trade.is_option && trade.size >= 100")]}
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules, underlying_symbols=("SYM"))

    # Below threshold
    alert = engine.process(make_event("SYMC090", 2.50, 50))
    assert alert is None

    # At threshold
    alert = engine.process(make_event("SYMC090", 2.50, 100))
    assert alert is not None
    assert alert.rule_name == "large_print"
    assert alert.size == 100


def test_cel_engine_simple_threshold():
    """Test a simple size threshold rule."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {"SYM": [CelAlertRule(name="large_print", expression="trade.size >= 100")]}
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    # Below threshold
    alert = engine.process(make_event("SYM", 2.50, 50))
    assert alert is None

    # At threshold
    alert = engine.process(make_event("SYM", 2.50, 100))
    assert alert is not None
    assert alert.rule_name == "large_print"
    assert alert.size == 100


def test_cel_engine_compound_expression():
    """Test compound CEL expressions with &&."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {
        "SYM": [
            CelAlertRule(
                name="large_and_expensive",
                expression="trade.size >= 50 && trade.price > 2.0",
            )
        ]
    }
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    # Both conditions met
    alert = engine.process(make_event("SYM", 2.50, 100))
    assert alert is not None
    assert alert.rule_name == "large_and_expensive"

    # Size met but price too low
    alert = engine.process(make_event("SYM", 1.50, 100))
    assert alert is None

    # Price met but size too low
    alert = engine.process(make_event("SYM", 2.50, 25))
    assert alert is None


def test_cel_engine_default_rules():
    """Test that default rules apply when no per-symbol rule exists."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    defaults = [
        CelAlertRule(
            name="global_min_size",
            expression="trade.size >= 1000",
        )
    ]
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        per_symbol_rules={},
        default_rules=defaults,
    )

    # Not a registered symbol — should use default rule
    alert = engine.process(make_event("UNKNOWN", 2.50, 500))
    assert alert is None

    alert = engine.process(make_event("UNKNOWN", 2.50, 1000))
    assert alert is not None
    assert alert.rule_name == "global_min_size"


def test_cel_engine_stats_variable():
    """Test that stats.* variables are available in CEL rules."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)

    # Populate rolling stats with 50 entries of size 100
    for _ in range(50):
        stats.add("SYM", 100)

    rules = {
        "SYM": [
            CelAlertRule(
                name="anomaly",
                expression="trade.size > stats.median * 5.0",
            )
        ]
    }
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    # median is 100, threshold is 500 — 1000 should trigger
    alert = engine.process(make_event("SYM", 2.50, 1000))
    assert alert is not None
    assert alert.rule_name == "anomaly"

    # 300 is below 500 — should not trigger
    alert = engine.process(make_event("SYM", 2.50, 300))
    assert alert is None


def test_cel_engine_invalid_expression():
    """Test that invalid CEL expressions are logged and skipped at compile."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {
        "SYM": [
            CelAlertRule(
                name="bad_rule",
                expression="trade.size >= @@ INVALID",
            )
        ]
    }
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    # Should not crash — the bad rule is simply not evaluated
    alert = engine.process(make_event("SYM", 2.50, 100))
    assert alert is None


def test_cel_engine_multiple_rules():
    """Test that multiple rules are evaluated and the first match wins."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {
        "SYM": [
            CelAlertRule(name="small", expression="trade.size >= 10"),
            CelAlertRule(name="large", expression="trade.size >= 1000"),
        ]
    }
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    # Size 100 should trigger the first matching rule ("small"), not "large"
    alert = engine.process(make_event("SYM", 2.50, 100))
    assert alert is not None
    assert alert.rule_name == "small"

    # Size 1000 matches the first rule too
    alert = engine.process(make_event("SYM", 2.50, 1000))
    assert alert is not None
    assert alert.rule_name == "small"


def test_cel_engine_config_model():
    """Test that CelAlertRule config model works."""
    rule = CelAlertRule(
        name="test_rule",
        expression="trade.size >= 50",
        severity="medium",
    )
    assert rule.name == "test_rule"
    assert rule.expression == "trade.size >= 50"
    assert rule.severity == "medium"


def test_cel_engine_config_model_default_severity():
    """Test that CelAlertRule defaults severity to 'high'."""
    rule = CelAlertRule(name="test", expression="trade.size >= 1")
    assert rule.severity == "high"


def test_cel_engine_from_config():
    """Test that alert_rules can be loaded from a TickerConfig."""
    ticker = TickerConfig(
        symbol="SPY",
        alert_rules=[
            CelAlertRule(name="big_print", expression="trade.size >= 100"),
        ],
    )
    assert len(ticker.alert_rules) == 1
    assert ticker.alert_rules[0].name == "big_print"
    assert ticker.alert_rules[0].expression == "trade.size >= 100"


def test_cel_engine_underlying_vs_option():
    """Test that underlying and option symbols are handled separately."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {
        "SPY": [
            CelAlertRule(
                name="underlying_large",
                expression="trade.size >= 500",
            )
        ]
    }
    # SPY is an underlying symbol
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        per_symbol_rules=rules,
        underlying_symbols={"SPY"},
    )

    # Underlying trade of 500 — should trigger
    alert = engine.process(make_event("SPY", 450.00, 500))
    assert alert is not None
    assert alert.size == 500


def test_cel_engine_rule_count():
    """Test that rule_count returns total compiled rules."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {
        "SYM1": [
            CelAlertRule(name="r1", expression="trade.size >= 10"),
            CelAlertRule(name="r2", expression="trade.size >= 100"),
        ],
        "SYM2": [CelAlertRule(name="r3", expression="trade.size >= 50")],
    }
    defaults = [CelAlertRule(name="r4", expression="trade.size >= 1000")]
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        per_symbol_rules=rules,
        default_rules=defaults,
    )
    assert engine.rule_count == 4


def test_cel_engine_decimal_conversion():
    """Test that Decimal prices are properly converted to float for CEL."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {
        "SYM": [
            CelAlertRule(
                name="price_check",
                expression="trade.price > 2.0",
            )
        ]
    }
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    # price is 3.50 — should trigger
    alert = engine.process(make_event("SYM", 3.50, 100))
    assert alert is not None
    assert alert.rule_name == "price_check"

    # price is 1.50 — should not trigger
    alert = engine.process(make_event("SYM", 1.50, 100))
    assert alert is None


def test_cel_engine_severity_in_alert():
    """Test that the severity is accessible in the rule config (future use)."""
    rule = CelAlertRule(
        name="high_priority",
        expression="trade.size >= 100",
        severity="critical",
    )
    assert rule.severity == "critical"


def test_cel_engine_severity_propagated_to_alert():
    """Test that rule severity is passed through to the Alert object."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {
        "SYM": [
            CelAlertRule(
                name="critical_print",
                expression="trade.size >= 100",
                severity="critical",
            )
        ]
    }
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    alert = engine.process(make_event("SYM", 2.50, 100))
    assert alert is not None
    assert alert.severity == "critical"


def test_cel_engine_severity_default():
    """Test that CEL rules default to severity='high'."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {"SYM": [CelAlertRule(name="default_sev", expression="trade.size >= 100")]}
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    alert = engine.process(make_event("SYM", 2.50, 100))
    assert alert is not None
    assert alert.severity == "high"


@pytest.mark.asyncio
async def test_cel_engine_aprocess_is_async_compatible():
    """Test that aprocess returns the same result as process."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {"SYM": [CelAlertRule(name="test", expression="trade.size >= 50")]}
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    result_sync = engine.process(make_event("SYM", 2.50, 100))
    result_async = await engine.aprocess(make_event("SYM", 2.50, 100))
    assert result_sync is not None
    assert result_async is not None
    assert result_sync.rule_name == result_async.rule_name


def test_cel_engine_underlying_scoped_rules():
    """Test that underlying_alert_rules apply to all option symbols of an underlying."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(
        tickers=[
            TickerConfig(
                symbol="SPY",
                underlying_alert_rules=[
                    CelAlertRule(
                        name="spy_option_sweep",
                        expression="trade.is_option && trade.size >= 50",
                    )
                ],
            )
        ]
    )
    stats = RollingStatsManager(cfg)
    underlying_map = {
        ".SPY260731C500": "SPY",
        ".SPY260731P500": "SPY",
    }
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        underlying_symbols={"SPY"},
        underlying_symbol_map=underlying_map,
    )

    # SPY call option — should trigger underlying-scoped rule
    alert = engine.process(make_event(".SPY260731C500", 2.50, 100))
    assert alert is not None
    assert alert.rule_name == "spy_option_sweep"

    # SPY put option — should also trigger (same underlying rule)
    alert = engine.process(make_event(".SPY260731P500", 1.75, 60))
    assert alert is not None
    assert alert.rule_name == "spy_option_sweep"

    # Below threshold — should not trigger
    alert = engine.process(make_event(".SPY260731C500", 2.50, 25))
    assert alert is None


def test_cel_engine_underlying_vs_per_symbol_priority():
    """Per-symbol rules take priority over underlying-scoped rules."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(
        tickers=[
            TickerConfig(
                symbol="SPY",
                underlying_alert_rules=[
                    CelAlertRule(name="underlying_rule", expression="trade.size >= 30"),
                ],
            )
        ]
    )
    stats = RollingStatsManager(cfg)
    underlying_map = {".SPY260731C500": "SPY"}
    # Per-symbol rule for the exact option symbol
    per_symbol = {
        ".SPY260731C500": [
            CelAlertRule(name="per_symbol_rule", expression="trade.size >= 20"),
        ]
    }
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        per_symbol_rules=per_symbol,
        underlying_symbols={"SPY"},
        underlying_symbol_map=underlying_map,
    )

    # Should hit per-symbol rule first (size=25 → matches per_symbol_rule at >=20)
    alert = engine.process(make_event(".SPY260731C500", 2.50, 25))
    assert alert is not None
    assert alert.rule_name == "per_symbol_rule"


def test_cel_engine_underlying_only_when_per_symbol_empty():
    """Underlying-scoped rules apply only when no per-symbol rule is registered."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(
        tickers=[
            TickerConfig(
                symbol="SPY",
                underlying_alert_rules=[
                    CelAlertRule(name="spy_rule", expression="trade.size >= 100"),
                ],
            )
        ]
    )
    stats = RollingStatsManager(cfg)
    underlying_map = {".SPY260731C500": "SPY"}
    # No per-symbol rules — underlying-scoped should be the fallback
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        underlying_symbols={"SPY"},
        underlying_symbol_map=underlying_map,
    )

    alert = engine.process(make_event(".SPY260731C500", 2.50, 100))
    assert alert is not None
    assert alert.rule_name == "spy_rule"


def test_cel_engine_underlying_fallback_to_default():
    """Underlying-scoped rules fall through to default rules when no match."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(
        tickers=[
            TickerConfig(
                symbol="SPY",
                underlying_alert_rules=[
                    CelAlertRule(name="spy_rule", expression="trade.size >= 1000"),
                ],
            )
        ]
    )
    stats = RollingStatsManager(cfg)
    underlying_map = {".SPY260731C500": "SPY"}
    defaults = [
        CelAlertRule(name="global_default", expression="trade.size >= 50"),
    ]
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        default_rules=defaults,
        underlying_symbols={"SPY"},
        underlying_symbol_map=underlying_map,
    )

    # Size 60 — doesn't match underlying rule (>=1000) but matches default (>=50)
    alert = engine.process(make_event(".SPY260731C500", 2.50, 60))
    assert alert is not None
    assert alert.rule_name == "global_default"


def test_cel_engine_resolve_underlying_prefix_fallback():
    """_resolve_underlying falls back to watchlist prefix matching.

    In the real scanner, option symbols have a '.' prefix (e.g.
    '.SPY260731C500') while the underlying is plain 'SPY'. The prefix
    match uses startswith, so '.SPY260731C500' does NOT start with 'SPY'.
    The underlying_symbol_map handles this mapping; the prefix fallback
    is a secondary mechanism for symbols without the map.
    """
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[TickerConfig(symbol="SPY")])
    stats = RollingStatsManager(cfg)
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        underlying_symbols={"SPY"},
    )

    # Without the map, symbols with a '.' prefix won't match by prefix
    # because ".SPY..." does not start with "SPY"
    result = engine._resolve_underlying(".SPY260731C500")
    assert result is None

    # With a symbol that directly starts with the underlying (no dot prefix)
    result = engine._resolve_underlying("SPY260731C500")
    assert result == "SPY"


def test_cel_engine_underlying_rules_in_rule_count():
    """rule_count includes underlying-scoped rules."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(
        tickers=[
            TickerConfig(
                symbol="SPY",
                underlying_alert_rules=[
                    CelAlertRule(name="rule1", expression="trade.size >= 10"),
                    CelAlertRule(name="rule2", expression="trade.size >= 50"),
                ],
            )
        ]
    )
    stats = RollingStatsManager(cfg)
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        underlying_symbols={"SPY"},
    )

    # 2 underlying-scoped rules compiled under "SPY" key
    assert engine.rule_count == 2


def test_cel_engine_multiple_underlyings():
    """Underlying-scoped rules for multiple underlyings."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(
        tickers=[
            TickerConfig(
                symbol="SPY",
                underlying_alert_rules=[
                    CelAlertRule(name="spy_big", expression="trade.size >= 100"),
                ],
            ),
            TickerConfig(
                symbol="QQQ",
                underlying_alert_rules=[
                    CelAlertRule(name="qqq_big", expression="trade.size >= 200"),
                ],
            ),
        ]
    )
    stats = RollingStatsManager(cfg)
    underlying_map = {
        ".SPY260731C500": "SPY",
        ".QQQ260731C500": "QQQ",
    }
    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        underlying_symbols={"SPY", "QQQ"},
        underlying_symbol_map=underlying_map,
    )

    # SPY option — should trigger spy_big
    alert = engine.process(make_event(".SPY260731C500", 2.50, 100))
    assert alert is not None
    assert alert.rule_name == "spy_big"

    # QQQ option — should trigger qqq_big (higher threshold)
    alert = engine.process(make_event(".QQQ260731C500", 1.75, 200))
    assert alert is not None
    assert alert.rule_name == "qqq_big"

    # QQQ option with size 100 — matches spy threshold but not qqq
    alert = engine.process(make_event(".QQQ260731C500", 1.75, 100))
    assert alert is None


def test_cel_engine_alert_includes_underlying_price() -> None:
    """Alert should include the most recent underlying_price from the snapshot store.

    underlying_price is derived from Quote mid_price (bid+ask)/2 on the
    underlying streamer symbol.
    """
    from unittest.mock import MagicMock

    from dxlink_scanner.models import ConsolidatedSnapshot

    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {"SYM": [CelAlertRule(name="big_print", expression="trade.size >= 100")]}

    # Underlying snapshot: has mid_price from Quote (used as underlying_price)
    snap = ConsolidatedSnapshot(
        symbol="SYM",
        underlying_symbol="SYM",
        updated_at=datetime.now(tz=UTC),
        mid_price=Decimal("450.00"),
    )
    mock_store = MagicMock()
    mock_store.get.return_value = snap

    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        per_symbol_rules=rules,
        snapshot_store=mock_store,
    )

    alert = engine.process(make_event("SYM", 2.50, 100))
    assert alert is not None
    assert alert.underlying_price == 450.0


def test_cel_engine_alert_underlying_price_from_underlying_snapshot() -> None:
    """When option snapshot has no mid_price, fall back to underlying snapshot.

    The option's underlying_symbol points to the underlying streamer
    symbol (e.g. /ES:XCME for futures), whose Quote mid_price
    serves as underlying_price.
    """
    from unittest.mock import MagicMock

    from dxlink_scanner.models import ConsolidatedSnapshot

    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {".ES260731C525": [CelAlertRule(name="big_print", expression="trade.size >= 100")]}

    # Option snapshot: no mid_price, underlying_symbol=/ES:XCME
    option_snap = ConsolidatedSnapshot(
        symbol=".ES260731C525",
        underlying_symbol="/ES:XCME",
        updated_at=datetime.now(tz=UTC),
    )
    # Underlying snapshot: has mid_price from Quote on /ES:XCME
    underlying_snap = ConsolidatedSnapshot(
        symbol="/ES:XCME",
        underlying_symbol="/ES:XCME",
        updated_at=datetime.now(tz=UTC),
        mid_price=Decimal("450.00"),
    )
    mock_store = MagicMock()

    def mock_get(symbol: str) -> ConsolidatedSnapshot | None:
        if symbol == ".ES260731C525":
            return option_snap
        if symbol == "/ES:XCME":
            return underlying_snap
        return None

    mock_store.get.side_effect = mock_get

    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        per_symbol_rules=rules,
        snapshot_store=mock_store,
    )

    alert = engine.process(make_event(".ES260731C525", 2.50, 100))
    assert alert is not None
    assert alert.underlying_price == 450.0


def test_cel_engine_alert_underlying_price_equity_option() -> None:
    """Equity option: underlying_symbol maps to SPY, mid_price from Quote on SPY."""
    from unittest.mock import MagicMock

    from dxlink_scanner.models import ConsolidatedSnapshot

    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {".SPY260731C525": [CelAlertRule(name="big_print", expression="trade.size >= 100")]}

    # Option snapshot: no mid_price, underlying_symbol=SPY
    option_snap = ConsolidatedSnapshot(
        symbol=".SPY260731C525",
        underlying_symbol="SPY",
        updated_at=datetime.now(tz=UTC),
    )
    # Underlying snapshot: has mid_price from Quote on SPY
    underlying_snap = ConsolidatedSnapshot(
        symbol="SPY",
        underlying_symbol="SPY",
        updated_at=datetime.now(tz=UTC),
        mid_price=Decimal("530.00"),
    )
    mock_store = MagicMock()

    def mock_get(symbol: str) -> ConsolidatedSnapshot | None:
        if symbol == ".SPY260731C525":
            return option_snap
        if symbol == "SPY":
            return underlying_snap
        return None

    mock_store.get.side_effect = mock_get

    engine = CELRuleEngine(
        cfg,
        watchlist,
        stats,
        per_symbol_rules=rules,
        snapshot_store=mock_store,
    )

    alert = engine.process(make_event(".SPY260731C525", 2.50, 100))
    assert alert is not None
    assert alert.underlying_price == 530.0


def test_cel_engine_alert_without_snapshot_store() -> None:
    """When no snapshot_store is provided, underlying_price should be None."""
    cfg = DetectionConfig()
    watchlist = WatchlistConfig(tickers=[])
    stats = RollingStatsManager(cfg)
    rules = {"SYM": [CelAlertRule(name="big_print", expression="trade.size >= 100")]}
    engine = CELRuleEngine(cfg, watchlist, stats, per_symbol_rules=rules)

    alert = engine.process(make_event("SYM", 2.50, 100))
    assert alert is not None
    assert alert.underlying_price is None
