"""cont. 62c — Category-aware universe builder for the 100-pair scanner.

Adds a category bucket layer on top of the existing composite scoring:

  1. Fetch each CoinGecko category's top-100 coins ONCE per 24 h.
  2. Intersect with the live Binance USDT-M perp set.
  3. Assign each Binance perp to the FIRST matching bucket in the configured
     order (anchors first → residual last) — deterministic, no double-count.
  4. Within each bucket, the caller picks top-10 by composite score.
  5. Underfilled buckets get their deficit filled from the residual bucket
     by market cap, so the union still hits the 100-pair target.

No hardcoded symbol lists. The bucket order and the CoinGecko slug per
bucket are read from Redis (`scanner:category_slugs`, JSON) with the
defaults below baked in. Owner can swap any bucket without redeploy.

Free-tier budget: 10 categories × 1 call / 24 h = 10 calls/day ≈ 300/month
vs CoinGecko Demo's 10 000/month cap. Comfortable margin even if cadence
is later cut to 6 h.
"""
from __future__ import annotations

import json
import time
from typing import Iterable

import requests
import structlog

import redis_client

log = structlog.get_logger()

_CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
_CG_PER_PAGE    = 100
_CG_TIMEOUT_SEC = 12

# Cache keys. Per-bucket TTL so SUCCESSFUL buckets persist across scans
# even when others 429'd; the next scan only retries the empty ones.
_BUCKET_CACHE_KEY_PREFIX = "scanner:category_bucket:"   # + bucket_name
_BUCKET_CACHE_TTL_SEC    = 24 * 3600

# Stale-state marker — set by ops to force a full refetch on the next scan.
_FORCE_REFRESH_FLAG      = "scanner:category_cache_stale"

# Default bucket configuration. Order is PRECEDENCE-CRITICAL: a coin is
# assigned to the FIRST bucket whose CoinGecko slug appears in its category
# list, so put broader / higher-priority buckets first. Each entry is
#   (bucket_name, [coingecko_category_slugs...]).
#
# Residual = a union of three small narrative buckets — pooled because each
# alone would not yield 10 perps on Binance USDT-M.
DEFAULT_BUCKETS: list[tuple[str, list[str]]] = [
    ("anchors_l1",     ["layer-1"]),
    ("layer_2",        ["layer-2"]),
    ("defi",           ["decentralized-finance-defi"]),
    ("ai",             ["artificial-intelligence"]),
    ("memes",          ["meme-token"]),
    ("gaming",         ["gaming", "metaverse"]),
    ("lst_restaking",  ["liquid-staking-tokens"]),
    ("rwa",            ["real-world-assets-rwa"]),
    ("depin_storage",  ["depin", "storage"]),
    ("residual",       ["oracle", "privacy-coins",
                        "non-fungible-tokens-nft"]),
]

# Stable / wrapped tokens excluded from all buckets — copied from
# scanner.market_data to keep the exclusion in one place if either file
# evolves. Kept as a SET to keep the lookup O(1).
_EXCLUDED_BASE_SYMBOLS = {
    "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "USDE",
    "PYUSD", "USDD", "GUSD", "USDS", "FRAX", "LUSD", "RLUSD",
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBETH",
}


def _load_bucket_config() -> list[tuple[str, list[str]]]:
    """Return the active bucket configuration. Reads
    `scanner:category_slugs` (JSON `[[name, [slug, ...]], ...]`) from Redis
    when present, otherwise falls back to `DEFAULT_BUCKETS`. Invalid JSON
    is logged and falls back."""
    try:
        raw = redis_client.get().get("scanner:category_slugs")
        if raw:
            parsed = json.loads(raw)
            if (isinstance(parsed, list) and parsed
                    and all(isinstance(b, list) and len(b) == 2 for b in parsed)):
                return [(name, list(slugs)) for name, slugs in parsed]
            log.warning("scanner_category_slugs_invalid_shape")
    except Exception as exc:
        log.warning("scanner_category_slugs_read_failed", error=str(exc)[:120])
    return DEFAULT_BUCKETS


def _cg_to_binance(cg_symbol: str, binance_set: set[str]) -> str | None:
    base = (cg_symbol or "").upper()
    if not base or base in _EXCLUDED_BASE_SYMBOLS:
        return None
    candidate = f"{base}USDT"
    return candidate if candidate in binance_set else None


