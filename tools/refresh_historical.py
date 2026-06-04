"""cont. 65k Step 1 — refresh historical CSVs with DEEP REAL mainnet data.

Why: the bot trades on Binance TESTNET, whose kline history is seeded/thin
(~1000 rows of recent synthetic data) — too shallow + low-quality to train good
models (AUC stuck ~0.56). But market DATA needs no auth, so we pull REAL
mainnet klines (fapi.binance.com futures, spot fallback) with PAGINATION to get
months of genuine market structure, while trading stays on testnet. This is the
"fall back to a source with latest+deep data" fix.

Owner Q (cont. 65k): "testnet/binance limit — check or fall back to other
sources with latest data." Answer: testnet caps ~1000 rows; mainnet public API
paginates to arbitrary depth. Source order per symbol: mainnet-futures →
mainnet-spot. Symbols absent on mainnet are skipped (kept as-is).

Run inside any bot container (needs internet + the data/historical bind-mount,
now world-writable). Idempotent: only overwrites when the fetch returns more/
fresher data than the existing CSV.
"""
from __future__ import annotations
import csv
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import structlog

log = structlog.get_logger()

DATA_DIR = Path("/app/data/historical")
# Skip 1m in the deep refresh — heaviest (130k candles/90d) and noisiest; the
# cascade votes on 5m/15m (dir3). 1m keeps its existing CSV.
INTERVALS = ["5m", "15m", "30m", "1h"]
FETCH_DAYS = int(os.environ.get("REFRESH_FETCH_DAYS", "45"))   # depth of real history
PAGE = 1500                                                    # mainnet max per call
FUTURES = "https://fapi.binance.com/fapi/v1/klines"
SPOT = "https://api.binance.com/api/v3/klines"


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def _paginate(base: str, sym: str, itv: str, start_ms: int, end_ms: int) -> list:
    out, cur = [], start_ms
    for _ in range(400):  # hard cap on pages
        url = f"{base}?symbol={sym}&interval={itv}&startTime={cur}&endTime={end_ms}&limit={PAGE}"
        try:
            k = _get(url)
        except Exception:
            break
        if not k:
            break
        out += k
        cur = k[-1][0] + 1
        if len(k) < PAGE:
            break
        time.sleep(0.12)   # respect rate limits
    return out


def _existing_count(path: Path) -> int:
    try:
        with open(path) as f:
            return sum(1 for _ in f) - 1
    except Exception:
        return 0


def refresh() -> None:
    pairs = sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir())
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = now - int(FETCH_DAYS * 86400 * 1000)
    log.info("refresh_start", pairs=len(pairs), intervals=INTERVALS,
             fetch_days=FETCH_DAYS, source="mainnet-futures→spot")
    refreshed = nodata = failed = 0

    for n, pair in enumerate(pairs):
        for interval in INTERVALS:
            out = DATA_DIR / pair / f"{interval}.csv"
            kl = _paginate(FUTURES, pair, interval, start, now)
            if not kl:                              # futures miss → spot fallback
                kl = _paginate(SPOT, pair, interval, start, now)
            if not kl:
                nodata += 1
                continue
            # Only overwrite if we got meaningfully MORE than what's there.
            if len(kl) <= _existing_count(out) and len(kl) < 1100:
                continue
            try:
                with open(out, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                    for k in kl:
                        w.writerow([k[0], k[1], k[2], k[3], k[4], k[5]])
                refreshed += 1
            except Exception as exc:
                failed += 1
                log.warning("refresh_write_failed", pair=pair, interval=interval,
                            error=str(exc)[:120])
        if (n + 1) % 25 == 0:
            log.info("refresh_progress", done=n + 1, total=len(pairs),
                     refreshed=refreshed, nodata=nodata, failed=failed)

    log.info("refresh_complete", refreshed=refreshed, nodata=nodata, failed=failed)


if __name__ == "__main__":
    refresh()
