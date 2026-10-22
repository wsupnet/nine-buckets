"""Price and FX fetching. Yahoo Finance for stocks/metals/FX, CoinGecko for crypto.

Both are keyless public endpoints. Every fetch returns (price, currency) or raises
PriceError with a message suitable for showing in the UI.
"""

import logging
import time

import httpx

from .db import connect, now_iso

log = logging.getLogger("networth.prices")

# CoinGecko's keyless endpoint allows only a handful of calls a minute. Refreshing
# everything at once goes over that comfortably, so a 429 is a normal thing to meet
# rather than an error — wait it out instead of reporting the holding as failed.
RATE_LIMIT_ATTEMPTS = 3
RATE_LIMIT_MAX_WAIT = 20.0

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
COINGECKO_PRICE = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_SEARCH = "https://api.coingecko.com/api/v3/search"
COINGECKO_LIST = "https://api.coingecko.com/api/v3/coins/list"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"

# Yahoo rejects requests without a browser-ish User-Agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

BUILTIN_COIN_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "DOT": "polkadot",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "MATIC": "matic-network",
    "LTC": "litecoin",
    "XMR": "monero",
}

GOLD_API = "https://api.gold-api.com/price/{symbol}"

# Spot, not futures. Gold futures trade ~1.3% above spot and that gap moves with
# contract roll, which would quietly overstate a physical stack.
#
# `quote_unit` matters: precious metals are quoted per troy ounce, but copper is
# quoted per POUND. Treating copper as troy ounces overstates it by 14.58x.
METALS = {
    "XAU": {"name": "Gold", "quote_unit": "toz"},
    "XAG": {"name": "Silver", "quote_unit": "toz"},
    "XPT": {"name": "Platinum", "quote_unit": "toz"},
    "XPD": {"name": "Palladium", "quote_unit": "toz"},
    "HG": {"name": "Copper", "quote_unit": "lb"},
}

# Legacy tickers and friendly names that should resolve to a metal code.
METAL_ALIASES = {
    "GOLD": "XAU", "GC=F": "XAU", "XAUUSD": "XAU",
    "SILVER": "XAG", "SI=F": "XAG", "XAGUSD": "XAG",
    "PLATINUM": "XPT", "PL=F": "XPT",
    "PALLADIUM": "XPD", "PA=F": "XPD",
    "COPPER": "HG", "HG=F": "HG", "XCU": "HG",
}

TROY_OZ_PER_LB = 14.583333333333334


def resolve_metal(code: str) -> str:
    c = (code or "").strip().upper()
    c = METAL_ALIASES.get(c, c)
    if c not in METALS:
        raise PriceError(
            f"'{code}' is not a supported metal. Use one of: "
            + ", ".join(f"{k} ({v['name']})" for k, v in METALS.items())
        )
    return c


def _coingecko_metal_crossrate(metal: str):
    """Fallback for gold and silver: CoinGecko quotes bitcoin in XAU/XAG, so
    (btc in usd) / (btc in xau) gives USD per troy ounce."""
    vs = {"XAU": "xau", "XAG": "xag"}.get(metal)
    if not vs:
        raise PriceError(f"No fallback spot source for {metal}")
    try:
        with httpx.Client(timeout=10.0, headers=HEADERS) as c:
            r = _get(c, COINGECKO_PRICE,
                     params={"ids": "bitcoin", "vs_currencies": f"usd,{vs}"})
            r.raise_for_status()
            d = r.json()["bitcoin"]
        if not d.get(vs):
            raise PriceError(f"CoinGecko returned no {vs} rate")
        return float(d["usd"]) / float(d[vs])
    except httpx.HTTPError as e:
        raise PriceError(f"CoinGecko fallback failed ({type(e).__name__})") from e
    except (KeyError, TypeError, ZeroDivisionError) as e:
        raise PriceError("CoinGecko fallback returned unusable data") from e


def fetch_metal_spot(code: str):
    """Spot price for a metal, always returned as USD per TROY OUNCE.

    Copper is quoted per pound upstream and is converted here, so every caller can
    assume troy ounces.
    """
    metal = resolve_metal(code)
    spec = METALS[metal]
    price = None

    try:
        with httpx.Client(timeout=10.0, headers=HEADERS, follow_redirects=True) as c:
            r = _get(c, GOLD_API.format(symbol=metal))
            r.raise_for_status()
            data = r.json()
        price = float(data.get("price") or 0) or None
    except (httpx.HTTPError, ValueError, TypeError):
        price = None

    if price is None:
        # Only gold and silver have a fallback; the rest surface the failure.
        price = _coingecko_metal_crossrate(metal)

    if spec["quote_unit"] == "lb":
        price = price / TROY_OZ_PER_LB

    return price, "USD"


