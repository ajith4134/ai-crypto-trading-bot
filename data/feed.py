"""
Section K: Market Data Ingestion — K-01 to K-13.
Stage 1: REST polling every 5s for mark prices (all pairs, single call).
WebSocket streams added in Stage 2+ when performance needs increase.
"""
import asyncio, csv, json, time
from pathlib import Path
import numpy as np, structlog, requests
import redis_client, redis_keys
import config

log = structlog.get_logger()

# cont. 47 — FUTURES_BASE is env-driven so live mode hits the real exchange.
# Previously hardcoded to testnet — caused TESTNET mark prices to appear in
# Redis even when the brain was placing REAL orders on MAINNET, producing
# visible lag/divergence vs the Binance app for the operator.
FUTURES_BASE = (
    "https://testnet.binancefuture.com" if config.BINANCE_TESTNET
    else "https://fapi.binance.com"
)
POLL_INTERVAL  = 5   # seconds between mark price polls
_price_history: dict[str, list] = {}
_returns:       dict[str, float] = {}
# cont. 69x item 1 — throttles WS→_price_history sampling to POLL_INTERVAL so the
# microstructure window (F15/F26/F28) keeps IDENTICAL 5s semantics after the REST
# premiumIndex poll goes standby. Updated only by _ws_mark_price_loop.
_ws_hist_last_ts: float = 0.0

# cont. 66 — asyncio fire-and-forget safety. asyncio.create_task() keeps only a
# WEAK reference to the task; an unreferenced task can be garbage-collected
# before it finishes (documented at docs.python.org asyncio.create_task). The
# 30m candle poll (data_loop, tick % 360) was the canonical victim: it is
# created immediately before the SYNCHRONOUS, allocation-heavy GNN + Transfer-
# Entropy refresh, whose GC pass collected the still-unstarted 30m task every
# single cycle → {pair}:30m:candles never populated despite F48_30m being
# active. (15m survived only because most of its % 180 fires are NOT % 360, so
# no blocking work follows them.) Holding a strong reference until completion
# is the fix.
_background_tasks: set = set()


def _spawn(coro):
    """create_task + strong reference so the task can't be GC'd mid-flight.
    The done-callback drops the reference once the task completes."""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


def _poll_mark_prices(r) -> int:
    """K-01: Fetch all USDT-M mark prices in one REST call."""
    try:
        resp = requests.get(f"{FUTURES_BASE}/fapi/v1/premiumIndex", timeout=10)
        resp.raise_for_status()
        count = 0
        pipe = r.pipeline(transaction=False)
        for item in resp.json():
            pair = item.get("symbol", "")
            if not pair.endswith("USDT"):
                continue
            price = item.get("markPrice", "0")
            funding = item.get("lastFundingRate", "0")
            pipe.set(redis_keys.MARK_PRICE.replace("{pair}", pair), price)
            pipe.set(redis_keys.FUNDING_RATE.replace("{pair}", pair), funding)
            pipe.set(redis_keys.LAST_PRICE.replace("{pair}", pair), price)
            # cont. 36: rolling 30-min mark window for DCA recovery gate.
            # Push price + trim to last 360 samples (30 min at 5s poll).
            # Consumer: risk/manager.py::_dca_recovery_ok.
            mw_key = f"{pair}:mark_window"
            pipe.lpush(mw_key, price)
            pipe.ltrim(mw_key, 0, 359)
            p = float(price)
            if p > 0:
                hist = _price_history.setdefault(pair, [])
                hist.append(p)
                if len(hist) > 100:
                    _price_history[pair] = hist[-100:]
            count += 1
        pipe.execute()
        return count
    except Exception as exc:
        log.error("mark_price_poll_failed", error=str(exc))
        return 0


