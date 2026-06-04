"""Layer 1 — Microstructure Jump Detector (cont. 65k).

The candle cascade (Layer 2) predicts direction over minutes-hours but is LAGGING —
it cannot catch sudden moves before they happen. Research (arXiv 2602.00776, cross-
asset crypto microstructure) shows the top short-horizon predictors are, in order:
  1. L1 order-flow imbalance   (bid vs ask resting size, top levels)
  2. bid-ask spread
  3. VWAP-to-mid deviation
and that these patterns are STABLE across assets & regimes ("irrespective of market").
During the 2025-10-10 flash crash the OFI signal "reached unprecedented magnitudes and
correctly identified the directional collapse" — i.e. it caught the move as it began.

This module computes those features from the REAL Binance MAINNET order book (depth needs
no auth; trading stays on testnet) and derives an early-warning JUMP SCORE + direction
that Layer 2 / the engine consume to TIME and GATE entries.

Outputs per pair (Redis, TTL 30s — short so stale microstructure never gates a trade):
  {pair}:micro:ofi_l1        signed top-N order-flow imbalance  [-1, 1]  (+ = bid pressure)
  {pair}:micro:spread        relative spread (ask-bid)/mid
  {pair}:micro:ofi_accel     OFI minus previous OFI (imbalance building same-sign = jump)
  {pair}:micro:jump_score    0-100 early-warning strength
  {pair}:micro:direction     "long" / "short" / "flat"
  {pair}:micro:ts            epoch of computation
State: {pair}:micro:ofi_prev (for the acceleration term).

v1 uses REST depth on a beat cadence; a WebSocket depth stream (true tick-level) is the
v2 upgrade. Hawkes order-flow clustering + change-point detection are v2 enhancements —
the v1 jump score already encodes "large + building one-sided imbalance with a tight
spread" which is the actionable core of the Hawkes early-warning.
"""
from __future__ import annotations
import json
import time
import urllib.request

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

_FUT_DEPTH = "https://fapi.binance.com/fapi/v1/depth?symbol={sym}&limit={lim}"
_SPOT_DEPTH = "https://api.binance.com/api/v3/depth?symbol={sym}&limit={lim}"
_LEVELS = 10            # top-N book levels for the imbalance sum (weight-2 call)
# TTL must exceed the real scan cadence (every ~15s scheduled, but ~25-30s under
# worker contention) so signals persist between scans; the engine still checks
# {pair}:micro:ts for freshness before acting, so a stale key never gates a trade.
_TTL = 90              # seconds

# Jump-score thresholds (tunable via Redis micro:cfg:* later).
_OFI_STRONG = 0.35     # |OFI| above this = meaningful one-sided pressure
_SPREAD_WIDE = 0.0015  # relative spread above this = thin/illiquid → discount


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    return json.loads(urllib.request.urlopen(req, timeout=8).read())


def _depth(sym: str):
    """Top-N L1 depth from mainnet futures, spot fallback. Returns (bids, asks)."""
    for base in (_FUT_DEPTH, _SPOT_DEPTH):
        try:
            d = _get(base.format(sym=sym, lim=_LEVELS))
            b, a = d.get("bids"), d.get("asks")
            if b and a:
                return b, a
        except Exception:
            continue
    return None, None


