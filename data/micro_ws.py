"""data/micro_ws.py — Microstructure v2: tick-level order-book stream (partial-depth).

The REST poller (signals/microstructure.scan_all, celery `microstructure-scan`) gives a
coarse ~15s snapshot of L1 order-flow imbalance. For short-horizon (15-/30-min) holding
the OFI signal is only as good as its refresh rate. This service keeps the same
`{pair}:micro:*` features continuously fresh at sub-second cadence from Binance USDT-M
futures order-book streams.

cont. 69x — MIGRATED from diff-depth (`@depth@100ms`) to PARTIAL BOOK DEPTH
(`@depth20@500ms`). Why:
  * Partial book depth pushes the FULL top-N book every interval → SELF-CONTAINED. No REST
    snapshot to seed, no U/u/pu sequence state machine, no resync. This REMOVES all
    production fapi.binance.com REST weight from the microstructure path. (Binance
    market-data WS streams do NOT consume the /fapi/v1 request-weight pool that triggers
    -1003 IP bans — they are connection-limited only: <=1024 streams/conn, 24h life.)
  * Lower volume (500ms vs 100ms) + no blocking REST snapshots inside the loop fixes the
    keepalive-ping-timeout flapping the diff-depth version suffered.
Features are computed by the SHARED signals/microstructure functions (compute_book_features /
write_micro), so the REST and WS paths stay byte-identical on the consume side (engine micro
veto, frontier exits). The REST `microstructure-scan` beat remains a THROTTLED fallback
(signals/microstructure.scan_all, micro:rest:* levers); whichever path wrote most recently
wins and the consumer only trusts a 90s freshness window via `{pair}:micro:ts`.

Config (Redis):
  micro:ws:enabled            "0" disables (sleep-loops)                 [default 1]
  micro:ws:max_pairs          0 = ALL active pairs; N = top-N by 24h vol [default 0]
  micro:ws:write_interval_ms  per-pair feature-write throttle            [default 500]

Run as docker-compose service `micro_ws`: `python -m data.micro_ws`.
"""
from __future__ import annotations

import asyncio
import json
import time

import structlog
import websockets

import redis_client
import redis_keys
from signals.microstructure import compute_book_features, write_micro, _LEVELS

log = structlog.get_logger()

_WS_BASE      = "wss://fstream.binance.com/stream?streams="
_DEPTH_LEVELS = 20                                  # partial-book levels (5/10/20 valid)
_DEPTH_SPEED  = f"@depth{_DEPTH_LEVELS}@500ms"      # self-contained top-N snapshot, 500ms


def _cfg_int(r, key: str, default: int) -> int:
    try:
        v = r.get(key)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _select_pairs(r, max_pairs: int) -> list[str]:
    """Active pairs by 24h volume (most-informative books first). max_pairs<=0 = all."""
    try:
        raw = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
        pairs = [(x.decode() if isinstance(x, bytes) else x) for x in raw]
    except Exception:
        return []

    def _vol(p: str) -> float:
        try:
            return float(r.get(redis_keys.TICKER_VOLUME_24H.replace("{pair}", p)) or 0)
        except (TypeError, ValueError):
            return 0.0

    pairs.sort(key=_vol, reverse=True)
    return pairs[:max_pairs] if max_pairs > 0 else pairs


async def _run_stream(pairs: list[str], stop_after_s: float) -> None:
    """Stream partial-depth books for `pairs` until stop_after_s or the socket drops.

    Each partial-depth message is the full top-N book (fields `b`/`a`, bids high->low,
    asks low->high — same ordering as REST depth), so it feeds compute_book_features
    directly; no local book state or snapshot is needed.
    """
    r = redis_client.get()
    prev_ofi: dict[str, float] = {}
    last_write: dict[str, float] = {}
    write_interval = _cfg_int(r, "micro:ws:write_interval_ms", 500) / 1000.0

    streams = "/".join(f"{p.lower()}{_DEPTH_SPEED}" for p in pairs)
    url = _WS_BASE + streams
    started = time.time()

    # ping_timeout generous so a brief Redis-write burst can't trip a false keepalive
    # timeout (the diff-depth flap cause); no REST snapshot work blocks the loop now.
    async with websockets.connect(url, ping_interval=20, ping_timeout=60,
                                  max_queue=4096, close_timeout=5) as ws:
        log.info("micro_ws_connected", pairs=len(pairs), stream=_DEPTH_SPEED)
        n_written = 0
        async for raw in ws:
            if time.time() - started > stop_after_s:
                break
            try:
                msg = json.loads(raw)
                ev = msg.get("data") or msg
                sym = (ev.get("s") or "").upper()
                if not sym:
                    sym = msg.get("stream", "").split("@", 1)[0].upper()
            except Exception:
                continue
            bids = ev.get("b")
            asks = ev.get("a")
            if not bids or not asks:
                continue

            now = time.time()
            if now - last_write.get(sym, 0.0) < write_interval:
                continue

            # Slice to _LEVELS so WS features are byte-identical to the REST path
            # (signals/microstructure._depth uses limit=_LEVELS).
            feats = compute_book_features(bids[:_LEVELS], asks[:_LEVELS],
                                          prev_ofi.get(sym))
            if feats is None:
                continue
            write_micro(r, sym, feats)
            prev_ofi[sym] = feats["ofi"]
            last_write[sym] = now
            n_written += 1
            if n_written % 500 == 0:
                try:
                    r.setex("micro:ws:writes", 120, n_written)
                except Exception:
                    pass
        log.info("micro_ws_stream_ended", writes=n_written)


async def main() -> None:
    r = redis_client.get()
    # Rotate the connection every ~12h (well under Binance's 24h forced disconnect)
    # so the pair universe stays current.
    rotate_s = 12 * 3600
    while True:
        if (r.get("micro:ws:enabled") or "1") != "1":
            log.info("micro_ws_disabled_sleeping")
            await asyncio.sleep(60)
            continue
        max_pairs = _cfg_int(r, "micro:ws:max_pairs", 0)   # 0 = all active pairs
        pairs = _select_pairs(r, max_pairs)
        if not pairs:
            log.warning("micro_ws_no_pairs")
            await asyncio.sleep(30)
            continue
        try:
            await _run_stream(pairs, stop_after_s=rotate_s)
        except Exception as exc:
            log.warning("micro_ws_stream_error", error=str(exc)[:200])
            await asyncio.sleep(5)          # backoff before reconnect


if __name__ == "__main__":
    asyncio.run(main())