def _ws_mark_fresh(r) -> int:
    """Number of pairs the WS mark feed is currently tracking, or 0 if its
    freshness beacon (set by _ws_mark_price_loop, TTL 30s) has expired.

    cont. 69x item 1 — drives the REST-premiumIndex standby in data_loop: while
    the WS feed is alive this returns >0 and the production-fapi mark poll never
    fires; if the WS drops for >30s the beacon expires and REST takes over as a
    visible fallback (Silent Rejection Rule)."""
    try:
        v = r.get("feed:ws_mark:fresh")
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _ws_ticker_fresh(r) -> int:
    """Number of symbols the WS 24h-ticker feed is currently tracking, or 0 if its
    freshness beacon (set by _ws_ticker_loop, TTL 60s) has expired.

    cont. 69x item 3 — drives the REST /fapi/v1/ticker/24hr standby in data_loop:
    while the !ticker@arr WS feed is alive this returns >0 and the production-fapi
    24h-ticker poll never fires; if the WS drops for >60s the beacon expires and
    REST takes over as a visible fallback (Silent Rejection Rule)."""
    try:
        v = r.get("feed:ws_ticker:fresh")
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _poll_24h_tickers(r) -> None:
    """K-02: 24h ticker data (volume, change). cont. 69x item 3: now a STANDBY
    fallback — data_loop calls this only when the !ticker@arr WS beacon is stale.
    REST /fapi/v1/ticker/24hr is weight 40 on the production fapi pool."""
    try:
        resp = requests.get(f"{FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=10)
        resp.raise_for_status()
        for item in resp.json():
            pair = item.get("symbol", "")
            if not pair.endswith("USDT"):
                continue
            r.set(redis_keys.TICKER_VOLUME_24H.replace("{pair}", pair), item.get("volume", "0"))
            r.set(redis_keys.TICKER_CHANGE_24H.replace("{pair}", pair), item.get("priceChangePercent", "0"))
    except Exception as exc:
        log.warning("ticker_poll_failed", error=str(exc))


def _compute_microstructure(r) -> None:
    """K-09/K-13: OFI, VPIN, Turbulence Index from price history.
    F30 governance gates: F15 (OFI/VPIN/Amihud/Kyles), F26 (BOCPD), F28 (Turbulence).
    Flags are cached once per tick to avoid 50× Redis GET in the per-pair loop."""
    try:
        # Cache feature flags once per tick — per-pair loop runs 50+ times so inline
        # is_active() would cost 50 Redis GETs. One tick = one check per flag.
        try:
            from feature_governance.registry import is_active as _fg
            f15_active = _fg("F15")
            f26_active = _fg("F26")
            f28_active = _fg("F28")
        except Exception:
            f15_active = f26_active = f28_active = True

        active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))
        for pair in active_pairs:
            hist = _price_history.get(pair, [])
            if len(hist) >= 2:
                ret = (hist[-1] - hist[-2]) / hist[-2] if hist[-2] > 0 else 0.0
                _returns[pair] = ret
                if f15_active:
                    vol = float(r.get(redis_keys.TICKER_VOLUME_24H.replace("{pair}", pair)) or 1)
                    abs_ret = abs(ret)
                    # Build a 20-tick rolling returns window for VPIN
                    # (D-05 fix: previously vpin = abs(ret) = same as OFI magnitude).
                    rwin = []
                    for i in range(max(1, len(hist) - 20), len(hist)):
                        if hist[i - 1] > 0:
                            rwin.append((hist[i] - hist[i - 1]) / hist[i - 1])
                    # VPIN proxy: rolling realized volatility — bursts of vol
                    # correlate with informed-trading toxicity in absence of
                    # tick-level buy/sell volume.
                    realized_vol = float(np.std(rwin)) if len(rwin) >= 5 else 0.0
                    # Kyle's λ proxy: price impact per √notional. Different
                    # dimension from Amihud (|ret|/vol) — λ uses sqrt for
                    # better behaviour with very-large volumes.
                    import math as _m
                    kyle = abs_ret / (_m.sqrt(vol) + 1e-9) if vol > 0 else 0.0
                    r.set(redis_keys.OFI.replace("{pair}", pair),          round(ret, 8))           # signed return
                    r.set(redis_keys.VPIN.replace("{pair}", pair),         round(realized_vol, 8))  # rolling vol
                    r.set(redis_keys.AMIHUD.replace("{pair}", pair),       round(abs_ret / vol, 10) if vol else 0)
                    r.set(redis_keys.KYLES_LAMBDA.replace("{pair}", pair), round(kyle, 10))

                # Blueprint F26: BOCPD changepoint detection — runs on every price update
                if f26_active and len(hist) >= 50:
                    try:
                        from ml.bocpd import update as bocpd_update
                        bocpd_update(pair, hist[-1])
                    except Exception:
                        pass

        if _returns:
            vals = list(_returns.values())
            arr  = np.array(vals, dtype=float)
            std  = float(np.std(arr))

            # cont. (2026-06-04) TURBULENCE FIX. The old formula was a
            # statistical identity: turb = sqrt(mean(((x-mean)/std)^2)). Because
            # the mean and std come from the SAME cross-section being scored, the
            # standardized vector ALWAYS has variance 1 → turb ≡ 1.0 forever,
            # regardless of real market conditions. It measured nothing, the
            # circuit-breaker (threshold 2.5) could never fire, and the LLM kept
            # misreading the constant "1.00" as alarming "high turbulence".
            #
            # Correct turbulence in the Kritzman-Li spirit asks: how unusual is
            # TODAY's cross-sectional dispersion relative to its own recent
            # history? We track a rolling window of the cross-sectional return
            # dispersion (std) and express the current dispersion as a ratio to
            # the rolling median. Calm market → ~1.0; a broad dislocation where
            # everything moves a lot at once → >> 1.0 (and CAN exceed 2.5, so the
            # deterministic breaker becomes meaningful again).
            turb = 1.0
            try:
                _HIST_KEY = "turbulence:dispersion_hist"
                _WARMUP = 30      # samples before the ratio is trusted
                _MAXHIST = 500
                if std > 0:
                    r.lpush(_HIST_KEY, std)
                    r.ltrim(_HIST_KEY, 0, _MAXHIST - 1)
                    _hist = [float(x) for x in r.lrange(_HIST_KEY, 0, _MAXHIST - 1)]
                    if len(_hist) >= _WARMUP:
                        _baseline = float(np.median(_hist))
                        if _baseline > 0:
                            turb = std / _baseline
                    # else: still warming up → leave at neutral 1.0
            except Exception as _texc:
                log.warning("turbulence_calc_skipped", error=str(_texc)[:120])
                turb = 1.0
            if f28_active:
                r.set(redis_keys.TURBULENCE_INDEX, round(turb, 6))
            # Publish returns for brain container's HMM — brain holds the model file.
            # NOT gated: HMM consumer (brain/soar.py) has its own F14 gate; data here
            # is just raw returns, no model output.
            import json as _json
            r.set("data:returns:recent", _json.dumps(vals[-100:]))
    except Exception as exc:
        log.error("microstructure_error", error=str(exc))


# ── cont. 69x — candle source migration: WS zset + WS-fed CSV corpus, ZERO REST ──
# _poll_candles/_poll_short_candles used to hammer /fapi/v1/klines for EVERY active pair
# on every cycle (~1500 production request-weight/min → the -1003 ban risk this whole
# migration removes). Both now build the CANDLES list from two ban-free sources, with REST
# kept only as a gated per-pair cold-start fallback:
#   1. klines:ws:{pair}:{itv}  — freshest CLOSED candles pushed by data/kline_ws.py
#      (market-data WS; consumes NO request-weight). Deep enough for the SHORT TFs.
#   2. data/historical/{pair}/{itv}.csv — the SAME corpus, topped up from those very WS
#      zsets by the celery incremental_ws task; carries the DEEP history the shallow live
#      zset lacks (e.g. 300×1h for PatchTST F20). Mounted read-only into data_feed.
# Levers (Redis): feed:candles:ws_enabled (default 1) master toggle → 0 reverts to pure REST;
# feed:candles:rest_fallback_enabled (default 1) allows per-pair REST when BOTH ban-free
# sources are empty (a brand-new pair). Beacons feed:candles:source:{itv} (ws|mixed|rest) +
# feed:candles:rest_fallback:{itv} (count) keep the fallback visible (Silent Rejection Rule).
_CORPUS_DIR = Path("data/historical")


