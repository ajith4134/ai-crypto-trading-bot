"""F51 cont. 51 — Market data helpers for the dynamic scanner universe.

Replaces hardcoded anchor symbol lists with two live sources of truth:

  1. CoinGecko market-cap data — picks the top-N anchor candidates by mcap
     so the anchor set self-updates as the market evolves (no maintenance).
  2. Binance futures_exchange_info `onboardDate` — gives a listing-age filter
     to exclude freshly listed perps that haven't seasoned past their
     price-discovery phase.

Both sources are cached aggressively (Redis TTL 24h / 7d respectively) so
the per-scan cost stays at ~1 CoinGecko HTTP call + 1 Binance API call.

No hardcoded symbol lists anywhere in this module.
"""
from __future__ import annotations

import json
import time
from typing import Iterable

import requests
import structlog

import redis_client

log = structlog.get_logger()

# --- Redis cache keys ---
_MCAP_CACHE_KEY        = "scanner:market_caps_json"
_MCAP_CACHE_TTL_SEC    = 24 * 3600          # refresh once per day
_LISTING_AGE_CACHE_KEY = "scanner:listing_ages_json"
_LISTING_AGE_TTL_SEC   = 7 * 24 * 3600      # listing dates never change → 7d

# --- CoinGecko ---
_CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
_CG_PER_PAGE    = 250          # CoinGecko max per page
_CG_TIMEOUT_SEC = 10


# cont. 51: stablecoins should never appear in the anchor universe even
# though CoinGecko ranks them in the top-20 by market cap. Trading
# stablecoin/USDT perps has near-zero variance — no edge, just fees.
# This set is the BASE token symbol (upper-case), not the perp pair.
_STABLECOIN_BASE_SYMBOLS = {
    "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "USDE",
    "PYUSD", "USDD", "GUSD", "USDS", "FRAX", "LUSD", "RLUSD",
    # Wrapped/pegged BTC/ETH variants — track underlying so are redundant.
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBETH",
}


def _cg_to_binance_symbol(cg_symbol: str, binance_symbols: set[str]) -> str | None:
    """Map a CoinGecko symbol (lowercase, e.g. 'btc') to the corresponding
    Binance USDT-M perp symbol (uppercase, e.g. 'BTCUSDT'). Returns None
    when the asset isn't listed as a USDT-M perp on Binance OR when the
    asset is a stablecoin / pegged wrapper (no tradable variance).

    A naive `<symbol>.upper() + "USDT"` would mostly work but breaks for
    a handful of name collisions (e.g. 'ada' on CoinGecko has multiple
    matches). We restrict to symbols that exist in the live Binance set,
    which guarantees we never invent a non-tradeable pair.
    """
    base = cg_symbol.upper()
    if base in _STABLECOIN_BASE_SYMBOLS:
        return None
    candidate = f"{base}USDT"
    return candidate if candidate in binance_symbols else None


def fetch_top_marketcaps(binance_symbols: Iterable[str],
                         top_n: int = 250,
                         force_refresh: bool = False) -> dict[str, float]:
    """Return `{binance_symbol: market_cap_usd}` for the intersection of
    the live Binance USDT-M perp set and the global CoinGecko top-N by
    market cap.

    The result is cached in Redis with a 24h TTL so scanner_loop's 8h
    cadence makes at most one CoinGecko call per day.

    No hardcoded symbol lists — purely intersection logic.
    """
    r = redis_client.get()
    binance_set = set(binance_symbols)

    if not force_refresh:
        try:
            cached = r.get(_MCAP_CACHE_KEY)
            if cached:
                data = json.loads(cached)
                if isinstance(data, dict) and data:
                    # Re-filter against the (possibly updated) binance set
                    return {k: float(v) for k, v in data.items() if k in binance_set}
        except Exception as exc:
            log.debug("mcap_cache_read_failed", error=str(exc)[:120])

    out: dict[str, float] = {}
    # CoinGecko returns ≤ 250 per page; one page gives the top 250 — more than
    # enough for a 30-anchor + 30-mover universe.
    try:
        params = {
            "vs_currency":  "usd",
            "order":        "market_cap_desc",
            "per_page":     _CG_PER_PAGE,
            "page":         1,
            "sparkline":    "false",
        }
        resp = requests.get(_CG_MARKETS_URL, params=params, timeout=_CG_TIMEOUT_SEC)
        resp.raise_for_status()
        rows = resp.json()
        for row in rows:
            cg_sym = (row.get("symbol") or "").lower()
            mcap   = float(row.get("market_cap") or 0)
            if not cg_sym or mcap <= 0:
                continue
            binance_sym = _cg_to_binance_symbol(cg_sym, binance_set)
            if binance_sym is not None:
                # CoinGecko sometimes returns multiple coins with the same
                # 3-letter symbol; the API returns market_cap_desc so the
                # FIRST hit is the highest-mcap one — keep it.
                out.setdefault(binance_sym, mcap)
            if len(out) >= top_n:
                break
        log.info("mcap_fetched", count=len(out), top_n=top_n)
        try:
            r.setex(_MCAP_CACHE_KEY, _MCAP_CACHE_TTL_SEC, json.dumps(out))
        except Exception as exc:
            log.debug("mcap_cache_write_failed", error=str(exc)[:120])
    except Exception as exc:
        log.warning("mcap_fetch_failed", error=str(exc)[:200])
        # On failure return an empty dict — caller falls back to other
        # ranking signals (volume, etc).
        return {}

    return out


