"""
Shared historical-kline corpus — P1 of next_impl/kline_corpus_model_reframe.md (cont. 69f).

Persists OHLCV to data/historical/{SYMBOL}/{interval}.csv with header
`timestamp,open,high,low,close,volume` (timestamp = open_time ms) — the SAME store
candlenet (ml/candlenet.py:661) and pretrainer already read, so deepening/refreshing it
upgrades every kline-trained model with no reader changes.

Why this exists: the pre-existing store was SHALLOW (~1000-2000 rows/file) and STALE
(1m/5m/15m/30m ended mid-2024). Models trained on year-old, days-deep data → degenerate.
This module backfills DEEP history and keeps it CURRENT.

Usage:
  docker compose exec brain python -m ml.klines_corpus backfill --pairs active
  docker compose exec brain python -m ml.klines_corpus incremental --pairs active
  (or call backfill_pair / incremental_pair / update_corpus from a celery task)
"""
import csv
import os
import sys
import time
from pathlib import Path

import structlog

log = structlog.get_logger()

DATA_DIR = Path("data/historical")
HEADER = ["timestamp", "open", "high", "low", "close", "volume"]

# Target depth per TF (days). 1m is huge so kept shorter; longer TFs go deep for
# regime coverage. Tunable.
# cont. 69s — rolling-window depths. Fine TFs kept short (fresh, cheap, enough to
# span trades for SL/TP tuning + live signals); 1h kept deep for HMM/GNN regime work.
# A daily ban-immune bulk top-up (bulk_topup_pair) keeps ALL of these current.
DEFAULT_LOOKBACK_DAYS = {
    "1m": 90, "5m": 90, "15m": 120, "30m": 180, "1h": 365,
    # cont. 70 — 4h/1d added to the rolling refresh. They were frozen (4h stuck
    # at 2024-10, 1d ~18d behind) because the daily bulk-topup + backfill derive
    # their interval list from THIS dict. Coarse bars are cheap (few rows) so
    # deeper history is affordable: 4h=2y, 1d=5y for HTF regime context.
    "4h": 730, "1d": 1825,
}
_MS_DAY = 86_400_000


class MainnetKlines:
    """Public MAINNET kline source. The trading BinanceClient runs on TESTNET
    (BINANCE_TESTNET=1) whose history is sparse/stale (~1000 rows, mid-2024) —
    useless for a training corpus. Klines are a PUBLIC endpoint (no auth), so a
    keyless mainnet client gives full, current data. Light self-throttle keeps us
    under the public weight budget during the one-time deep backfill."""

    def __init__(self, sleep_between: float = 0.40):
        # cont. 69f: 0.40s/call ≈ ≤150 calls/min — well under the public futures
        # weight budget (2400/min; klines ~10 weight at limit 1500 → ~240/min cap).
        # The earlier 0.15s tripped an IP ban (-1003). Ban-aware retry below.
        from binance.client import Client
        self._c = Client()              # mainnet, keyless
        self._sleep = sleep_between

    def _one_call(self, pair, interval, start, end):
        """One futures_klines call, sleeping THROUGH a -1003 IP ban if hit."""
        import re as _re
        import time as _t
        for _attempt in range(6):
            try:
                return self._c.futures_klines(
                    symbol=pair, interval=interval,
                    startTime=start, endTime=end, limit=1500)
            except Exception as exc:
                msg = str(exc)
                if "-1003" in msg or "banned until" in msg:
                    m = _re.search(r"banned until (\d+)", msg)
                    until = int(m.group(1)) / 1000.0 if m else (_t.time() + 120)
                    wait = max(5.0, until - _t.time() + 5.0)
                    log.warning("corpus_ip_ban_wait", pair=pair, interval=interval,
                                wait_s=round(wait, 1))
                    _t.sleep(wait)
                    continue
                raise
        return []

    def get_historical_klines(self, pair, interval, start_ms, end_ms):
        # PAGINATE: futures_klines returns ≤limit candles from startTime and does
        # NOT auto-fill the range, so loop forward until end_ms (else deep windows
        # come back stale-ended). cont. 69f.
        import time as _t
        out: list = []
        cur, end = int(start_ms), int(end_ms)
        while cur < end:
            batch = self._one_call(pair, interval, cur, end)
            if not batch:
                break
            out.extend(batch)
            last = int(batch[-1][0])
            if last <= cur:
                break
            cur = last + 1
            if len(batch) < 1500:        # reached the present
                break
            if self._sleep:
                _t.sleep(self._sleep)
        return out


def _csv_path(pair: str, interval: str) -> Path:
    return DATA_DIR / pair / f"{interval}.csv"


