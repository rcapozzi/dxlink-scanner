"""Simple DXFeed debug client for 0DTE option TimeAndSale events.

Usage:
    python -m dxlink_scanner.debug_dxfeed SPY,QQQ,SPX
    python -m dxlink_scanner.debug_dxfeed /ES,/NQ --verbose
    python -m dxlink_scanner.debug_dxfeed SPY,/ES
    python -m dxlink_scanner.debug_dxfeed /ES --dm

Resolves each underlying symbol to its 0DTE (same-day expiration) option
contracts nearest to the ATM strike. Subscribes to two DXFeed event types:

  - **Quote** on each underlying symbol (bid/ask for the underlying itself)
  - **TimeAndSale** on both the underlying and all option symbols

This allows comparing and contrasting the two event types in production:
TimeAndSale (trade prints) and Quote (best bid/ask for the underlying).

The underlying price used for ATM strike selection and alert `underlying_price`
is derived from the Quote mid price (bid + ask) / 2 on the underlying symbol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from tastytrade.dxfeed import Quote, TheoPrice, TimeAndSale
from tastytrade.instruments import Option, get_future_option_chain, get_option_chain
from tastytrade.market_data import get_market_data_by_type
from tastytrade.session import Session as TastyTradeSession
from tastytrade.streamer import DXLinkStreamer
from tastytrade.utils import today_in_new_york

logger = logging.getLogger(__name__)

# Number of strikes to select around ATM for each underlying
STRIKES_AROUND_ATM = 10


def setup_logging(verbose: bool = False, debug_messages: bool = False) -> None:
    """Configure logging.

    By default, the tastytrade library logger is set to ERROR to suppress
    all DEBUG messages. The --verbose flag enables DEBUG on this module's
    logger only. The --debug-messages flag enables DEBUG on the tastytrade
    library logger for raw DXLink WebSocket message diagnostics.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("tastytrade").setLevel(logging.ERROR)
    if verbose:
        logger.setLevel(logging.DEBUG)
    if debug_messages:
        logging.getLogger("tastytrade").setLevel(logging.DEBUG)


def parse_symbols(raw: str) -> list[str]:
    """Split a comma-separated string of symbols into a clean list.

    Symbols with a leading '/' (e.g. /ES) are preserved as-is for the
    futures option chain resolver. All other symbols are uppercased.
    """
    symbols = []
    for s in raw.split(","):
        s = s.strip()
        if not s:
            continue
        if s.startswith("/"):
            symbols.append(s.upper())
        else:
            symbols.append(s.upper())
    if not symbols:
        raise ValueError("No symbols provided. Pass a comma-separated list like 'SPY,QQQ'.")
    return symbols


def is_future_symbol(symbol: str) -> bool:
    """Return True if the symbol is a futures contract (leading '/')."""
    return symbol.startswith("/")


def strip_future_prefix(symbol: str) -> str:
    """Strip the leading '/' from a futures symbol."""
    return symbol[1:] if symbol.startswith("/") else symbol


def get_today_expiry(chain: dict[date, list[Any]], is_future: bool = False) -> date:
    """Find the 0DTE expiration date in the chain.

    For equity options, 0DTE means today's same-day expiration.
    For futures (which trade 24/7), if the current time is past 16:15 ET
    (equity options market close), 0DTE resolves to the next trading day
    since today's daily options have already expired.
    """
    today = today_in_new_york()

    if is_future:
        # Futures trade 24/7; after 16:15 ET, today's options are done
        # and the 0DTE is tomorrow's expiration
        now_et = datetime.now(ZoneInfo("US/Eastern"))
        market_close = dt_time(16, 15)
        if now_et.time() >= market_close:
            today = today + timedelta(days=1)

    for expiry in sorted(chain.keys()):
        if expiry >= today:
            if expiry != today:
                logger.warning(
                    "No 0DTE expiration found for today (%s); using nearest: %s",
                    today,
                    expiry,
                )
            return expiry
    # No future expirations; fall back to the last entry
    if chain:
        last = max(chain.keys())
        logger.warning("No upcoming expiration found; using last: %s", last)
        return last
    return today


async def get_underlying_price_from_chain(
    session: TastyTradeSession,
    symbol: str,
    chain: dict[date, list[Any]],
) -> Decimal | None:
    """Fetch the underlying price for a symbol.

    For futures options, the price is looked up using the ``underlying_symbol``
    from one of the 0DTE ``FutureOption`` objects (e.g. ``ESU6``), which is
    the specific future contract that is currently trading. For equity options,
    the underlying symbol itself (e.g. ``SPY``) is used.

    Falls back through last → mark → mid → close price fields.
    """
    if is_future_symbol(symbol):
        # For futures, extract the underlying_symbol from the 0DTE chain
        target_expiry = get_today_expiry(chain, is_future=True)
        options = chain.get(target_expiry, [])
        # Find the underlying_symbol from the first option in the 0DTE expiry
        underlying_sym: str | None = None
        for opt in options:
            if hasattr(opt, "underlying_symbol") and opt.underlying_symbol:
                underlying_sym = str(opt.underlying_symbol)
                break
        if underlying_sym is None:
            logger.warning("Could not find underlying_symbol in futures chain for %s", symbol)
            return None
        data = await get_market_data_by_type(session, futures=[underlying_sym])
        label = f"future {underlying_sym}"
    else:
        data = await get_market_data_by_type(session, equities=[symbol])
        label = f"equity {symbol}"

    if not data:
        logger.warning("No market data for %s", label)
        return None
    md = data[0]
    for field_name in ("last", "mark", "mid", "close"):
        price = getattr(md, field_name, None)
        if price is not None:
            logger.info("Underlying %s: %s price = %s", label, field_name, price)
            return Decimal(str(price))
    logger.warning("No price field available for %s", label)
    return None