def fetch_listing_ages(exchange_client,
                       force_refresh: bool = False) -> dict[str, int]:
    """Return `{binance_symbol: days_since_listing}` from Binance futures
    `exchangeInfo`. The `onboardDate` field is the perp launch timestamp
    in milliseconds.

    Cached for 7 days — listing dates do not change for existing symbols
    (only new symbols get added, and the cache will pick those up at the
    next refresh).
    """
    r = redis_client.get()

    if not force_refresh:
        try:
            cached = r.get(_LISTING_AGE_CACHE_KEY)
            if cached:
                data = json.loads(cached)
                if isinstance(data, dict) and data:
                    # Convert from seconds-stored to current days
                    now_ms = int(time.time() * 1000)
                    return {
                        sym: max(0, int((now_ms - int(onboard_ms)) / 86_400_000))
                        for sym, onboard_ms in data.items()
                    }
        except Exception as exc:
            log.debug("listing_age_cache_read_failed", error=str(exc)[:120])

    out_raw: dict[str, int] = {}     # symbol → onboardDate ms (what we cache)
    try:
        # Direct call to the underlying python-binance client; the wrapped
        # `get_all_usdt_futures_symbols` only returns names, not metadata.
        info = exchange_client._client.futures_exchange_info()
        for sym_info in info.get("symbols", []):
            symbol = sym_info.get("symbol")
            onboard = sym_info.get("onboardDate")
            if symbol and onboard and str(symbol).endswith("USDT"):
                out_raw[symbol] = int(onboard)
        log.info("listing_ages_fetched", count=len(out_raw))
        try:
            r.setex(_LISTING_AGE_CACHE_KEY, _LISTING_AGE_TTL_SEC, json.dumps(out_raw))
        except Exception as exc:
            log.debug("listing_age_cache_write_failed", error=str(exc)[:120])
    except Exception as exc:
        log.warning("listing_ages_fetch_failed", error=str(exc)[:200])
        return {}

    now_ms = int(time.time() * 1000)
    return {
        sym: max(0, int((now_ms - onboard_ms) / 86_400_000))
        for sym, onboard_ms in out_raw.items()
    }


def get_anchor_universe(binance_symbols: Iterable[str],
                        top_n: int = 30) -> list[str]:
    """Top-N USDT-M perps by market cap, intersected with the live
    Binance perp set. Replaces the hardcoded ["BTCUSDT", ...] list.

    Returns symbols in DESCENDING market-cap order so the caller can
    truncate to any size ≤ top_n.

    Returns [] on total failure (CoinGecko unreachable AND no cache);
    caller must handle the empty-anchor case (current scanner logic
    falls back to the default seed list, but we'll remove that seed
    behaviour).
    """
    mcaps = fetch_top_marketcaps(binance_symbols, top_n=top_n)
    if not mcaps:
        return []
    return [
        sym for sym, _mc in sorted(mcaps.items(),
                                   key=lambda kv: kv[1],
                                   reverse=True)
    ][:top_n]