def _flag(r, key: str, default: str = "1") -> bool:
    try:
        v = r.get(key)
        return (v if v is not None else default) == "1"
    except Exception:
        return default == "1"


def _ws_zset_candles(r, pair: str, interval: str, need: int) -> dict[int, list]:
    """Newest `need` CLOSED candles from kline_ws's zset → {ts: [ts,o,h,l,c,v]}."""
    out: dict[int, list] = {}
    try:
        members = r.zrevrange(f"klines:ws:{pair}:{interval}", 0, need - 1)
    except Exception:
        return out
    for m in (members or []):
        try:
            row = json.loads(m)
            ts = int(row[0])
            out[ts] = [ts, row[1], row[2], row[3], row[4], row[5]]
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return out


def _corpus_csv_candles(pair: str, interval: str, need: int) -> dict[int, list]:
    """Newest `need` candles from the WS-fed CSV corpus → {ts: [ts,o,h,l,c,v]}. Zero REST."""
    out: dict[int, list] = {}
    p = _CORPUS_DIR / pair / f"{interval}.csv"
    if not p.exists():
        return out
    try:
        with open(p, newline="") as f:
            rd = csv.reader(f)
            next(rd, None)  # header
            rows = [row[:6] for row in rd if len(row) >= 6]
    except Exception:
        return out
    for row in rows[-need:]:
        try:
            ts = int(row[0])
            out[ts] = [ts, row[1], row[2], row[3], row[4], row[5]]
        except (TypeError, ValueError):
            continue
    return out


def _stored_list_candles(r, pair: str, interval: str, need: int) -> dict[int, list]:
    """Newest `need` candles ALREADY in the live CANDLES list → {ts:[ts,o,h,l,c,v]}.

    cont. 75 — merging this back into each rebuild lets a pair PRESERVE accumulated depth
    (including a prior REST backfill) instead of every poll DELETE+rewriting from a thin
    WS-only payload. Without it a freshly rotated-in pair could never grow history beyond
    live WS accumulation (1h gains only 1 closed bar/hour)."""
    out: dict[int, list] = {}
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", interval)
    try:
        items = r.lrange(key, 0, need - 1)
    except Exception:
        return out
    for it in (items or []):
        try:
            c = json.loads(it)
            ts = int(c["t"])
            out[ts] = [ts, c["o"], c["h"], c["l"], c["c"], c.get("v", 0)]
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return out


def _fetch_rest(pair: str, interval: str, limit: int) -> list[str]:
    """Cold-start fallback: /fapi/v1/klines → oldest-first JSON-dict strings (production weight).
    Binance kline: [openTime, open, high, low, close, volume, closeTime, ...]."""
    try:
        resp = requests.get(
            f"{FUTURES_BASE}/fapi/v1/klines",
            params={"symbol": pair, "interval": interval, "limit": limit},
            timeout=8,
        )
        resp.raise_for_status()
        klines = resp.json()
        if not isinstance(klines, list) or not klines:
            return []
        return [
            json.dumps({"t": k[0], "o": k[1], "h": k[2], "l": k[3], "c": k[4], "v": k[5]})
            for k in klines
        ]
    except Exception:
        return []


def _build_candles_ws(r, pair: str, interval: str, need: int) -> list[str]:
    """CANDLES payload (oldest-first JSON-dict strings) from corpus CSV + WS zset.
    Newest closed bars (zset) win over corpus for any overlapping ts. [] if both empty."""
    merged = _corpus_csv_candles(pair, interval, need)
    merged.update(_stored_list_candles(r, pair, interval, need))   # cont.75 — preserve depth
    merged.update(_ws_zset_candles(r, pair, interval, need))       # freshest WS wins on overlap
    if not merged:
        return []
    ordered = [merged[ts] for ts in sorted(merged)][-need:]
    return [
        json.dumps({"t": int(rw[0]), "o": rw[1], "h": rw[2], "l": rw[3], "c": rw[4], "v": rw[5]})
        for rw in ordered
    ]


# cont. 75 — per-interval "enough bars" target. Below this a pair is treated as THIN and
# gets a one-off REST backfill (1h needs 168 for HMM/regime; short TFs 60 for the modules).
_MIN_BARS_TARGET = {"1m": 60, "5m": 60, "15m": 60, "30m": 60, "1h": 168}
_BACKFILL_COOLDOWN_S = 7200   # re-backfill a (pair,interval) at most once / 2h
_BACKFILL_WINDOW_S = 30       # global rate window
_BACKFILL_PER_WINDOW = 20     # max REST backfills per window → request-weight safe


def _backfill_allowed(r, pair: str, interval: str) -> bool:
    """Rate-gate thin-history REST backfills so seeding can never trip the /fapi weight ban:
    a per-(pair,interval) cooldown (no repeat within 2h) AND a global per-window token budget
    (≤ _BACKFILL_PER_WINDOW per 30s across all pairs/TFs). Reserves the token only when it
    actually grants the backfill."""
    cd_key = f"feed:candles:backfilled:{pair}:{interval}"
    try:
        if r.get(cd_key):
            return False
        win = "feed:candles:backfill_window"
        n = r.incr(win)
        if n == 1:
            r.expire(win, _BACKFILL_WINDOW_S)
        if n > _BACKFILL_PER_WINDOW:
            return False
        r.setex(cd_key, _BACKFILL_COOLDOWN_S, "1")
        return True
    except Exception:
        return False


def _collect_candles(r, pair: str, interval: str, need: int,
                     use_ws: bool, rest_allowed: bool) -> tuple[list[str], str]:
    """Returns (oldest-first payload, source) — source in {ws, rest, none}.

    cont. 75 — backfills a THIN history from REST, not only an empty one. A freshly
    rotated-in pair accumulates higher-TF bars slowly over WS (1h = 1 bar/hr), so returning
    the few-bar partial payload starved the modules/forecasts (e.g. hmm_regime needs 168×1h).
    The backfill is rate-gated (_backfill_allowed) and the merge in _build_candles_ws then
    keeps the seeded depth, so each pair needs REST at most once."""
    payload = _build_candles_ws(r, pair, interval, need) if use_ws else []
    target = _MIN_BARS_TARGET.get(interval, need)
    if rest_allowed and len(payload) < target and _backfill_allowed(r, pair, interval):
        rp = _fetch_rest(pair, interval, need)
        if rp and len(rp) > len(payload):
            return rp, "rest"
    if payload:
        return payload, "ws"
    if rest_allowed:
        rp = _fetch_rest(pair, interval, need)
        if rp:
            return rp, "rest"
    return [], "none"