async def fetch_chain(session: TastyTradeSession, symbol: str) -> dict[date, list[Any]]:
    """Fetch the option chain for a symbol (equity or futures)."""
    if is_future_symbol(symbol):
        future_sym = strip_future_prefix(symbol)
        logger.info("Fetching futures option chain for %s", future_sym)
        return await get_future_option_chain(session, future_sym)
    logger.info("Fetching equity option chain for %s", symbol)
    return await get_option_chain(session, symbol)


def filter_0dte_strikes(
    chain: dict[date, list[Option]],
    underlying_price: Decimal,
    count: int,
    is_future: bool = False,
) -> list[str]:
    """Filter chain to 0DTE expiration and select ATM strikes.

    Selects the ``count`` option strikes whose strike price is closest to
    ``underlying_price``, using sorted absolute distance.
    """
    target_expiry = get_today_expiry(chain, is_future=is_future)
    options = chain.get(target_expiry, [])
    # Build (strike, streamer_symbol) tuples, sort by distance from ATM
    strike_syms: list[tuple[Decimal, str]] = []
    for opt in options:
        streamer_symbol = str(opt.streamer_symbol)
        if not streamer_symbol:
            continue
        strike = Decimal(str(opt.strike_price))
        strike_syms.append((strike, streamer_symbol))

    strike_syms.sort(key=lambda pair: abs(pair[0] - underlying_price))
    return [sym for _, sym in strike_syms[:count]]


async def build_0dte_streamer_symbols(
    session: TastyTradeSession,
    underlyings: list[str],
    strikes_around_atm: int = STRIKES_AROUND_ATM,
) -> tuple[list[str], list[str]]:
    """Resolve underlying symbols into 0DTE ATM option streamer symbols.

    For each underlying, fetches the option chain (equity or futures),
    finds today's (or nearest) expiration, gets the underlying's market
    price, and selects the ``strikes_around_atm`` strikes closest to the
    underlying price.

    Returns a tuple of:
      - all_option_symbols: every option streamer symbol (for TimeAndSale)
      - underlying_symbols: underlying streamer symbols (for Quote)

    The underlying price is derived from Quote mid_price (bid+ask)/2 on
    the underlying symbol.
    """
    today = today_in_new_york()
    logger.info("Today's date (NY): %s", today)

    all_symbols: list[str] = []
    underlying_symbols: list[str] = []
    for underlying in underlyings:
        logger.info("Fetching option chain for %s", underlying)
        is_fut = is_future_symbol(underlying)
        chain: dict[date, list[Option]] = await fetch_chain(session, underlying)

        # Determine the underlying streamer symbol for Quote
        options: list[Any] = []
        if is_fut:
            # For futures, extract the underlying_symbol from the 0DTE chain
            target_expiry = get_today_expiry(chain, is_future=True)
            options = chain.get(target_expiry, [])
            underlying_sym: str | None = None
            for opt in options:
                if hasattr(opt, "underlying_symbol") and opt.underlying_symbol:
                    underlying_sym = str(opt.underlying_symbol)
                    break
            if underlying_sym is None:
                logger.warning("Could not find underlying_symbol in futures chain for %s", underlying)
                continue
            underlying_streamer = underlying_sym
        else:
            underlying_streamer = underlying

        underlying_symbols.append(underlying_streamer)
        logger.info("  underlying for Quote: %s", underlying_streamer)

        underlying_price = await get_underlying_price_from_chain(session, underlying, chain)
        if underlying_price is None:
            logger.warning(
                "Could not resolve price for %s; using first %d strikes",
                underlying,
                strikes_around_atm,
            )
            # Fallback: just grab first N from 0DTE expiration
            if not options:
                target_expiry = get_today_expiry(chain, is_future=is_fut)
                options = chain.get(target_expiry, [])
            symbols = [str(opt.streamer_symbol) for opt in options[:strikes_around_atm]]
        else:
            symbols = filter_0dte_strikes(chain, underlying_price, strikes_around_atm, is_future=is_fut)

        all_symbols.extend(symbols)
        logger.info(
            "  %s: %d 0DTE ATM symbols (price=%s)",
            underlying,
            len(symbols),
            underlying_price,
        )

    return all_symbols, underlying_symbols