def _read_existing(pair: str, interval: str) -> dict[int, list]:
    """Return {open_time_ms: [ts,o,h,l,c,v]} from the existing CSV (if any)."""
    p = _csv_path(pair, interval)
    out: dict[int, list] = {}
    if not p.exists():
        return out
    try:
        with open(p, newline="") as f:
            rd = csv.reader(f)
            next(rd, None)  # header
            for row in rd:
                if len(row) >= 6:
                    try:
                        out[int(row[0])] = row[:6]
                    except (TypeError, ValueError):
                        continue
    except Exception as exc:
        log.warning("corpus_read_failed", pair=pair, interval=interval,
                    error=str(exc)[:120])
    return out


def _write_atomic(pair: str, interval: str, rows_by_ts: dict[int, list]) -> None:
    p = _csv_path(pair, interval)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for ts in sorted(rows_by_ts):
            w.writerow(rows_by_ts[ts])
    os.replace(tmp, str(p))


def _fetch(client, pair: str, interval: str,
           start_ms: int, end_ms: int) -> dict[int, list]:
    """Pull klines [start,end] (the client paginates internally). Returns
    {open_time_ms: [ts,o,h,l,c,v]}."""
    kl = client.get_historical_klines(pair, interval, start_ms, end_ms)
    out: dict[int, list] = {}
    for k in (kl or []):
        try:
            ts = int(k[0])
            out[ts] = [ts, k[1], k[2], k[3], k[4], k[5]]
        except (TypeError, ValueError, IndexError):
            continue
    return out


def backfill_pair(client, pair: str, intervals=None,
                  lookback_days: dict | None = None) -> dict:
    """Fetch the full target window per interval, MERGE with existing (dedup by
    open_time), trim to the window, write. Refreshes stale files + deepens."""
    intervals = intervals or list(DEFAULT_LOOKBACK_DAYS)
    lb = lookback_days or DEFAULT_LOOKBACK_DAYS
    now = int(time.time() * 1000)
    report = {}
    for itv in intervals:
        days = lb.get(itv, 120)
        window_start = now - days * _MS_DAY
        existing = _read_existing(pair, itv)
        try:
            fetched = _fetch(client, pair, itv, window_start, now)
        except Exception as exc:
            log.warning("corpus_backfill_fetch_failed", pair=pair,
                        interval=itv, error=str(exc)[:150])
            report[itv] = {"error": str(exc)[:80]}
            continue
        existing.update(fetched)                       # new overwrites/extends
        merged = {ts: v for ts, v in existing.items() if ts >= window_start}
        if merged:
            _write_atomic(pair, itv, merged)
        last_ts = max(merged) if merged else 0
        report[itv] = {"rows": len(merged), "fetched": len(fetched),
                       "last_ts": last_ts}
    return report


def incremental_pair(client, pair: str, intervals=None) -> dict:
    """Append only candles since the last stored open_time (cheap top-up)."""
    intervals = intervals or list(DEFAULT_LOOKBACK_DAYS)
    now = int(time.time() * 1000)
    report = {}
    for itv in intervals:
        existing = _read_existing(pair, itv)
        if not existing:
            # nothing yet → fall back to a backfill of this interval
            report[itv] = backfill_pair(client, pair, [itv]).get(itv, {})
            continue
        last = max(existing)
        try:
            fetched = _fetch(client, pair, itv, last + 1, now)
        except Exception as exc:
            log.warning("corpus_incremental_fetch_failed", pair=pair,
                        interval=itv, error=str(exc)[:150])
            report[itv] = {"error": str(exc)[:80]}
            continue
        if fetched:
            existing.update(fetched)
            # keep the window bounded to the interval's target depth
            cutoff = now - DEFAULT_LOOKBACK_DAYS.get(itv, 120) * _MS_DAY
            merged = {ts: v for ts, v in existing.items() if ts >= cutoff}
            _write_atomic(pair, itv, merged)
            report[itv] = {"added": len(fetched), "rows": len(merged)}
        else:
            report[itv] = {"added": 0, "rows": len(existing)}
    return report


# ─────────────────────────────────────────────────────────────────────────────
# WS-klines corpus path (cont. 69x) — ZERO fapi REST. data/kline_ws.py streams
# CLOSED mainnet candles into Redis sorted sets `klines:ws:{pair}:{itv}` (score =
# open_time_ms, member = JSON [ts,o,h,l,c,v]). This merges them into the same CSV
# corpus as the REST path, byte-identical, so it retires the hourly /fapi/v1/klines
# top-up hammer. Coarse TFs (4h/1d) are left to the daily data.binance.vision bulk —
# a WS close for those is hours/days apart, useless for hourly freshness.
# ─────────────────────────────────────────────────────────────────────────────
WS_INTERVALS = ["1m", "5m", "15m", "30m", "1h"]


