"""Tests for configuration loading."""

from pathlib import Path

import yaml

from dxlink_scanner.config import (
    CelAlertRule,
    ScannerConfig,
    TastyTradeConfig,
    TickerConfig,
    WatchlistConfig,
    load_config,
)


def test_load_config(tmp_path: Path):
    """Test loading a valid config file with multiple tickers."""
    config_data = {
        "tastytrade": {
            "client_id": "test_id",
            "client_secret": "test_secret",
            "refresh_token": "test_refresh",
            "sandbox": True,
        },
        "watchlist": {
            "default_alert_rules": [
                {"name": "absolute_size", "expression": "trade.size >= 1000", "severity": "critical"},
            ],
            "tickers": [
                {
                    "symbol": "SPY",
                    "strikes_around_atm": 10,
                    "expiration_filter": "0DTE",
                    "underlying_alert_rules": [
                        {"name": "spy_large_print", "expression": "trade.is_option && trade.size >= 100",
                        "severity": "high"},
                    ],
                },
                {
                    "symbol": "QQQ",
                    "strikes_around_atm": 15,
                    "expiration_filter": "all",
                    "underlying_alert_rules": [
                        {"name": "qqq_sweep", "expression": "trade.size >= 50",
                        "severity": "medium"},
                    ],
                },
                {
                    "symbol": "IWM",
                    "strikes_around_atm": 20,
                    "expiration_filter": "all",
                },
                {
                    "symbol": "SPX",
                    "option_type": "equity",
                    "strikes_around_atm": 10,
                    "underlying_alert_rules": [
                        {"name": "spx_large", "expression": "trade.size >= 50", "severity": "high"},
                    ],
                },
            ],
        },
        "detection": {
            "size_mult": 5.0,
            "abs_min_size": 10,
            "stats_window": 50,
        },
        "outputs": {
            "stdout": True,
            "webhook": {"enabled": False, "url": ""},
        },
        "logging": {
            "level": "INFO",
            "json_format": True,
        },
    }
    config_path = tmp_path / "dxlink-scanner.yaml"
    config_path.write_text(yaml.dump(config_data))

    config = load_config(str(config_path))
    assert config.tastytrade.client_id == "test_id"
    assert config.tastytrade.sandbox is True
    assert len(config.watchlist.tickers) == 4
    assert config.watchlist.tickers[0].symbol == "SPY"
    assert config.watchlist.tickers[1].symbol == "QQQ"
    assert config.watchlist.tickers[2].symbol == "IWM"
    assert config.watchlist.tickers[1].strikes_around_atm == 15
    assert config.watchlist.tickers[1].expiration_filter == "all"
    assert len(config.watchlist.tickers[0].underlying_alert_rules) == 1
    assert config.watchlist.tickers[0].underlying_alert_rules[0].name == "spy_large_print"
    assert config.watchlist.tickers[3].option_type == "equity"
    assert config.watchlist.tickers[3].symbol == "SPX"
    assert len(config.watchlist.tickers[3].underlying_alert_rules) == 1
    assert config.watchlist.default_alert_rules[0].name == "absolute_size"
    assert config.detection.size_mult == 5.0
    assert config.outputs.stdout is True


def test_default_config():
    """Test that default config values are sensible."""
    config = ScannerConfig(
        tastytrade=TastyTradeConfig(
            client_id="test",
            client_secret="test",
            refresh_token="test",
        ),
    )
    assert len(config.watchlist.tickers) == 1
    assert config.watchlist.tickers[0].symbol == "SPY"
    assert config.watchlist.tickers[0].strikes_around_atm == 10
    assert config.watchlist.tickers[0].expiration_filter == "0DTE"
    assert config.watchlist.tickers[0].option_type == "equity"
    assert config.watchlist.tickers[0].underlying_alert_rules == []
    assert config.watchlist.tickers[0].alert_rules == []
    assert config.watchlist.default_alert_rules == []
    assert config.detection.size_mult == 5.0
    assert config.detection.abs_min_size == 10
    assert config.outputs.stdout is True


def test_symbols_property():
    """Test that the symbols property returns underlying symbols."""
    config = WatchlistConfig(
        tickers=[
            TickerConfig(symbol="SPY"),
            TickerConfig(symbol="QQQ"),
            TickerConfig(symbol="IWM"),
        ]
    )
    assert config.symbols == ["SPY", "QQQ", "IWM"]


def test_multiple_underlyings():
    """Test that multiple underlyings are supported."""
    config = WatchlistConfig(
        tickers=[
            TickerConfig(symbol="SPY", strikes_around_atm=5),
            TickerConfig(symbol="QQQ", strikes_around_atm=20, expiration_filter="all"),
            TickerConfig(symbol="IWM", strikes_around_atm=15),
        ]
    )
    assert len(config.tickers) == 3
    assert config.symbols == ["SPY", "QQQ", "IWM"]
    assert config.tickers[0].strikes_around_atm == 5
    assert config.tickers[1].strikes_around_atm == 20
    assert config.tickers[1].expiration_filter == "all"


def test_option_type_futures():
    """Test that option_type 'futures' is accepted."""
    ticker = TickerConfig(symbol="SPX", option_type="futures", strikes_around_atm=10)
    assert ticker.option_type == "futures"


def test_option_type_equity():
    """Test that option_type 'equity' is accepted."""
    ticker = TickerConfig(symbol="SPY", option_type="equity", strikes_around_atm=10)
    assert ticker.option_type == "equity"


def test_underlying_alert_rules():
    """Test that underlying_alert_rules are stored per-ticker."""
    config = WatchlistConfig(
        tickers=[
            TickerConfig(
                symbol="SPY",
                underlying_alert_rules=[
                    CelAlertRule(name="spy_large_print", expression="trade.size >= 100", severity="high"),
                ],
            ),
            TickerConfig(
                symbol="IWM",
                underlying_alert_rules=[
                    CelAlertRule(name="iwm_large_print", expression="trade.size >= 50", severity="medium"),
                ],
            ),
        ]
    )
    assert len(config.tickers[0].underlying_alert_rules) == 1
    assert config.tickers[0].underlying_alert_rules[0].name == "spy_large_print"
    assert config.tickers[1].underlying_alert_rules[0].severity == "medium"


def test_default_alert_rules():
    """Test that default_alert_rules are stored at the watchlist level."""
    rules = [
        CelAlertRule(name="absolute_size", expression="trade.size >= 1000", severity="critical"),
        CelAlertRule(name="anomaly", expression="trade.size > stats.median * 5.0", severity="high"),
    ]
    config = WatchlistConfig(tickers=[TickerConfig(symbol="SPY")], default_alert_rules=rules)
    assert len(config.default_alert_rules) == 2
    assert config.default_alert_rules[0].name == "absolute_size"
    assert config.default_alert_rules[1].severity == "high"