def _write_candles_list(r, pair: str, interval: str, payload_oldest_first: list[str], cap: int) -> None:
    """Write CANDLES list newest-first. LPUSH oldest→newest leaves newest at index 0,
    oldest at -1 — the exact contract consumers' `reversed()` relies on."""
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", interval)
    pipe = r.pipeline()
    pipe.delete(key)
    for cj in payload_oldest_first:
        pipe.lpush(key, cj)
    pipe.ltrim(key, 0, cap - 1)
    pipe.execute()


def _report_candle_source(r, interval: str, count: int, rest_used: int) -> None:
    try:
        src = "rest" if (count and rest_used == count) else ("mixed" if rest_used else "ws")
        r.setex(f"feed:candles:source:{interval}", 600, src)
        if rest_used:
            r.setex(f"feed:candles:rest_fallback:{interval}", 600, rest_used)
            log.warning("candles_rest_fallback", interval=interval, pairs=rest_used)
    except Exception:
        pass


async def _poll_candles(r) -> int:
    """K-02b: populate the 1h CANDLES list for active pairs.

    Rule 4 finding: TFT (ml/tft.py), PatchTST (ml/patchtst.py) and Transfer Entropy
    (ml/transfer_entropy.py) ALL read this key — empty input → theatrical forecasts.

    Storage contract: Redis list newest-first; element = JSON dict t/o/h/l/c/v.
    cont. 69x: sourced from WS zset + CSV corpus (zero request-weight); REST only as a
    gated cold-start fallback. 300 candles satisfy PatchTST's 256-context (F20) + margin;
    TFT (F19) uses the most recent 100 — backward compatible.

    F30 governance gate: skip entirely when all of F19/F20/F27 are deactivated.
    """
    try:
        from feature_governance.registry import is_active
        if not any(is_active(fid) for fid in ("F19", "F20", "F27")):
            return 0
    except Exception:
        pass

    # cont. 65 — [:500] (was [:50]); SMEMBERS is unordered so a smaller cap starved
    # the non-top pairs of candle data and cascaded into multi_tf_cascade OFI-fallback.
    active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))[:500]
    if not active_pairs:
        return 0

    use_ws = _flag(r, "feed:candles:ws_enabled", "1")
    rest_allowed = _flag(r, "feed:candles:rest_fallback_enabled", "1")
    loop = asyncio.get_event_loop()

    # Build each pair's payload off the event loop (CSV read + Redis zset reads are
    # blocking); redis-py's client/pool is thread-safe. Writes stay on the loop.
    def _one(pair: str):
        payload, src = _collect_candles(r, pair, "1h", 300, use_ws, rest_allowed)
        return (pair, payload, src)

    results = await asyncio.gather(
        *[loop.run_in_executor(None, _one, p) for p in active_pairs],
        return_exceptions=True,
    )

    count = rest_used = 0
    for result in results:
        if isinstance(result, Exception):
            continue
        pair, payload, src = result
        if not payload:
            continue
        _write_candles_list(r, pair, "1h", payload, 300)
        count += 1
        if src == "rest":
            rest_used += 1
    _report_candle_source(r, "1h", count, rest_used)
    return count


async def _poll_candles_and_log(r) -> None:
    """Wrapper that logs the candle poll result. Used with asyncio.create_task."""
    import time as _time
    t0 = _time.time()
    try:
        n = await _poll_candles(r)
        log.info("candles_polled", pairs=n, elapsed_s=round(_time.time() - t0, 2))
    except Exception as exc:
        log.error("candles_poll_failed", error=str(exc)[:200])


async def _poll_short_candles(r, interval: str, limit: int = 65) -> int:
    """F48: populate the {1m,5m,15m,30m} CANDLES list for active pairs (multi-TF cascade).

    Only runs when F48_{interval} is active (F30 governance gate). `limit`=65 covers
    CONTEXT_LENGTH=60 + margin. Called every 60s (1m), 5min (5m), 15min (15m), 30min (30m).
    cont. 69x: WS zset + CSV corpus (zero request-weight); REST only as a gated cold-start
    fallback. The short-TF zsets are deep enough that REST never fires in steady state.
    """
    try:
        from feature_governance.registry import is_active as _fg
        interval_id = {"1m": "F48_1m", "5m": "F48_5m",
                       "15m": "F48_15m", "30m": "F48_30m"}.get(interval)
        if interval_id is None:
            return 0
        if not _fg(interval_id):
            return 0
    except Exception:
        pass

    # cont. 65 — [:500] (was [:50]); a smaller cap silently starved non-top pairs of
    # 1m/5m/15m candles and OFI-fallbacked their entries in multi_tf_cascade.
    active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))[:500]
    if not active_pairs:
        return 0

    use_ws = _flag(r, "feed:candles:ws_enabled", "1")
    rest_allowed = _flag(r, "feed:candles:rest_fallback_enabled", "1")
    loop = asyncio.get_event_loop()

    def _one(pair: str):
        payload, src = _collect_candles(r, pair, interval, limit, use_ws, rest_allowed)
        return (pair, payload, src)

    results = await asyncio.gather(
        *[loop.run_in_executor(None, _one, p) for p in active_pairs],
        return_exceptions=True,
    )

    count = rest_used = 0
    for result in results:
        if isinstance(result, Exception):
            continue
        pair, payload, src = result
        if not payload:
            continue
        _write_candles_list(r, pair, interval, payload, limit)
        count += 1
        if src == "rest":
            rest_used += 1
    _report_candle_source(r, interval, count, rest_used)
    return count