def incremental_from_ws(which: str = "active", r=None) -> dict:
    """Top up the corpus from WS-collected closed candles in Redis — no REST.

    Mirrors incremental_pair's read→merge→trim→write but sources candles from the
    klines:ws:{pair}:{itv} sorted sets data/kline_ws.py maintains. Pairs/TFs with no
    WS data yet are skipped (the daily bulk keeps them current to ~T-1)."""
    import json as _json
    if r is None:
        import redis_client
        r = redis_client.get()
    pairs = _resolve_pairs(which)
    now = int(time.time() * 1000)
    ok = added_total = skipped = 0
    t0 = time.time()
    for pair in pairs:
        for itv in WS_INTERVALS:
            key = f"klines:ws:{pair}:{itv}"
            try:
                existing = _read_existing(pair, itv)
                last = max(existing) if existing else 0
                members = r.zrangebyscore(key, last + 1, now)
                if not members:
                    skipped += 1
                    continue
                added = 0
                for m in members:
                    try:
                        row = _json.loads(m)
                        ts = int(row[0])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if ts <= last:
                        continue
                    existing[ts] = [ts, row[1], row[2], row[3], row[4], row[5]]
                    added += 1
                if not added:
                    skipped += 1
                    continue
                cutoff = now - DEFAULT_LOOKBACK_DAYS.get(itv, 120) * _MS_DAY
                merged = {ts: v for ts, v in existing.items() if ts >= cutoff}
                _write_atomic(pair, itv, merged)
                ok += 1
                added_total += added
            except Exception as exc:
                log.warning("corpus_ws_pair_failed", pair=pair, interval=itv,
                            error=str(exc)[:120])
    res = {"mode": "incremental_ws", "pairs": len(pairs), "tf_written": ok,
           "tf_skipped": skipped, "added": added_total,
           "elapsed_s": round(time.time() - t0, 1)}
    log.info("corpus_ws_update_complete", **res)
    return res


# ─────────────────────────────────────────────────────────────────────────────
# BULK backfill via data.binance.vision (cont. 69h) — NO rate limits (static CDN).
# REST (-1003) bans on deep history; the public data dumps are the ban-free way to
# get years of klines. Monthly ZIPs cover completed months; daily ZIPs the current.
# ─────────────────────────────────────────────────────────────────────────────
_BULK_BASE = "https://data.binance.vision/data/futures/um"


def _normalize_ts_ms(ts: int) -> int:
    """data.binance.vision switched some files to MICROSECOND open_time in 2025.
    Normalise to ms (current ms ~1.78e12; µs ~1.78e15)."""
    return ts // 1000 if ts > 1e14 else ts


def _bulk_rows(url: str):
    """Download+unzip+parse one kline ZIP → list of [ts_ms,o,h,l,c,v]. None on 404."""
    import io
    import urllib.request
    import urllib.error
    import zipfile
    try:
        with urllib.request.urlopen(url, timeout=90) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception as exc:
        log.debug("bulk_download_failed", url=url[-60:], error=str(exc)[:100])
        return None
    out = []
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        with z.open(z.namelist()[0]) as f:
            for line in io.TextIOWrapper(f, "utf-8"):
                p = line.rstrip("\n").split(",")
                if len(p) < 6 or not p[0].lstrip("-").isdigit():
                    continue                       # skip header / bad lines
                ts = _normalize_ts_ms(int(p[0]))
                out.append([ts, p[1], p[2], p[3], p[4], p[5]])
    except Exception as exc:
        log.debug("bulk_parse_failed", url=url[-60:], error=str(exc)[:100])
        return None
    return out


def _months_back(n: int):
    from datetime import datetime, timezone
    d = datetime.now(timezone.utc)
    y, m = d.year, d.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y -= 1
            m = 12
    return out


def bulk_backfill_pair(pair: str, intervals=None,
                       lookback_days: dict | None = None) -> dict:
    """Deep backfill one pair from data.binance.vision. Monthly ZIPs for completed
    months + daily ZIPs for the current (unpublished) month. Merge → corpus CSV."""
    import time as _t
    from datetime import datetime, timezone, timedelta
    intervals = intervals or list(DEFAULT_LOOKBACK_DAYS)
    lb = lookback_days or DEFAULT_LOOKBACK_DAYS
    now_ms = int(_t.time() * 1000)
    today = datetime.now(timezone.utc).date()
    report = {}
    for itv in intervals:
        days = lb.get(itv, 120)
        window_start = now_ms - days * _MS_DAY
        existing = _read_existing(pair, itv)
        n_months = days // 28 + 2
        got = 0
        # completed months (skip the current month → only in daily files)
        for (y, m) in _months_back(n_months):
            if (y, m) == (today.year, today.month):
                continue
            url = f"{_BULK_BASE}/monthly/klines/{pair}/{itv}/{pair}-{itv}-{y:04d}-{m:02d}.zip"
            rows = _bulk_rows(url)
            if rows:
                for r in rows:
                    existing[r[0]] = r
                got += len(rows)
        # current month → daily files (day 1 .. today)
        for dd in range(1, today.day + 1):
            day = today.replace(day=dd)
            url = f"{_BULK_BASE}/daily/klines/{pair}/{itv}/{pair}-{itv}-{day:%Y-%m-%d}.zip"
            rows = _bulk_rows(url)
            if rows:
                for r in rows:
                    existing[r[0]] = r
                got += len(rows)
        merged = {ts: v for ts, v in existing.items() if ts >= window_start}
        if merged:
            _write_atomic(pair, itv, merged)
        report[itv] = {"rows": len(merged),
                       "last_ts": max(merged) if merged else 0, "fetched": got}
    return report


