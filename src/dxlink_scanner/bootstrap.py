"""Bootstrap: fetch option chains using the tastytrade SDK and resolve streamer symbols.

Uses the official SDK's get_option_chain() and get_future_option_chain()
functions instead of bare HTTP calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from tastytrade.instruments import (
    FutureOption,
    Option,
    get_future_option_chain,
    get_option_chain,
)
from tastytrade.market_data import get_market_data_by_type
from tastytrade.session import Session
from tastytrade.utils import today_in_new_york

from dxlink_scanner.models import StrikeInfo

logger = logging.getLogger(__name__)


class InstrumentResolver:
    """Resolves streamer symbols for futures and index options.

    Caches futures metadata (product code → streamer_symbol) until daily
    restart. Equity option streamer symbols are derived directly from
    the chain result (no additional lookup needed).
    """

    def __init__(self) -> None:
        self._futures_cache: dict[str, str] = {}

    @staticmethod
    def resolve_futures_streamer(product_code: str) -> str:
        """Return the DXLink streamer symbol for a futures product.

        For futures like /ES, the streamer symbol requires an
        exchange suffix (e.g. /ES:XCME). This is the root-symbol
        and streamer-exchange-code from the InstrumentResolver.

        Args:
            product_code: Root symbol, with or without leading slash
                (e.g. "ES", "/ES").

        Returns:
            Streamer symbol with exchange suffix (e.g. "/ES:XCME").
        """
        # Strip leading slash — product_code may be "/ES" or "ES"
        clean_code = product_code.lstrip("/")
        exchange_map = {
            "ES": "XCME",
            "NQ": "XCME",
            "YM": "XBOT",
            "CL": "NYMEX",
            "GC": "COMEX",
            "6E": "ICE",
        }
        exchange = exchange_map.get(clean_code, "XCME")
        return f"/{clean_code}:{exchange}"

    @staticmethod
    def resolve_index_option_streamer(root_symbol: str) -> str:
        """Return the DXLink streamer symbol for an index option.

        Index options (SPX, NDX, RUT) do NOT require an exchange suffix.
        """
        return root_symbol


@dataclass
class UnderlyingInfo:
    """Underlying symbol information.

    Attributes:
        symbol: Underlying symbol (e.g. "SPY").
        streamer_symbol: DXLink streamer symbol for the underlying.
        price: Current price.
        bid: Current bid.
        ask: Current ask.
    """

    symbol: str
    streamer_symbol: str
    price: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None


class ChainLoader:
    """Loads option chains using the tastytrade SDK.

    Supports both equity options (SPY, QQQ, IWM, SPX) and futures options
    (ES, NQ) via the SDK's typed instrument functions.

    Attributes:
        session: Authenticated Tastytrade session.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    async def get_nested_chain(self, symbol: str, option_type: str = "equity") -> dict[date, list[Any]]:
        """Fetch the option chain for a symbol using the SDK.

        Args:
            symbol: Underlying symbol (e.g. "SPY", "SPX", "ES").
            option_type: "equity" for stocks/ETFs/index options,
                "futures" for futures options.

        Returns:
            Dict mapping expiration date to list of Option/FutureOption objects.
        """
        if option_type == "futures":
            logger.info("Fetching futures option chain for %s", symbol)
            return await get_future_option_chain(self.session, symbol)
        logger.info("Fetching equity option chain for %s", symbol)
        return await get_option_chain(self.session, symbol)

    async def get_underlying_price(self, symbol: str, option_type: str = "equity") -> Decimal | None:
        """Fetch the current market price (last/mark) for an underlying.

        Uses the Tastytrade market-data API to retrieve the last trade price.
        Falls back to mark, then mid, then close price if last is unavailable.

        Args:
            symbol: Underlying symbol (e.g. "SPY", "ES").
            option_type: "equity" for stocks/ETFs/indexes,
                "futures" for futures contracts.

        Returns:
            The best available price as Decimal, or None if not found.
        """
        if option_type == "futures":
            market_data_list = await get_market_data_by_type(self.session, futures=[symbol])
        else:
            market_data_list = await get_market_data_by_type(self.session, equities=[symbol])

        if not market_data_list:
            logger.warning("No market data returned for %s", symbol)
            return None

        md = market_data_list[0]
        # Prefer last trade price, fall back to mark, then mid, then close
        for field_name in ("last", "mark", "mid", "close"):
            price = getattr(md, field_name, None)
            if price is not None:
                logger.info("Underlying %s: %s price = %s", symbol, field_name, price)
                return Decimal(str(price))

        logger.warning("No price field available for %s", symbol)
        return None

    @staticmethod
    def parse_chain(
        chains: dict[date, list[Option | FutureOption]],
        expiration_filter: str = "0DTE",
        is_future: bool = False,
    ) -> tuple[UnderlyingInfo, list[StrikeInfo]]:
        """Parse SDK option chain result into typed structures.

        The SDK returns a dict mapping expiration dates to lists of
        Option (equity) or FutureOption (futures) objects. This method
        selects the appropriate expiration(s) based on the filter and
        extracts underlying info from the first option.

        Args:
            chains: Dict from get_option_chain() or get_future_option_chain().
            expiration_filter: "0DTE" to select only today's expiration
                (same-day expiry), "all" for all expirations.
            is_future: When True, treats the chain as futures options.
                For futures (which trade 24/7), if the current time is past
                16:15 ET (equity market close), 0DTE resolves to the next
                trading day since today's daily options have already expired.

        Returns:
            Tuple of (UnderlyingInfo, list of StrikeInfo for the filtered
            expiration). Returns ALL strikes for the expiration — no ATM filtering.
        """
        if not chains:
            return UnderlyingInfo(symbol="", streamer_symbol=""), []

        strikes: list[StrikeInfo] = []
        underlying_info: UnderlyingInfo | None = None

        if expiration_filter == "0DTE":
            today = today_in_new_york()
            # For futures, after 16:15 ET, today's options have expired;
            # 0DTE shifts to the next trading day (futures trade 24/7)
            if is_future:
                now_et = datetime.now(ZoneInfo("US/Eastern"))
                market_close = dt_time(16, 15)
                if now_et.time() >= market_close:
                    today = today + timedelta(days=1)
            # Find the expiration matching today; if not found, use the nearest
            # upcoming expiration
            target_expiry: date | None = None
            for expiry in sorted(chains.keys()):
                if expiry >= today:
                    target_expiry = expiry
                    break
            if target_expiry is None:
                # No future expirations; fall back to the last entry
                target_expiry = max(chains.keys())
            if target_expiry != today:
                logger.warning(
                    "No 0DTE expiration found for today (%s); using nearest: %s",
                    today,
                    target_expiry,
                )
            expiries_to_process: list[date] = [target_expiry]
        else:
            expiries_to_process = list(chains.keys())

        for expiry in expiries_to_process:
            expiry_str = expiry.isoformat()
            for opt in chains[expiry]:
                strike_price = Decimal(str(opt.strike_price))
                streamer_symbol = str(opt.streamer_symbol)
                option_type_str = "call" if opt.option_type == "C" else "put"
                strike = StrikeInfo(
                    symbol=streamer_symbol,
                    strike=strike_price,
                    expiry=expiry_str,
                    option_type=option_type_str,
                )
                strikes.append(strike)

                if underlying_info is None:
                    underlying_info = UnderlyingInfo(
                        symbol=str(opt.underlying_symbol),
                        streamer_symbol=streamer_symbol,
                    )

        if underlying_info is None:
            return UnderlyingInfo(symbol="", streamer_symbol=""), []

        return underlying_info, strikes
