"""SciBrain Layer-0 SensorBus — build a SensorFrame for a symbol from live Redis.

Reads ONLY existing keys already populated by the bot (verified live 2026-06-08):
  {sym}:{tf}:candles          LIST of JSON {t,o,h,l,c,v} (strings), NEWEST-first
  {sym}:{tf}:candle_forecast  JSON {dir1,dir3,dir5,mag1,mag3,mag5,trend}
  {sym}:ofi {sym}:vpin        str float (TTL 60s)
  {sym}:funding_rate          str float
  {sym}:oi_change_5m {sym}:oi_change_z   str float
  {sym}:sentiment             str float (0..1)
No new data feeds. Every parse is defensive — a missing/garbled key leaves that
field None and the modules degrade on their own.
"""
from __future__ import annotations

import json

import numpy as np
import structlog

from .contracts import SensorFrame

log = structlog.get_logger()

# timeframes the SensorBus loads (modules pick what they need)
TFS = ("1m", "5m", "15m", "30m", "1h")
_MAX_CANDLES = 200
# the per-symbol scalar sensor keys build_frame reads (suffix after "{sym}:")
_SENSOR_SUFFIXES = ("ofi", "vpin", "funding_rate", "oi_change_5m", "oi_change_z", "sentiment")

# Process-global in-RAM universe snapshot (Phase 5). The parent bulk-loads every key build_frame
# needs ONCE per cycle, sets this, and forks the scorer pool so workers inherit it read-only via
# copy-on-write — eliminating ~16 per-symbol Redis round-trips × ~491 pairs each cycle. None ⇒ every
# read goes straight to Redis exactly as before (serial path / cache disabled). See bulk_load().
_SNAPSHOT_READER = None


class SnapshotReader:
    """Read-only Redis-shaped facade over an in-RAM {key: value} snapshot, with a live fallback.

    Exposes only the two ops build_frame uses — .get(key) and .lrange(key, start, end) — so the
    SAME parse path runs whether data comes from the snapshot dict or Redis ⇒ bit-identical frames.
    A key absent from the snapshot (e.g. a pair added mid-cycle) falls back to the live client, so
    the cache can never silently drop a symbol's data."""

    __slots__ = ("_snap", "_fallback")

    def __init__(self, snapshot: dict, fallback=None):
        self._snap = snapshot
        self._fallback = fallback

    def get(self, key: str):
        if key in self._snap:
            return self._snap[key]
        return self._fallback.get(key) if self._fallback is not None else None

    def lrange(self, key: str, start: int, end: int):
        if key in self._snap:
            v = self._snap[key]
            return v if v is not None else []
        return self._fallback.lrange(key, start, end) if self._fallback is not None else []


def set_snapshot(reader) -> None:
    """Install (or clear with None) the process-global snapshot reader. Set in the PARENT before
    forking the scorer pool so children inherit it via copy-on-write."""
    global _SNAPSHOT_READER
    _SNAPSHOT_READER = reader


def _reader(r):
    """The active read source: the inherited snapshot (fallback=r) if installed, else live r."""
    return _SNAPSHOT_READER if _SNAPSHOT_READER is not None else r


def bulk_load(r, symbols, fallback=None) -> "SnapshotReader":
    """ONE chunked-pipelined read of EVERY key build_frame touches for `symbols` → a SnapshotReader.

    Replays build_frame's exact read set (5 TFs × candles+forecast + 6 scalar sensors per symbol) in
    pipelines so the whole universe costs a handful of round-trips instead of ~16×N. Deterministic;
    never raises (a failed chunk just leaves those keys absent → live fallback)."""
    snap: dict = {}
    CHUNK = 64                                   # bound each pipeline's response size/latency
    syms = list(symbols)
    for i in range(0, len(syms), CHUNK):
        batch = syms[i:i + CHUNK]
        ops: list[str] = []                      # parallel list of keys, in pipeline order
        try:
            pipe = r.pipeline(transaction=False)
            for sym in batch:
                for tf in TFS:
                    pipe.lrange(f"{sym}:{tf}:candles", 0, _MAX_CANDLES - 1)
                    ops.append(f"{sym}:{tf}:candles")
                    pipe.get(f"{sym}:{tf}:candle_forecast")
                    ops.append(f"{sym}:{tf}:candle_forecast")
                for suf in _SENSOR_SUFFIXES:
                    pipe.get(f"{sym}:{suf}")
                    ops.append(f"{sym}:{suf}")
            results = pipe.execute()
            for key, val in zip(ops, results):
                snap[key] = val
        except Exception as exc:
            log.warning("scibrain_bulk_load_chunk_failed", error=str(exc)[:120], n=len(batch))
            continue
    return SnapshotReader(snap, fallback=fallback)


def _f(r, key: str):
    """Best-effort float GET; None on missing/garbled."""
    try:
        v = r.get(key)
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _candles(r, symbol: str, tf: str) -> np.ndarray:
    """Return an (N,6) float array [t,o,h,l,c,v], OLDEST-first. Empty array if none.

    Redis stores newest-first (LINDEX 0 = newest), so we reverse to give modules a
    normal forward time series (row -1 = newest)."""
    try:
        raw = r.lrange(f"{symbol}:{tf}:candles", 0, _MAX_CANDLES - 1)
    except Exception:
        return np.empty((0, 6), dtype=float)
    rows = []
    for item in raw:
        try:
            c = json.loads(item)
            rows.append((float(c["t"]), float(c["o"]), float(c["h"]),
                         float(c["l"]), float(c["c"]), float(c.get("v", 0.0))))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if not rows:
        return np.empty((0, 6), dtype=float)
    rows.reverse()                      # newest-first -> oldest-first
    return np.asarray(rows, dtype=float)


def _forecast(r, symbol: str, tf: str) -> dict | None:
    try:
        raw = r.get(f"{symbol}:{tf}:candle_forecast")
        return json.loads(raw) if raw else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def build_frame(r, symbol: str) -> SensorFrame:
    """Assemble a full SensorFrame for `symbol`. Reads via the in-RAM snapshot when one is installed
    (Phase 5 fork-COW cache), else straight from live Redis — identical parse path either way."""
    r = _reader(r)
    candles: dict[str, np.ndarray] = {}
    forecasts: dict[str, dict] = {}
    for tf in TFS:
        arr = _candles(r, symbol, tf)
        if len(arr):
            candles[tf] = arr
        fc = _forecast(r, symbol, tf)
        if fc is not None:
            forecasts[tf] = fc

    last_price = None
    for tf in ("1m", "5m", "15m"):
        arr = candles.get(tf)
        if arr is not None and len(arr):
            last_price = float(arr[-1, 4])
            break

    return SensorFrame(
        symbol=symbol,
        candles=candles,
        ofi=_f(r, f"{symbol}:ofi"),
        vpin=_f(r, f"{symbol}:vpin"),
        funding=_f(r, f"{symbol}:funding_rate"),
        oi_change_5m=_f(r, f"{symbol}:oi_change_5m"),
        oi_change_z=_f(r, f"{symbol}:oi_change_z"),
        sentiment=_f(r, f"{symbol}:sentiment"),
        cn_forecasts=forecasts,
        last_price=last_price,
    )