async def _poll_short_candles_and_log(r, interval: str, limit: int = 65) -> None:
    """Wrapper that logs the short-candle poll result so a zero-write becomes
    visible instead of silently vanishing (Silent Rejection Rule). Mirrors
    _poll_candles_and_log for the 1h poll. Also bumps a per-interval Redis
    counter so the dashboard / watchdog can detect a stalled timeframe."""
    t0 = time.time()
    try:
        n = await _poll_short_candles(r, interval, limit=limit)
        log.info("short_candles_polled", interval=interval, pairs=n,
                 elapsed_s=round(time.time() - t0, 2))
        try:
            r.setex(f"feed:short_candles:{interval}:last_pairs", 3600, n)
        except Exception:
            pass
    except Exception as exc:
        log.error("short_candles_poll_failed", interval=interval,
                  error=str(exc)[:200])


async def _poll_patchtst_forecasts_and_log(r) -> None:
    """F20: refresh PatchTST forecasts. cont. 74 — now BATCHED: one forward pass over
    the whole active set (chunked) instead of ~6-9s/pair sequential. This makes
    full-universe coverage feasible (the old loop capped at 30 pairs = 211s/cycle).
    Runs the batch in the executor so the heavy forward pass doesn't block the loop.
    patchtst:poll_cap (default 0 = all active) bounds the set if ever needed."""
    import time as _time
    t0 = _time.time()
    from ml.patchtst import get_longsequence_forecast_batch
    active = sorted(p.decode() if isinstance(p, bytes) else p
                    for p in r.smembers(redis_keys.ACTIVE_PAIRS))
    try:
        _cap = int(r.get("patchtst:poll_cap") or 0)
    except Exception:
        _cap = 0
    if _cap > 0:
        active = active[:_cap]
    if not active:
        return
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, get_longsequence_forecast_batch, active)
    except Exception as exc:
        log.error("patchtst_batch_failed", error=str(exc)[:200])
        return
    log.info("patchtst_polled", ok=len(results), attempted=len(active),
             elapsed_s=round(_time.time() - t0, 2), mode="batched")


async def _poll_tft_forecasts(r) -> None:
    """F19: cont. 74 — pre-warm TFT quantile forecasts across the active set AND multiple
    timeframes via BATCHED inference, so tft_score covers the full universe and (once the
    consumer is wired) multiple horizons — not just 1h on the funnel pairs. Timeframes
    tunable via tft:poll_tfs (default '5m,15m,1h'). Batched → bounded cost."""
    import time as _time
    t0 = _time.time()
    from ml.tft import get_price_forecast_batch
    active = sorted(p.decode() if isinstance(p, bytes) else p
                    for p in r.smembers(redis_keys.ACTIVE_PAIRS))
    if not active:
        return
    _raw = r.get("tft:poll_tfs")
    _raw = (_raw.decode() if isinstance(_raw, bytes) else _raw) if _raw else "5m,15m,1h"
    tfs = [x.strip() for x in _raw.split(",") if x.strip()]
    # Per-TF context: 1h keeps 100 (matches the live single-path 1h so the cached value
    # the engine reads is unchanged); short TFs use 50 (their candle lists hold ~65).
    _ctx = {"1m": 50, "5m": 50, "15m": 50, "30m": 50, "1h": 100, "2h": 80, "4h": 60}
    loop = asyncio.get_event_loop()
    total = 0
    for tf in tfs:
        try:
            res = await loop.run_in_executor(
                None, get_price_forecast_batch, active, tf, _ctx.get(tf, 60))
            total += len(res)
        except Exception as exc:
            log.error("tft_batch_poll_failed", tf=tf, error=str(exc)[:160])
    log.info("tft_polled", ok=total, pairs=len(active), tfs=len(tfs),
             elapsed_s=round(_time.time() - t0, 2), mode="batched")


def _poll_fear_greed(r) -> None:
    """L-05 proxy: Crypto Fear & Greed Index as global sentiment signal.
    Writes global_sentiment (0-1 scale) and per-pair sentiment for all active pairs.
    Per-pair values can be overridden by real NLP sentiment when web_intel activates.
    F30 governance gate: skip when F18 deactivated.

    DEFERRAL (2026-05-20, PROGRESS.md cont. 9): when ml/sentiment.py has
    written real CryptoBERT+FinBERT scores within the last 30 minutes, this
    proxy step DOES NOT overwrite SENTIMENT_GLOBAL — the real value stands.
    Per-pair fallback still runs for pairs that haven't been tagged in
    web_intel yet (web_intel.pairs_affected coverage is partial).
    """
    try:
        from feature_governance.registry import is_active as _fg_active
        if not _fg_active("F18"):
            return
    except Exception:
        pass
    # Defer to real sentiment when fresh — F&G proxy was stuck at 27 (extreme
    # fear) for the entire production window and triggered the D-03 bull-short
    # bias. With CryptoBERT+FinBERT live, we want its value to drive direction.
    try:
        from ml.sentiment import real_sentiment_fresh as _real_fresh
        defer_global = _real_fresh()
    except Exception:
        defer_global = False

    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        resp.raise_for_status()
        value = int(resp.json()["data"][0]["value"])  # 0-100
        sentiment = round(value / 100, 4)             # 0.0 = fear, 1.0 = greed
        if not defer_global:
            r.set(redis_keys.SENTIMENT_GLOBAL, sentiment)
            r.set("sentiment:source", "fear_greed_proxy")
        # When CryptoBERT/FinBERT global is fresh, use the real global value
        # for per-pair fallback instead of the F&G proxy. F&G is a single global
        # number anyway — clobbering every per-pair key with it when the better
        # global signal exists in SENTIMENT_GLOBAL is strictly worse. This was
        # the root of the cont. 22 audit: 100 active pairs all stuck at 0.29 (F&G
        # value today) while CryptoBERT's real global sat at 0.4267.
        pair_fallback_sentiment = sentiment
        if defer_global:
            try:
                _real = r.get(redis_keys.SENTIMENT_GLOBAL)
                if _real is not None:
                    pair_fallback_sentiment = float(_real)
            except (TypeError, ValueError):
                pass
        # Distribute to active pairs only when the pair has no recent real
        # per-pair sentiment of its own. This keeps any pair we've successfully
        # scored from being clobbered by the proxy on the next 30s tick.
        import time as _t
        now = int(_t.time())
        active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))
        pairs_proxied = 0
        for pair in active_pairs:
            pair_ts = r.get(f"sentiment:pair:{pair}:last_ts")
            if pair_ts:
                try:
                    if (now - int(pair_ts)) < 30 * 60:
                        continue   # pair has fresh real sentiment, skip
                except (TypeError, ValueError):
                    pass
            r.set(redis_keys.SENTIMENT_PAIR.replace("{pair}", pair),
                  pair_fallback_sentiment)
            pairs_proxied += 1
        log.info("fear_greed_updated", value=value, sentiment=sentiment,
                 global_deferred=defer_global,
                 pair_fallback=pair_fallback_sentiment,
                 pairs_proxied=pairs_proxied,
                 pairs_skipped_real=len(active_pairs) - pairs_proxied)
    except Exception as exc:
        log.warning("fear_greed_poll_failed", error=str(exc))