def compute_book_features(bids, asks, prev_ofi: float | None) -> dict | None:
    """Pure microstructure feature math from a top-N order book snapshot.

    Shared by the REST poller (compute_pair) and the WebSocket streamer
    (data/micro_ws.py) so both paths produce byte-identical features. `bids`
    and `asks` are [[price, qty], ...] (strings or floats); `prev_ofi` is the
    last OFI for the acceleration term (None on first observation → 0 accel).
    Returns the feature dict or None when the book is unusable.
    """
    if not bids or not asks:
        return None
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return None
        bid_q = sum(float(b[1]) for b in bids)
        ask_q = sum(float(a[1]) for a in asks)
        denom = bid_q + ask_q
        if denom <= 0:
            return None
        ofi = (bid_q - ask_q) / denom                       # [-1, 1]
        spread = (best_ask - best_bid) / mid
        # Depth-weighted VWAP-to-mid: where is the resting liquidity centered vs mid.
        vwap = (sum(float(b[0]) * float(b[1]) for b in bids) +
                sum(float(a[0]) * float(a[1]) for a in asks)) / denom
        vwap_dev = (vwap - mid) / mid
    except Exception:
        return None

    prev = prev_ofi if prev_ofi is not None else ofi
    ofi_accel = ofi - prev

    # Jump score: large one-sided imbalance THAT IS BUILDING (accel same sign as OFI),
    # discounted when the spread is wide (thin book = unreliable). 0-100.
    building = 1.0 if (ofi * ofi_accel) > 0 else 0.4      # same-sign accel = building
    spread_factor = 1.0 if spread < _SPREAD_WIDE else max(0.3, _SPREAD_WIDE / spread)
    raw = min(1.0, abs(ofi) / _OFI_STRONG) * building * spread_factor
    # VWAP-dev agreement nudges it up when liquidity skews the same way as OFI.
    if (ofi > 0 and vwap_dev > 0) or (ofi < 0 and vwap_dev < 0):
        raw = min(1.0, raw * 1.1)
    jump_score = round(raw * 100.0, 2)

    if abs(ofi) < 0.15:
        direction = "flat"
    else:
        direction = "long" if ofi > 0 else "short"

    return {"ofi": round(ofi, 4), "spread": round(spread, 6),
            "ofi_accel": round(ofi_accel, 4), "vwap_dev": round(vwap_dev, 6),
            "jump_score": jump_score, "direction": direction}


def write_micro(r, pair: str, feats: dict) -> None:
    """Write a computed feature dict to the canonical {pair}:micro:* keys.

    Identical key/TTL layout for both REST and WebSocket producers so the
    consume side (signals/engine.py micro veto, frontier exits) is agnostic to
    which producer last refreshed the pair. ofi_prev is persisted without TTL
    as the acceleration state for the next observation.
    """
    now = int(time.time())
    try:
        pipe = r.pipeline()
        pipe.setex(f"{pair}:micro:ofi_l1", _TTL, feats["ofi"])
        pipe.setex(f"{pair}:micro:spread", _TTL, feats["spread"])
        pipe.setex(f"{pair}:micro:ofi_accel", _TTL, feats["ofi_accel"])
        pipe.setex(f"{pair}:micro:vwap_dev", _TTL, feats["vwap_dev"])
        pipe.setex(f"{pair}:micro:jump_score", _TTL, feats["jump_score"])
        pipe.setex(f"{pair}:micro:direction", _TTL, feats["direction"])
        pipe.setex(f"{pair}:micro:ts", _TTL, now)
        pipe.set(f"{pair}:micro:ofi_prev", feats["ofi"])    # state (no TTL)
        pipe.execute()
    except Exception as exc:
        log.debug("micro_write_failed", pair=pair, error=str(exc)[:120])


def compute_pair(pair: str, r) -> dict | None:
    """REST path: pull top-N depth, compute features, write to Redis."""
    bids, asks = _depth(pair)
    if not bids or not asks:
        return None
    prev_raw = r.get(f"{pair}:micro:ofi_prev")
    try:
        prev = float(prev_raw) if prev_raw is not None else None
    except (TypeError, ValueError):
        prev = None
    feats = compute_book_features(bids, asks, prev)
    if feats is None:
        return None
    write_micro(r, pair, feats)
    return {"pair": pair, "ofi": feats["ofi"], "spread": feats["spread"],
            "jump_score": feats["jump_score"], "direction": feats["direction"]}


