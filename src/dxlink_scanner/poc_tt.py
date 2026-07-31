import asyncio
import json
import logging
import os
import sys
from pprint import pprint

from dotenv import load_dotenv
from tastytrade import API_URL
from tastytrade.instruments import get_future_option_chain
from tastytrade.market_data import get_market_data
from tastytrade.order import InstrumentType
from tastytrade.session import Session as TastyTradeSession

logger = logging.getLogger(__name__)
load_dotenv()


def stringify_keys(obj):
    if isinstance(obj, dict):
        return {str(k): stringify_keys(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [stringify_keys(item) for item in obj]
    else:
        return obj

async def resolve_futures_root_symbol(session, symbol):
    """DXLink valid symbols are /ES:XCME and /ESU6:XCME"""
    data = await session._get(API_URL + f"/instruments/futures/?product-code[]={symbol.replace('/', '')}")
    with open("data/product.json", "w", encoding="utf-8") as f:
        json.dump(stringify_keys(data), f, indent=2)

    return [
        {
            "streamer-symbol": item.get("streamer-symbol"),
            "streamer-exchange-code": item.get("streamer-exchange-code"),
            "streamer-root-symbol": symbol +":" + item.get("streamer-exchange-code"),
            "symbol": item["symbol"],
            "root-symbol": item.get("root-symbol"),
        }
        for item in data["items"]
        if item.get("active") and item.get("active-month")
    ]

async def resolve_futures(session, symbol):
    data = await resolve_futures_root_symbol(session, symbol)
    print(json.dumps(data, indent=2))

    data = await get_future_option_chain(session, symbol)
    key, value = min(data.items(), key=lambda item: item[0])
    print(key)


    return
    with open("data/chain.py", "w", encoding="utf-8") as f:
        pprint(data, stream=f, sort_dicts=True)
    #with open("data/chain.yaml", "w", encoding="utf-8") as f:
    #    yaml.dump(data, f, sort_keys=True)

    #print(json.dumps(data,indent=2))
    return



async def resolve_equity(session, symbol):
    data = await get_market_data(session, symbol, InstrumentType.FUTURE)
    print(json.dumps(data, indent=2))


async def resolve_equity_options(session, symbol):
    url = f"{API_URL}/option_chains/{symbol}/nested"
    print("Hitting " + url)
    data = await session._get(url)
    print(data)
    if data is None:
        print("Got none")
    else:
        print(json.dumps(data, indent=2))


async def _run():
    session = TastyTradeSession(
        provider_secret=os.environ["TASTY_CLIENT_SECRET"],
        refresh_token=os.environ["TASTY_REFRESH_TOKEN"],
        is_test=os.environ.get("TASTY_SANDBOX", "false").lower() == "true",
    )
    await session.refresh(force=True)
    await resolve_futures(session, "/ES")
    return
    await resolve_equity(session, "SPY")
    await resolve_equity_options(session, "SPY")

    return


def main():
    try:
        asyncio.run(_run())
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