async def _refresh_gnn() -> None:
    """F24 GNN inter-asset correlation refresh. get_interasset_signals() is
    synchronous + CPU-heavy; run it in the default executor so the now-5-min
    cadence (cont. 66) never blocks the 5s mark-price loop."""
    try:
        from ml.gnn import get_interasset_signals
        await asyncio.get_event_loop().run_in_executor(None, get_interasset_signals)
    except Exception as exc:
        log.warning("gnn_update_skipped", error=str(exc))


async def _refresh_transfer_entropy() -> None:
    """F27 Transfer Entropy lead-lag refresh — synchronous + heavy, run in executor."""
    try:
        from ml.transfer_entropy import get_lead_lag_matrix
        await asyncio.get_event_loop().run_in_executor(None, get_lead_lag_matrix)
    except Exception as exc:
        log.warning("transfer_entropy_skipped", error=str(exc))


async def data_loop() -> None:
    r = redis_client.get()
    tick_counter = 0
    log.info("data_feed_loop_started", poll_interval_s=POLL_INTERVAL)
    while True:
        # cont. 69x item 1 — REST premiumIndex is now a STANDBY fallback. While the
        # WS mark feed (_ws_mark_price_loop) is alive it populates MARK_PRICE +
        # _price_history and sets the freshness beacon, so the production-fapi mark
        # poll never fires. Only when the WS drops for >30s does REST take over —
        # logged + countered so the fallback is never silent (Silent Rejection Rule).
        fresh = _ws_mark_fresh(r)
        if fresh > 0:
            count = fresh
        else:
            count = _poll_mark_prices(r)
            if count:
                log.warning("mark_rest_fallback", pairs=count, reason="ws_mark_stale")
                try:
                    r.setex("feed:mark:rest_fallback", 120, count)
                    r.set("feed:mark:source", "rest_fallback")
                except Exception:
                    pass
        if count and tick_counter % 6 == 0:   # every ~30s
            # cont. 69x item 3 — REST /fapi/v1/ticker/24hr is now a STANDBY. While
            # the !ticker@arr WS feed is alive (_ws_ticker_loop sets the beacon) the
            # production-fapi 24h poll never fires; only a >60s WS gap triggers REST,
            # logged + countered so the fallback is never silent.
            if _ws_ticker_fresh(r) <= 0:
                _poll_24h_tickers(r)
                log.warning("ticker_rest_fallback", reason="ws_ticker_stale")
                try:
                    r.setex("feed:ticker:rest_fallback", 120, 1)
                    r.set("feed:ticker:source", "rest_fallback")
                except Exception:
                    pass
        if count and tick_counter % 2 == 0:   # every ~10s
            _compute_microstructure(r)
        if tick_counter % 12 == 0:            # every ~60s — F48 1min candles
            _spawn(_poll_short_candles_and_log(r, "1m", limit=65))
        if tick_counter % 60 == 0:            # every ~5 min
            _poll_fear_greed(r)
            # Schedule candle poll as a concurrent task — don't await it here so the
            # data loop's mark-price ticks keep firing even while 50 REST calls are
            # in flight. The task logs its own pair count on completion.
            _spawn(_poll_candles_and_log(r))
            # F48 5min candles — same cadence as 1h refresh
            _spawn(_poll_short_candles_and_log(r, "5m", limit=65))
            log.info("data_feed_heartbeat", pairs_updated=count, tick=tick_counter)

            # Blueprint F24/F27/F20 — model refresh every 5 min. cont. 66: MOVED
            # here from the % 360 block, where a mis-indentation had them running
            # every 30 min (6× slower than the blueprint's 5-min spec). GNN + TE
            # are synchronous + heavy → run in the executor via _spawn (strong-ref,
            # non-blocking); PatchTST is already async.
            try:
                from feature_governance.registry import is_active as _fg_active
                if _fg_active("F24"):
                    _spawn(_refresh_gnn())
                if _fg_active("F27"):
                    _spawn(_refresh_transfer_entropy())
                if _fg_active("F20"):
                    _spawn(_poll_patchtst_forecasts_and_log(r))
                if _fg_active("F19"):           # cont. 74 — batched multi-TF TFT pre-warm
                    _spawn(_poll_tft_forecasts(r))
            except Exception as exc:
                log.warning("model_refresh_skipped", error=str(exc))

        # F48 §Idea D — 15m candles (4-TF hierarchy). 15min closed candles
        # only appear every 15 min; poll every 15 min (tick_counter % 180).
        if tick_counter % 180 == 0:
            _spawn(_poll_short_candles_and_log(r, "15m", limit=65))

        # cont. 65d — 30m candles for multi-TF cascade gap fill (15m→1h).
        # cont. 66 — _spawn (not bare create_task) so the GC can't collect the task
        # before it runs. The synchronous GNN/TE refresh that used to sit here (and
        # starve this task) has been moved to the % 60 (5-min) block where it belongs.
        if tick_counter % 360 == 0:
            _spawn(_poll_short_candles_and_log(r, "30m", limit=65))

        if tick_counter % 120 == 0:         # every ~10 min
            # Blueprint F23: Mutual Information feature relevance rankings.
            # Compute I(feature_t; return_{t+k}) for each tracked feature against
            # returns of the most-traded pair, across lags k=1..50. Updates Redis
            # ranking under analytics:feature_relevance — consumed downstream by
            # any caller of get_feature_relevance_rankings / get_optimal_forecast_horizons.
            try:
                from feature_governance.registry import is_active as _fg_active
                if _fg_active("F23"):
                    from ml.mutual_info import compute_mutual_info, update_rankings
                    # Use price-derived features (which DO have history via the CANDLES list)
                    # against forward returns. Computes I(feature_t; return_{t+k}) for
                    # k=1..50 — this is what populates analytics:feature_relevance and
                    # surfaces the optimal forecast horizon per feature.
                    active = list(r.smembers(redis_keys.ACTIVE_PAIRS))
                    if active:
                        target_pair = active[0]
                        candles_raw = r.lrange(
                            redis_keys.CANDLES.replace("{pair}", target_pair).replace("{interval}", "1h"),
                            0, 199,
                        )
                        if candles_raw and len(candles_raw) >= 60:
                            candles = [json.loads(c) for c in reversed(candles_raw)]
                            closes = [float(c["c"]) for c in candles]
                            volumes = [float(c.get("v", 0)) for c in candles]
                            returns = [(closes[i] - closes[i - 1]) / closes[i - 1]
                                       for i in range(1, len(closes)) if closes[i - 1] > 0]
                            # Each feature is a per-candle scalar series — real variance
                            # exists so MI is meaningful.
                            log_returns = returns  # i.e. r_t
                            abs_returns = [abs(x) for x in returns]  # |r_t| = realized vol proxy
                            vol_norm = [
                                volumes[i] / volumes[i - 1] - 1 if volumes[i - 1] > 0 else 0.0
                                for i in range(1, len(volumes))
                            ]
                            for feat_key, feat_series in [
                                ("returns_autocorr", log_returns),
                                ("realized_vol", abs_returns),
                                ("volume_change", vol_norm),
                            ]:
                                scores = compute_mutual_info(feat_series, returns, lags=50)
                                if scores:
                                    update_rankings(feat_key, scores)
            except Exception as exc:
                log.warning("mutual_info_skipped", error=str(exc))
        tick_counter += 1
        await asyncio.sleep(POLL_INTERVAL)