def scan_all(max_pairs: int = 0) -> dict:
    """Scan the live scanner universe (dynamic) and write microstructure for each.

    Reads scanner:active_pairs at call time so it always reflects the current
    universe (100 today, 200 if reconfigured). max_pairs>0 caps the scan (e.g.
    to the shortlist) for rate-limit safety; 0 = all.

    cont. 69x — production-fapi WEIGHT THROTTLE (rotated-IP protection). This REST
    scan hits mainnet `fapi.binance.com/depth` (no auth) for EVERY active pair on a
    15s beat (~256 weight/scan → ~1 kweight/min), which alone walks the freshly
    rotated IP back toward a -1003 ban. micro_ws (WS producer) already keeps the top
    `micro:ws:max_pairs` fresh with zero REST, so this scan only needs the long tail.
    Levers (all Redis-tunable, reversible, default = previous behaviour unless set):
      micro:rest:enabled        "0" → skip entirely (kill switch)        [default 1]
      micro:rest:min_interval_s  rate-limit beat firings (s)             [default 0]
      micro:rest:fresh_skip_s    skip pairs whose :micro:ts is younger   [default 20]
                                 than this (WS-covered + recently REST'd)
      micro:rest:max_pairs       hard cap on pairs scanned per call      [default 0=arg]
    """
    r = redis_client.get()
    fresh_skip_s, cap = 20.0, max_pairs
    try:
        if (r.get("micro:rest:enabled") or "1") != "1":
            log.info("micro_scan_skipped", reason="micro:rest:enabled=0")
            return {"status": "disabled", "scanned": 0}
        min_iv = float(r.get("micro:rest:min_interval_s") or 0)
        if min_iv > 0:
            last = r.get("micro:rest:last_run_ts")
            if last is not None and (time.time() - float(last)) < min_iv:
                log.info("micro_scan_skipped", reason="min_interval", min_iv=min_iv)
                return {"status": "throttled", "scanned": 0}
        fresh_skip_s = float(r.get("micro:rest:fresh_skip_s") or 20)
        cap = int(r.get("micro:rest:max_pairs") or 0) or max_pairs
    except Exception:
        pass

    try:
        raw = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
        pairs = sorted((x.decode() if isinstance(x, bytes) else x) for x in raw)
    except Exception:
        pairs = []

    # Skip pairs already fresh — the WS producer covers the top pairs with zero REST,
    # and a recently REST'd pair self-rate-limits to once per fresh_skip_s. Bulk-read
    # :micro:ts in one pipeline to keep the freshness check cheap.
    n_skipped_fresh = 0
    if fresh_skip_s > 0 and pairs:
        now = time.time()
        try:
            pipe = r.pipeline()
            for p in pairs:
                pipe.get(f"{p}:micro:ts")
            ts_vals = pipe.execute()
        except Exception:
            ts_vals = [None] * len(pairs)
        kept = []
        for p, ts in zip(pairs, ts_vals):
            try:
                if ts is not None and (now - float(ts)) < fresh_skip_s:
                    continue
            except (TypeError, ValueError):
                pass
            kept.append(p)
        n_skipped_fresh = len(pairs) - len(kept)
        pairs = kept

    if cap and len(pairs) > cap:
        pairs = pairs[:cap]

    n_ok = n_jump = 0
    for p in pairs:
        res = compute_pair(p, r)
        if res:
            n_ok += 1
            if res["jump_score"] >= 50:
                n_jump += 1
    try:
        r.setex("micro:scan:last_ok", 120, n_ok)
        r.setex("micro:scan:last_jumps", 120, n_jump)
        r.setex("micro:rest:skipped_fresh", 120, n_skipped_fresh)
        r.set("micro:rest:last_run_ts", time.time())
    except Exception:
        pass
    log.info("micro_scan_complete", scanned=len(pairs),
             skipped_fresh=n_skipped_fresh, cap=cap, ok=n_ok, jumps=n_jump)
    return {"status": "ok", "scanned": len(pairs),
            "skipped_fresh": n_skipped_fresh, "ok": n_ok, "jumps": n_jump}