class PriceError(Exception):
    pass


def _get(client, url, **kw):
    """GET, waiting out a rate limit rather than treating it as a dead symbol.

    Honours Retry-After when the server sends one, and falls back to a short
    exponential wait when it doesn't.
    """
    for attempt in range(RATE_LIMIT_ATTEMPTS):
        r = client.get(url, **kw)
        if r.status_code != 429:
            return r
        if attempt == RATE_LIMIT_ATTEMPTS - 1:
            break
        try:
            wait = float(r.headers.get("Retry-After", ""))
        except ValueError:
            wait = 0.0
        wait = min(max(wait, 2.0 ** attempt), RATE_LIMIT_MAX_WAIT)
        log.info("rate limited, waiting %.1fs before retrying %s", wait, url)
        time.sleep(wait)
    raise PriceError("the price feed is rate limiting us — try again in a minute")


def fetch_yahoo(symbol: str):
    try:
        with httpx.Client(timeout=10.0, headers=HEADERS, follow_redirects=True) as c:
            r = _get(c, YAHOO_CHART.format(symbol=symbol))
            if r.status_code == 404:
                raise PriceError(f"Yahoo has no symbol '{symbol}' — check the ticker")
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
    except httpx.HTTPError as e:
        raise PriceError(f"Yahoo request failed for {symbol} ({type(e).__name__})") from e
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise PriceError(f"No Yahoo data for '{symbol}' — check the ticker") from e

    price = meta.get("regularMarketPrice")
    if price is None:
        raise PriceError(f"Yahoo returned no price for '{symbol}'")
    return float(price), meta.get("currency", "USD")


def resolve_coin_id(ticker: str) -> str:
    t = ticker.upper().strip()
    if t in BUILTIN_COIN_IDS:
        return BUILTIN_COIN_IDS[t]

    conn = connect()
    try:
        row = conn.execute(
            "SELECT coin_id FROM coin_ids WHERE ticker = ?", (t,)
        ).fetchone()
    finally:
        conn.close()
    if row:
        return row["coin_id"]

    # No connection is held across the network call, and the cache write below opens
    # its own. SQLite allows a single writer, so holding one here would deadlock any
    # caller that is itself mid-write — which is exactly what an import is.
    try:
        with httpx.Client(timeout=10.0, headers=HEADERS) as c:
            r = _get(c, COINGECKO_SEARCH, params={"query": t})
            r.raise_for_status()
            coins = r.json().get("coins", [])
    except httpx.HTTPError as e:
        raise PriceError(f"CoinGecko search failed for {t}: {e}") from e
    except ValueError as e:
        # Rate limiting answers with an HTML page rather than JSON.
        raise PriceError(f"CoinGecko sent no usable data for {t}") from e

    match = next((c for c in coins if c.get("symbol", "").upper() == t), None)
    coin_id = match["id"] if match else _coin_id_from_full_list(t)
    if not coin_id:
        raise PriceError(f"No CoinGecko coin found for '{ticker}'")

    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO coin_ids (ticker, coin_id) VALUES (?,?)",
            (t, coin_id),
        )
        conn.commit()
    finally:
        conn.close()
    return coin_id