async def _ws_mark_price_loop() -> None:
    """cont. 47 — Lag-2 fix: WebSocket stream of all USDT futures mark prices.

    Replaces the 5s REST polling cadence with realtime push from Binance's
    `!markPrice@arr@1s` stream (1-second update for every USDT-M futures
    symbol). Writes to the same Redis MARK_PRICE keys the REST poller does,
    so consumers (signals/engine, dashboard, monitor_trailing_sl) need no
    change. REST polling continues in parallel as a fallback in case the
    WS feed drops.

    On live mode (BINANCE_TESTNET=false) connects to fstream.binance.com.
    On testnet, connects to the stream-testnet endpoint.
    """
    import websockets
    import config as _config
    r = redis_client.get()

    # cont. 69x — ROUTED /market/stream endpoint. Binance decommissioned the legacy
    # UNROUTED /ws path for /market streams (markPrice is /market) on 2026-04-23, so
    # wss://fstream.binance.com/ws/!markPrice@arr delivered SILENT NO-DATA on production
    # — live mode was falling back to REST premiumIndex (weight). /market/stream delivers
    # (verified prod + testnet); messages arrive WRAPPED as {"stream":..,"data":[...]}.
    if _config.BINANCE_TESTNET:
        ws_url = "wss://stream.binancefuture.com/market/stream?streams=!markPrice@arr@1s"
    else:
        ws_url = "wss://fstream.binance.com/market/stream?streams=!markPrice@arr@1s"

    delay = 1
    while True:
        try:
            async with websockets.connect(ws_url,
                                          ping_interval=30,
                                          ping_timeout=10,
                                          close_timeout=10) as ws:
                log.info("ws_mark_price_connected", url=ws_url)
                delay = 1
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        # /market/stream wraps the array as {"stream":..,"data":[...]}
                        items = msg.get("data", msg) if isinstance(msg, dict) else msg
                        if not isinstance(items, list):
                            continue
                        # cont. 69x item 1 — MARK_PRICE/LAST_PRICE update at full 1s
                        # WS rate (realtime trailing-SL), but the microstructure
                        # price-history window is sampled only every POLL_INTERVAL so
                        # OFI/VPIN/Turbulence keep the SAME 5s cadence the retired REST
                        # premiumIndex poll gave them.
                        global _ws_hist_last_ts
                        now = time.time()
                        sample = (now - _ws_hist_last_ts) >= POLL_INTERVAL
                        pipe = r.pipeline(transaction=False)
                        n_marks = 0
                        for item in items:
                            pair = item.get("s", "")
                            if not pair.endswith("USDT"):
                                continue
                            mp = item.get("p")
                            fr = item.get("r")
                            if mp:
                                pipe.set(redis_keys.MARK_PRICE.replace("{pair}", pair), mp)
                                pipe.set(redis_keys.LAST_PRICE.replace("{pair}", pair), mp)
                                n_marks += 1
                                if sample:
                                    try:
                                        p = float(mp)
                                    except (TypeError, ValueError):
                                        p = 0.0
                                    if p > 0:
                                        hist = _price_history.setdefault(pair, [])
                                        hist.append(p)
                                        if len(hist) > 100:
                                            _price_history[pair] = hist[-100:]
                                        # cont. 36 DCA mark-window parity (consumer
                                        # dormant — DCA permanently disabled — but kept
                                        # so re-enabling needs no feed change).
                                        mw_key = f"{pair}:mark_window"
                                        pipe.lpush(mw_key, mp)
                                        pipe.ltrim(mw_key, 0, 359)
                            if fr:
                                pipe.set(redis_keys.FUNDING_RATE.replace("{pair}", pair), fr)
                        if sample:
                            _ws_hist_last_ts = now
                            # Freshness beacon — data_loop skips the REST premiumIndex
                            # fallback while this key is alive.
                            pipe.setex("feed:ws_mark:fresh", 30, n_marks)
                            pipe.set("feed:mark:source", "ws")
                        pipe.execute()
                    except Exception as parse_exc:
                        log.debug("ws_mark_parse_failed", error=str(parse_exc)[:120])
        except Exception as exc:
            log.warning("ws_mark_price_disconnected",
                        error=str(exc)[:200], reconnect_in_s=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def _ws_ticker_loop() -> None:
    """cont. 69x item 3 — WebSocket 24h rolling-window ticker for all USDT-M
    symbols via `!ticker@arr` on the routed /market/stream endpoint. Replaces the
    30s REST /fapi/v1/ticker/24hr poll (production fapi weight 40) — writes the
    SAME Redis keys (TICKER_VOLUME_24H ← base vol `v`, TICKER_CHANGE_24H ←
    pct change `P`) so the scanner / dead-pair filter / volume sort are unchanged.

    !ticker@arr pushes (every ~1s) an array of only the symbols whose 24h stats
    changed; over a few seconds the whole active universe is refreshed. The beacon
    feed:ws_ticker:fresh (TTL 60s) gates the REST standby in data_loop.

    Routed /market/stream is mandatory: !ticker is a /market stream and the legacy
    unrouted /ws path was decommissioned 2026-04-23 (silent no-data). Testnet vs
    prod endpoint mirrors _ws_mark_price_loop.
    """
    import websockets
    import config as _config
    r = redis_client.get()

    if _config.BINANCE_TESTNET:
        ws_url = "wss://stream.binancefuture.com/market/stream?streams=!ticker@arr"
    else:
        ws_url = "wss://fstream.binance.com/market/stream?streams=!ticker@arr"

    delay = 1
    while True:
        try:
            async with websockets.connect(ws_url,
                                          ping_interval=30,
                                          ping_timeout=10,
                                          close_timeout=10) as ws:
                log.info("ws_ticker_connected", url=ws_url)
                delay = 1
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        items = msg.get("data", msg) if isinstance(msg, dict) else msg
                        if not isinstance(items, list):
                            continue
                        pipe = r.pipeline(transaction=False)
                        n = 0
                        for item in items:
                            pair = item.get("s", "")
                            if not pair.endswith("USDT"):
                                continue
                            vol = item.get("v")   # 24h base-asset volume (== REST `volume`)
                            chg = item.get("P")   # 24h price-change percent
                            if vol is not None:
                                pipe.set(redis_keys.TICKER_VOLUME_24H.replace("{pair}", pair), vol)
                            if chg is not None:
                                pipe.set(redis_keys.TICKER_CHANGE_24H.replace("{pair}", pair), chg)
                            n += 1
                        if n:
                            pipe.setex("feed:ws_ticker:fresh", 60, n)
                            pipe.set("feed:ticker:source", "ws")
                        pipe.execute()
                    except Exception as parse_exc:
                        log.debug("ws_ticker_parse_failed", error=str(parse_exc)[:120])
        except Exception as exc:
            log.warning("ws_ticker_disconnected",
                        error=str(exc)[:200], reconnect_in_s=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def _account_metrics_loop() -> None:
    """cont. 47 — Lag-1 fix: refresh ACCOUNT_BALANCE every 30s.

    Pre-cont.47 update_account_metrics() ran only on trade close, so live
    mode showed entry-time balances on the dashboard until the next close.
    Now refreshed every 30s. For live mode the function requires a Binance
    client (otherwise it no-ops — caught one such bug in cont. 47 first
    deploy). Construct the client lazily so paper mode never instantiates it.
    """
    from account_risk.monitor import update_account_metrics
    import config as _config
    _client = None
    if _config.TRADING_MODE == "live":
        try:
            from exchange.client import BinanceClient
            _client = BinanceClient()
            log.info("account_metrics_loop_using_binance_client")
        except Exception as exc:
            log.error("account_metrics_loop_client_init_failed",
                      error=str(exc)[:200])
    while True:
        try:
            update_account_metrics(_client)
        except Exception as exc:
            log.warning("account_metrics_periodic_failed", error=str(exc)[:200])
        await asyncio.sleep(30)


async def _supervise(coro_factory, name: str) -> None:
    """cont. 74 — NEVER let a pipeline loop die silently. Root cause of the 13h
    data_feed zombie: gather(return_exceptions=True) swallowed a stale-pool
    ConnectionError when redis got a new IP; data_loop() died while the process
    + other loops stayed up, so Docker showed 'healthy'. This wraps each loop:
    on any crash, log LOUDLY + bump a Redis counter (Rule 12), rebuild the redis
    client (fresh DNS), back off, and RESTART the loop. A loop can no longer die.
    """
    backoff = 1
    while True:
        try:
            await coro_factory()
            log.warning("data_loop_exited_normally", loop=name)  # infinite loops shouldn't
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("data_loop_crashed_restarting", loop=name,
                      error=str(exc)[:200], backoff_s=backoff)
            try:
                redis_client.reinit()  # fresh pool → re-resolve `redis` hostname/IP
                redis_client.get().incr(f"data_feed:loop_restart:{name}")
            except Exception:
                pass
        await asyncio.sleep(backoff)
        backoff = min(30, backoff * 2)


async def _main_data_pipeline() -> None:
    """Run the legacy REST polling loop AND the new WS + account metrics
    loops concurrently. WS handles realtime mark prices; REST polling stays
    on as a safety net + provides 24h ticker / candles that aren't streamed.
    cont. 74 — each loop is supervised so a transient redis blip can't zombie it."""
    await asyncio.gather(
        _supervise(data_loop, "data_loop"),
        _supervise(_ws_mark_price_loop, "ws_mark"),
        _supervise(_ws_ticker_loop, "ws_ticker"),
        _supervise(_account_metrics_loop, "account_metrics"),
        return_exceptions=True,
    )


if __name__ == "__main__":
    import db
    db.init_pool()
    redis_client.init()
    asyncio.run(_main_data_pipeline())