# CoinGecko Demo enforces a burst limit tighter than the documented
# 100 calls/min — empirically ~10-30 calls/min before 429s start. A
# 2.5 s spacing between calls keeps us at ~24/min, comfortably inside.
_INTER_CALL_DELAY_SEC = 2.5


def _fetch_category(slug: str, binance_set: set[str],
                    max_retries: int = 2) -> list[tuple[str, float]]:
    """Fetch one category page from CoinGecko. Returns
    `[(binance_symbol, market_cap_usd), ...]` ordered descending by mcap.

    Retry policy: on HTTP 429 (rate limit), sleep `Retry-After` seconds
    (or 30 if absent) and retry up to `max_retries` times. Other failures
    return [] immediately.
    """
    params = {
        "vs_currency":  "usd",
        "order":        "market_cap_desc",
        "per_page":     _CG_PER_PAGE,
        "page":         1,
        "sparkline":    "false",
        "category":     slug,
    }
    rows = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(_CG_MARKETS_URL, params=params,
                                timeout=_CG_TIMEOUT_SEC)
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(resp.headers.get("Retry-After") or 30.0)
                retry_after = max(5.0, min(60.0, retry_after))
                log.info("scanner_category_429_retry",
                         slug=slug, attempt=attempt + 1,
                         wait_s=retry_after)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            rows = resp.json()
            break
        except Exception as exc:
            if attempt < max_retries:
                time.sleep(5.0)
                continue
            log.warning("scanner_category_fetch_failed",
                        slug=slug, attempt=attempt + 1,
                        error=str(exc)[:200])
            return []
    if rows is None:
        return []

    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for row in rows or []:
        cg_sym = (row.get("symbol") or "").lower()
        mcap = float(row.get("market_cap") or 0.0)
        if mcap <= 0:
            continue
        binance_sym = _cg_to_binance(cg_sym, binance_set)
        if binance_sym is None or binance_sym in seen:
            continue
        seen.add(binance_sym)
        out.append((binance_sym, mcap))
    return out


def build_buckets(binance_symbols: Iterable[str],
                  *,
                  force_refresh: bool = False) -> dict[str, list[tuple[str, float]]]:
    """Returns `{bucket_name: [(symbol, mcap), ...]}` ordered by mcap-desc
    inside each bucket.

    Assignment rule (deterministic, first-match precedence):
        For each (bucket, slugs) in order:
            for each slug:
                pull the CoinGecko top-N for that slug
                for each coin that maps to a Binance perp:
                    if the coin hasn't been claimed by an earlier bucket
                    → claim it for this bucket
        Within a bucket, dedupe by symbol; order descending by mcap.

    Cached for 24 h. Set `scanner:category_cache_stale=1` to force refresh.
    """
    r = redis_client.get()
    binance_set = set(binance_symbols)

    if not force_refresh:
        try:
            if r.get(_FORCE_REFRESH_FLAG) == "1":
                force_refresh = True
                r.delete(_FORCE_REFRESH_FLAG)
        except Exception:
            pass

    config = _load_bucket_config()
    claimed: set[str] = set()
    out: dict[str, list[tuple[str, float]]] = {name: [] for name, _ in config}

    # Stage 1: load whatever's cached PER BUCKET. Successful buckets from
    # earlier scans skip the fetch this round.
    cached_hits: set[str] = set()
    if not force_refresh:
        for name, _ in config:
            try:
                raw = r.get(_BUCKET_CACHE_KEY_PREFIX + name)
                if not raw:
                    continue
                parsed = json.loads(raw)
                if not isinstance(parsed, list) or not parsed:
                    continue
                for entry in parsed:
                    if not (isinstance(entry, list) and len(entry) == 2):
                        continue
                    sym, mcap = entry
                    if sym in binance_set and sym not in claimed:
                        claimed.add(sym)
                        out[name].append((sym, float(mcap)))
                if out[name]:
                    cached_hits.add(name)
            except Exception as exc:
                log.debug("bucket_cache_read_failed",
                          bucket=name, error=str(exc)[:120])

    # Stage 2: fetch only the buckets that didn't load from cache.
    first_call = True
    failures = 0
    for name, slugs in config:
        if name in cached_hits:
            log.debug("scanner_category_bucket_cached",
                      bucket=name, n_pairs=len(out[name]))
            continue
        for slug in slugs:
            if not first_call:
                time.sleep(_INTER_CALL_DELAY_SEC)
            first_call = False
            page = _fetch_category(slug, binance_set)
            if not page:
                failures += 1
            for sym, mcap in page:
                if sym in claimed:
                    continue
                claimed.add(sym)
                out[name].append((sym, mcap))
        out[name].sort(key=lambda kv: kv[1], reverse=True)
        log.info("scanner_category_bucket_built",
                 bucket=name, n_pairs=len(out[name]),
                 sample=[s for s, _ in out[name][:5]])
        # Cache this bucket if it has pairs — even partial overall scans
        # progressively fill the cache across successive runs.
        if out[name]:
            try:
                payload = json.dumps([[s, m] for s, m in out[name]])
                r.setex(_BUCKET_CACHE_KEY_PREFIX + name,
                        _BUCKET_CACHE_TTL_SEC, payload)
            except Exception as exc:
                log.debug("bucket_cache_write_failed",
                          bucket=name, error=str(exc)[:120])

    if cached_hits:
        log.info("scanner_category_cache_hits",
                 hits=sorted(cached_hits), n=len(cached_hits))

    try:
        r.set("scanner:category_buckets_built_at", int(time.time()))
        r.incr("scanner:category_buckets_built_count")
    except Exception:
        pass

    return out