def _coin_id_from_full_list(t: str):
    """Find a coin id by exact symbol, when /search can't.

    /search ranks by fuzzy *name* relevance, so a short ticker that happens to sit
    inside popular names loses to them and never appears in the results at all —
    a ticker spelled like a fragment of other coins' names gets buried. The full
    coin list has no ranking to lose to, so match against that instead. Only
    reached when search has already failed, and the answer is cached afterwards.
    """
    try:
        with httpx.Client(timeout=30.0, headers=HEADERS) as c:
            r = _get(c, COINGECKO_LIST)
            r.raise_for_status()
            everything = r.json()
    except (httpx.HTTPError, ValueError):
        return None

    matches = [c["id"] for c in everything
               if (c.get("symbol") or "").upper() == t and c.get("id")]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # A symbol can be claimed by dozens of coins, most of them dust. The one the
    # holder means is essentially always the largest.
    try:
        with httpx.Client(timeout=20.0, headers=HEADERS) as c:
            r = _get(c, COINGECKO_MARKETS,
                     params={"vs_currency": "usd", "ids": ",".join(matches[:250]),
                             "order": "market_cap_desc", "per_page": 1, "page": 1})
            r.raise_for_status()
            rows = r.json()
        if rows:
            log.info("%s matched %d coins; using the largest (%s)",
                     t, len(matches), rows[0]["id"])
            return rows[0]["id"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        pass
    return matches[0]


def fetch_crypto(ticker: str):
    coin_id = resolve_coin_id(ticker)
    try:
        with httpx.Client(timeout=10.0, headers=HEADERS) as c:
            r = _get(c, COINGECKO_PRICE, params={"ids": coin_id, "vs_currencies": "usd"})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        raise PriceError(f"CoinGecko request failed for {ticker}: {e}") from e
    except ValueError as e:
        raise PriceError(f"CoinGecko sent no usable data for {ticker}") from e

    price = data.get(coin_id, {}).get("usd")
    if price is None:
        raise PriceError(f"CoinGecko returned no price for '{ticker}'")
    return float(price), "USD"


def fetch_crypto_many(tickers):
    """Price every crypto ticker in as few calls as possible.

    Returns ({TICKER: (price, currency)}, {TICKER: reason_it_failed}).

    `/simple/price` accepts a comma-separated list of ids, so a whole portfolio is
    one request. Fetching per holding instead — which is what refreshing in a loop
    does — spends a request on every row, blows through CoinGecko's keyless
    per-minute allowance partway down the list, and reports the remainder as if the
    tickers themselves were bad.
    """
    prices_out, errors = {}, {}

    ids = {}
    for t in {(t or "").strip().upper() for t in tickers if (t or "").strip()}:
        try:
            ids[t] = resolve_coin_id(t)
        except PriceError as exc:
            errors[t] = str(exc)
    if not ids:
        return prices_out, errors

    # Chunked so the query string stays a sane length on a large portfolio.
    items = sorted(ids.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        try:
            with httpx.Client(timeout=20.0, headers=HEADERS) as c:
                r = _get(c, COINGECKO_PRICE,
                         params={"ids": ",".join(cid for _, cid in chunk),
                                 "vs_currencies": "usd"})
                r.raise_for_status()
                data = r.json()
        except PriceError as exc:
            for t, _ in chunk:
                errors[t] = str(exc)
            continue
        except httpx.HTTPError as exc:
            for t, _ in chunk:
                errors[t] = f"CoinGecko request failed ({type(exc).__name__})"
            continue
        except ValueError:
            for t, _ in chunk:
                errors[t] = "CoinGecko sent no usable data"
            continue

        for t, cid in chunk:
            price = (data.get(cid) or {}).get("usd")
            if price is None:
                errors[t] = f"CoinGecko returned no price for '{t}'"
            else:
                prices_out[t] = (float(price), "USD")

    return prices_out, errors


def fetch_price(ticker: str, asset_class: str):
    ticker = (ticker or "").strip()
    if not ticker:
        raise PriceError("No ticker set on this item")
    if asset_class == "crypto":
        return fetch_crypto(ticker)
    if asset_class == "metal":
        return fetch_metal_spot(ticker)
    return fetch_yahoo(ticker)


def refresh_fx(currency: str) -> float:
    """Update and return USD per 1 unit of `currency`.

    Quotes USD{ccy}=X and inverts: Yahoo rounds to 4dp, so the direct {ccy}USD=X
    pair gives only 2 significant figures for JPY-scale currencies.
    """
    currency = currency.upper()
    if currency == "USD":
        return 1.0
    try:
        price = 1.0 / fetch_yahoo(f"USD{currency}=X")[0]
    except (PriceError, ZeroDivisionError):
        price, _ = fetch_yahoo(f"{currency}USD=X")
    conn = connect()
    conn.execute(
        "INSERT INTO fx_rates (currency, usd_per_unit, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(currency) DO UPDATE SET usd_per_unit=excluded.usd_per_unit,"
        " updated_at=excluded.updated_at",
        (currency, price, now_iso()),
    )
    conn.commit()
    conn.close()
    return price