async def _run(underlyings: list[str]) -> None:
    """Connect to DXLink, subscribe to Quote + TimeAndSale, print events."""
    session = TastyTradeSession(
        provider_secret=os.environ["TASTY_CLIENT_SECRET"],
        refresh_token=os.environ["TASTY_REFRESH_TOKEN"],
        is_test=os.environ.get("TASTY_SANDBOX", "false").lower() == "true",
    )
    await session.refresh(force=True)
    logger.info("Authenticated with Tastytrade (sandbox=%s)", session.is_test)

    symbols, underlying_symbols = await build_0dte_streamer_symbols(session, underlyings)
    if not symbols:
        logger.error(
            "No 0DTE streamer symbols resolved for %s. "
            "Check that it is a trading day and options exist for today's expiry.",
            ", ".join(underlyings),
        )
        sys.exit(1)
    logger.info("Subscribing to %d option symbols for TimeAndSale", len(symbols))
    logger.info("  %s", ", ".join(symbols))
    logger.info("Subscribing to %d underlying symbols for Quote", len(underlying_symbols))
    logger.info("  %s", ", ".join(underlying_symbols))

    async with DXLinkStreamer(session) as streamer:
        # Quote on the underlying (bid/ask for the underlying itself)
        await streamer.subscribe(Quote, underlying_symbols)
        # TimeAndSale on both the underlying AND all option symbols
        await streamer.subscribe(TimeAndSale, underlying_symbols + symbols)
        # TheoPrice on all option symbols (for delta & Greeks)
        await streamer.subscribe(TheoPrice, symbols)
        logger.info("Listening for Quote + TimeAndSale + TheoPrice events... (Ctrl-C to stop)")

        # Consume all event streams concurrently; use return_exceptions=True
        # to avoid ExceptionGroup when the streamer closes or one stream errors.
        quote_task = asyncio.create_task(_consume_quote(streamer))
        tas_task = asyncio.create_task(_consume_timeandsale(streamer))
        theo_task = asyncio.create_task(_consume_theoprice(streamer))
        results = await asyncio.gather(quote_task, tas_task, theo_task, return_exceptions=True)
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error("Stream consumer %d exited with error: %s", i, r)


async def _consume_quote(streamer: DXLinkStreamer) -> None:
    """Listen for Quote events and log them.

    Quote events provide best bid/ask prices for the underlying symbols.
    The mid_price (bid+ask)/2 serves as the underlying_price for alerts.
    """
    async for q in streamer.listen(Quote):
        logger.info(
            "Quote    symbol=%s  bid=%s  ask=%s  time=%s",
            q.event_symbol,
            q.bid_price,
            q.ask_price,
            q.event_time,
        )


async def _consume_timeandsale(streamer: DXLinkStreamer) -> None:
    """Listen for TimeAndSale events and log them."""
    from tastytrade.dxfeed import Quote  # noqa: F401 — reference for clarity

    async for tas in streamer.listen(TimeAndSale):
        logger.info(
            "TimeAndSale  symbol=%s  price=%0.2f  size=%s  time=%s  bid=%0.2f  ask=%0.2f  flags=%s",
            tas.event_symbol,
            tas.price,
            tas.size,
            tas.time,
            tas.bid_price,
            tas.ask_price,
            tas.event_flags,
        )


async def _consume_theoprice(streamer: DXLinkStreamer) -> None:
    """Listen for TheoPrice events and log them."""
    async for tp in streamer.listen(TheoPrice):
        logger.info(
            "TheoPrice  symbol=%s  theo=%0.2f  underlying=%0.2f  "
            "delta=%0.4f  gamma=%0.4f  div=%0.4f  int=%0.4f  time=%s",
            tp.event_symbol,
            tp.price,
            tp.underlying_price,
            tp.delta,
            tp.gamma,
            tp.dividend,
            tp.interest,
            tp.event_time,
        )


def main() -> None:
    """Entry point: parse CLI args, authenticate, resolve symbols, and stream."""
    if len(sys.argv) < 2:
        print("Usage: python -m dxlink_scanner.debug_dxfeed <symbols>")
        print("Example: python -m dxlink_scanner.debug_dxfeed SPY,QQQ,SPX")
        print("         python -m dxlink_scanner.debug_dxfeed /ES,/NQ")
        sys.exit(1)

    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    debug_messages = "--debug-messages" in sys.argv or "--dm" in sys.argv
    setup_logging(verbose=verbose, debug_messages=debug_messages)

    # Filter out flags, take the first non-flag argument as symbols
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("Usage: python -m dxlink_scanner.debug_dxfeed <symbols>")
        print("Example: python -m dxlink_scanner.debug_dxfeed SPY,QQQ,SPX")
        print("         python -m dxlink_scanner.debug_dxfeed /ES,/NQ")
        sys.exit(1)

    underlyings = parse_symbols(args[0])
    load_dotenv()

    try:
        asyncio.run(_run(underlyings))
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down.")
    except KeyError as e:
        missing = e.args[0]
        logger.error(
            "Missing environment variable %s. Copy .env.example to .env and fill in your Tastytrade credentials.",
            missing,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