def select_top_per_bucket(buckets: dict[str, list[tuple[str, float]]],
                          composite_score: dict[str, float],
                          per_bucket: int = 10,
                          target_total: int = 100,
                          residual_bucket: str = "residual",
                          ) -> tuple[list[str], dict[str, str]]:
    """Pick top-`per_bucket` by composite score within each bucket;
    fill any deficit from the residual bucket (by mcap) until
    `target_total` is reached.

    Returns `(active_symbols, pair_to_bucket)`.

    Strict ownership: a symbol returned in the active list is recorded in
    `pair_to_bucket` with the bucket it was originally assigned to (NOT
    the residual it overflowed from), so per-pair attribution stays clean.

    `composite_score` may be missing entries; those are sorted last
    (score=0). A residual fill that lacks a composite score still goes
    in by mcap order — better than a thinner-than-100 universe.
    """
    selected: list[str] = []
    pair_to_bucket: dict[str, str] = {}

    def _ranked(syms: list[str]) -> list[str]:
        return sorted(syms,
                      key=lambda s: composite_score.get(s, 0.0),
                      reverse=True)

    for bucket, members in buckets.items():
        member_syms = [s for s, _ in members]
        chosen = _ranked(member_syms)[:per_bucket]
        for sym in chosen:
            if sym not in pair_to_bucket:
                pair_to_bucket[sym] = bucket
                selected.append(sym)

    deficit = target_total - len(selected)
    if deficit > 0:
        # Pool of unclaimed residual entries by mcap-desc (the natural
        # order produced by build_buckets).
        already_in = set(selected)
        residual_pool = [s for s, _ in buckets.get(residual_bucket, [])
                         if s not in already_in]
        for sym in residual_pool:
            if deficit <= 0:
                break
            pair_to_bucket[sym] = residual_bucket
            selected.append(sym)
            deficit -= 1

        if deficit > 0:
            # Last resort: any unallocated symbol from ANY bucket by mcap.
            all_pool = []
            seen: set[str] = set(selected)
            for bucket, members in buckets.items():
                for sym, mcap in members:
                    if sym not in seen:
                        all_pool.append((sym, mcap, bucket))
                        seen.add(sym)
            all_pool.sort(key=lambda t: t[1], reverse=True)
            for sym, _mcap, bucket in all_pool:
                if deficit <= 0:
                    break
                pair_to_bucket[sym] = bucket
                selected.append(sym)
                deficit -= 1

        if deficit > 0:
            try:
                redis_client.get().incr("scanner:bucket_underfilled_count")
            except Exception:
                pass
            log.warning("scanner_bucket_target_underfilled",
                        target=target_total, got=len(selected),
                        deficit=deficit)

    return selected, pair_to_bucket