def bulk_topup_pair(pair: str, intervals=None, days_back: int = 3,
                    lookback_days: dict | None = None) -> dict:
    """cont. 69s — ban-IMMUNE daily top-up from data.binance.vision DAILY zips.
    Pulls only the last `days_back`+today daily files per interval (cheap: 1-4
    files/itv), merges into the corpus CSV, trims to the rolling window. Unlike
    incremental_pair (REST → blocked during fapi -1003 bans) this uses the static
    bulk CDN, so it keeps ALL timeframes current regardless of fapi ban status."""
    import time as _t
    from datetime import datetime, timezone, timedelta
    intervals = intervals or list(DEFAULT_LOOKBACK_DAYS)
    lb = lookback_days or DEFAULT_LOOKBACK_DAYS
    now_ms = int(_t.time() * 1000)
    today = datetime.now(timezone.utc).date()
    report = {}
    for itv in intervals:
        existing = _read_existing(pair, itv)
        got = 0
        for dd in range(days_back, -1, -1):
            day = today - timedelta(days=dd)
            url = f"{_BULK_BASE}/daily/klines/{pair}/{itv}/{pair}-{itv}-{day:%Y-%m-%d}.zip"
            rows = _bulk_rows(url)
            if rows:
                for r in rows:
                    existing[r[0]] = r
                got += len(rows)
        window_start = now_ms - lb.get(itv, 120) * _MS_DAY
        merged = {ts: v for ts, v in existing.items() if ts >= window_start}
        if merged:
            _write_atomic(pair, itv, merged)
        report[itv] = {"rows": len(merged),
                       "last_ts": max(merged) if merged else 0, "fetched": got}
    return report


def _resolve_pairs(which: str) -> list[str]:
    if which == "active":
        try:
            import redis_client
            ps = redis_client.get().smembers("scanner:active_pairs")
            return sorted(p.decode() if isinstance(p, bytes) else p for p in ps)
        except Exception:
            return []
    if which == "all":
        return sorted(d.name for d in DATA_DIR.iterdir()
                      if d.is_dir() and d.name.endswith("USDT"))
    return [p for p in which.split(",") if p]


def update_corpus(mode: str = "incremental", which: str = "active",
                  client=None) -> dict:
    """Entry point for both the celery task and CLI. mode ∈ {backfill, incremental}."""
    pairs = _resolve_pairs(which)
    is_bulk = mode in ("bulk", "bulk_topup")   # bulk modes take pair-only (no REST client)
    if not is_bulk and client is None:
        # MAINNET public klines — NOT the testnet trading client (see MainnetKlines).
        client = MainnetKlines()
    fn = (bulk_topup_pair if mode == "bulk_topup"
          else bulk_backfill_pair if mode == "bulk"
          else backfill_pair if mode == "backfill" else incremental_pair)
    ok, failed, t0 = 0, 0, time.time()
    for i, pair in enumerate(pairs, 1):
        try:
            fn(pair) if is_bulk else fn(client, pair)
            ok += 1
        except Exception as exc:
            failed += 1
            log.warning("corpus_pair_failed", pair=pair, error=str(exc)[:120])
        if i % 25 == 0:
            log.info("corpus_progress", mode=mode, done=i, total=len(pairs),
                     ok=ok, failed=failed, elapsed_s=round(time.time() - t0, 1))
    res = {"mode": mode, "pairs": len(pairs), "ok": ok, "failed": failed,
           "elapsed_s": round(time.time() - t0, 1)}
    log.info("corpus_update_complete", **res)
    return res


if __name__ == "__main__":
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    _mode = "incremental"
    if len(sys.argv) > 1 and sys.argv[1] in ("backfill", "bulk", "incremental"):
        _mode = sys.argv[1]
    _which = "active"
    if "--pairs" in sys.argv:
        _which = sys.argv[sys.argv.index("--pairs") + 1]
    print(update_corpus(_mode, _which))
