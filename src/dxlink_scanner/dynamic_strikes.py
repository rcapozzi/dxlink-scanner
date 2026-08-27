"""Dynamic strike management for intraday option chain updates.

Rescans the option chain every 60 minutes and returns added/removed
streamer symbols so the scanner can subscribe/unsubscribe live without
restart.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from tastytrade.session import Session as TastyTradeSession

from dxlink_scanner.bootstrap import ChainLoader
from dxlink_scanner.config import TickerConfig, WatchlistConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StrikeDelta:
    """Result of a chain rescan: symbols to add and remove."""

    added: list[str]
    removed: list[str]
    timestamp: dt.datetime


class DynamicStrikeManager:
    """Manages dynamic strike additions/removals during a market day.

    Polls the Tastytrade API every ``rescan_interval_min`` minutes to
    re-fetch the option chain for each configured ticker. Compares the
    new strike list against the current watchlist and returns a
    :class:`StrikeDelta` describing what changed.

    Typical usage::

        mgr = DynamicStrikeManager(session, config.watchlist)
        delta = await mgr.rescan()
        if delta.added:
            await streamer.subscribe(TimeAndSale, delta.added)
        if delta.removed:
            await streamer.unsubscribe(TimeAndSale, delta.removed)
    """

    def __init__(
        self,
        session: TastyTradeSession,
        watchlist: WatchlistConfig,
        rescan_interval_min: int = 60,
    ) -> None:
        self._loader = ChainLoader(session=session)
        self._watchlist = watchlist
        self._rescan_interval = dt.timedelta(minutes=rescan_interval_min)
        self._last_scan: dt.datetime | None = None
        # Track current active symbols per ticker root
        self._active: dict[str, set[str]] = {}

    async def _collect_symbols(
        self,
        ticker: TickerConfig,
    ) -> tuple[str, list[str], str | None]:
        """Fetch option chain for a ticker and return (root, symbols, underlying)."""
        chains = await self._loader.get_nested_chain(ticker.symbol, ticker.option_type)
        underlying_info, strikes = self._loader.parse_chain(
            chains,
            ticker.expiration_filter,
            is_future=ticker.option_type == "futures",
        )

        price_symbol = underlying_info.symbol or ticker.symbol
        # Use all 0DTE strikes — no ATM filtering
        symbols = [s.symbol for s in strikes]
        return ticker.symbol, symbols, price_symbol

    async def initial_scan(self) -> StrikeDelta:
        """Perform the first chain scan and populate the active set."""
        added: list[str] = []
        self._last_scan = dt.datetime.now(dt.UTC)

        for ticker in self._watchlist.tickers:
            root, symbols, _ = await self._collect_symbols(ticker)
            self._active[root] = set(symbols)
            added.extend(symbols)

        logger.info("Initial scan: %d symbols across %d tickers", len(added), len(self._watchlist.tickers))
        return StrikeDelta(added=added, removed=[], timestamp=self._last_scan)

    def should_rescan(self) -> bool:
        """Check if enough time has elapsed since the last scan."""
        if self._last_scan is None:
            return True
        return (dt.datetime.now(dt.UTC) - self._last_scan) >= self._rescan_interval

    async def rescan(self) -> StrikeDelta | None:
        """Rescan all tickers and return delta if changes found.

        Returns ``None`` if the rescan interval has not elapsed.
        """
        if not self.should_rescan():
            return None

        self._last_scan = dt.datetime.now(dt.UTC)
        all_added: list[str] = []
        all_removed: list[str] = []

        for ticker in self._watchlist.tickers:
            _, symbols, _ = await self._collect_symbols(ticker)
            new_set = set(symbols)
            old_set = self._active.get(ticker.symbol, set())

            if new_set != old_set:
                added = sorted(new_set - old_set)
                removed = sorted(old_set - new_set)
                if added:
                    logger.info("Adding %d new symbols for %s", len(added), ticker.symbol)
                if removed:
                    logger.info("Removing %d stale symbols for %s", len(removed), ticker.symbol)
                all_added.extend(added)
                all_removed.extend(removed)
                self._active[ticker.symbol] = new_set

        if all_added or all_removed:
            return StrikeDelta(added=all_added, removed=all_removed, timestamp=self._last_scan)

        logger.debug("Rescan complete: no changes")
        return StrikeDelta(added=[], removed=[], timestamp=self._last_scan)

    @property
    def all_symbols(self) -> list[str]:
        """Return all currently active symbols across all tickers."""
        result: list[str] = []
        for syms in self._active.values():
            result.extend(sorted(syms))
        return result
