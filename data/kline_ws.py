"""data/kline_ws.py — WS klines corpus feed (cont. 69x).

Streams CLOSED mainnet candles for active pairs x {1m,5m,15m,30m,1h} from Binance
USDT-M futures and writes them to Redis sorted sets `klines:ws:{pair}:{itv}`
(score = open_time_ms, member = JSON [ts,o,h,l,c,v]). ml.klines_corpus.incremental_from_ws
merges these into the same CSV training corpus as the REST path — byte-identical — with
ZERO fapi REST, retiring the hourly /fapi/v1/klines top-up hammer (~1500 weight/min) that
re-walked the IP toward a -1003 ban.

Binance market-data WS streams do NOT consume the /fapi/v1 request-weight pool (they are
connection-limited only). Coarse TFs (4h/1d) stay on the daily data.binance.vision bulk —
a WS close for those is hours/days apart, useless for hourly freshness.

Uses the SUBSCRIBE control method (not a giant ?streams= URL) because 100+ pairs x 5 TFs is
600+ streams; chunked SUBSCRIBE stays well under the 10 control-msgs/sec limit and the
1024-streams/connection cap.

Config (Redis):
  klines:ws:enabled        "0" disables (sleep-loops)                 [default 1]
  klines:ws:max_pairs      0 = all active pairs; N = top-N by 24h vol [default 0]
  klines:ws:keep           closed candles retained per pair/interval  [default 500]
  klines:ws:resubscribe_s  re-read active set + SUBSCRIBE new pairs   [default 45]

cont. 69x follow-up: the scanner rotates `scanner:active_pairs` every ~20 min, but this
service used to re-read the universe only on the 12h connection rotation. Pairs that
rotated IN therefore got no WS klines for up to 12h, so data/feed._poll_candles fell back
to production /fapi/v1/klines REST for them every cycle (the persistent "candles_rest_fallback
pairs=2" bleed). A background resubscribe loop now diffs the live active set every
`resubscribe_s` and SUBSCRIBEs newly-added streams on the SAME socket (additive within a
session; the 12h rotation prunes dropped pairs), closing the gap to ~45s.

Run as docker-compose service `kline_ws`: `python -m data.kline_ws`.
"""
from __future__ import annotations

import asyncio
import json
import time

import structlog
import websockets

import redis_client
import redis_keys

log = structlog.get_logger()

# ROUTED /market endpoint. Binance decommissioned the legacy UNROUTED path for
# /market streams on 2026-04-23 — kline/markPrice/aggTrade/forceOrder now ONLY deliver
# under /market (they returned silent no-data on /ws). @depth is a /public stream and
# still works unrouted (that's why micro_ws on /stream is fine). Combined-stream +
# SUBSCRIBE works here; messages arrive wrapped as {"stream":..,"data":{..}}.
_WS_MARKET = "wss://fstream.binance.com/market/stream"
_INTERVALS = ["1m", "5m", "15m", "30m", "1h"]   # WS-fed; 4h/1d via daily bulk
_SUB_CHUNK = 150                                 # streams per SUBSCRIBE control message


def _cfg_int(r, key: str, default: int) -> int:
    try:
        v = r.get(key)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _select_pairs(r, max_pairs: int) -> list[str]:
    """Active pairs by 24h volume; max_pairs<=0 = all."""
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


def _streams_for(pairs: list[str]) -> list[str]:
    return [f"{p.lower()}@kline_{itv}" for p in pairs for itv in _INTERVALS]


async def _resubscribe_loop(ws, subscribed: set[str], r, max_pairs: int,
                            interval_s: float, started: float,
                            stop_after_s: float) -> None:
    """Every `interval_s`, re-read the active set and SUBSCRIBE any newly-added
    streams on the live socket so kline_ws tracks the ~20-min scanner rotation
    instead of waiting for the 12h connection cycle. Additive within a session
    (no UNSUBSCRIBE) to avoid candle-continuity gaps from transient churn — the
    12h rotation re-bases the subscription set and prunes dropped pairs."""
    loop = asyncio.get_event_loop()
    sub_id = 10_000
    while time.time() - started < stop_after_s:
        await asyncio.sleep(interval_s)
        try:
            pairs = await loop.run_in_executor(None, _select_pairs, r, max_pairs)
        except Exception:
            continue
        if not pairs:
            continue
        new = sorted(set(_streams_for(pairs)) - subscribed)
        if not new:
            continue
        for i in range(0, len(new), _SUB_CHUNK):
            await ws.send(json.dumps({"method": "SUBSCRIBE",
                                      "params": new[i:i + _SUB_CHUNK],
                                      "id": sub_id}))
            sub_id += 1
            await asyncio.sleep(0.25)
        subscribed.update(new)
        log.info("kline_ws_resubscribed", added=len(new), total=len(subscribed))
        try:
            r.setex("klines:ws:subscribed_streams", 600, len(subscribed))
        except Exception:
            pass


async def _run_stream(pairs: list[str], stop_after_s: float, max_pairs: int) -> None:
    """Subscribe kline streams for `pairs` and persist closed candles until
    stop_after_s elapses or the socket drops. A background loop SUBSCRIBEs
    pairs that rotate into the active set mid-session."""
    r = redis_client.get()
    keep = _cfg_int(r, "klines:ws:keep", 500)
    resub_s = float(_cfg_int(r, "klines:ws:resubscribe_s", 45))
    streams = _streams_for(pairs)
    subscribed: set[str] = set()
    started = time.time()

    async with websockets.connect(_WS_MARKET, ping_interval=20, ping_timeout=60,
                                  max_queue=8192, close_timeout=5) as ws:
        sub_id = 1
        for i in range(0, len(streams), _SUB_CHUNK):
            await ws.send(json.dumps({"method": "SUBSCRIBE",
                                      "params": streams[i:i + _SUB_CHUNK],
                                      "id": sub_id}))
            sub_id += 1
            await asyncio.sleep(0.25)
        subscribed.update(streams)
        log.info("kline_ws_connected", pairs=len(pairs), streams=len(streams))

        refresh_task = asyncio.create_task(
            _resubscribe_loop(ws, subscribed, r, max_pairs, resub_s,
                              started, stop_after_s))
        try:
            n_closed = 0
            async for raw in ws:
                if time.time() - started > stop_after_s:
                    break
                try:
                    m = json.loads(raw)
                    ev = m.get("data", m)        # /market/stream wraps as {stream,data}
                except Exception:
                    continue
                k = ev.get("k")
                if not k or not k.get("x"):       # only CLOSED candles
                    continue
                try:
                    sym = (k.get("s") or ev.get("s") or "").upper()
                    itv = k.get("i")
                    ts = int(k["t"])
                    row = json.dumps([ts, k["o"], k["h"], k["l"], k["c"], k["v"]])
                except (TypeError, ValueError, KeyError):
                    continue
                if not sym or not itv:
                    continue
                key = f"klines:ws:{sym}:{itv}"
                try:
                    pipe = r.pipeline()
                    pipe.zadd(key, {row: ts})
                    pipe.zremrangebyrank(key, 0, -(keep + 1))   # keep newest `keep`
                    pipe.execute()
                except Exception:
                    continue
                n_closed += 1
                if n_closed % 200 == 0:
                    try:
                        r.setex("klines:ws:closed", 300, n_closed)
                    except Exception:
                        pass
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("kline_ws_stream_ended", closed=n_closed)


async def main() -> None:
    r = redis_client.get()
    # Rotate every ~12h (under Binance's 24h forced disconnect) to refresh the universe.
    rotate_s = 12 * 3600
    while True:
        if (r.get("klines:ws:enabled") or "1") != "1":
            log.info("kline_ws_disabled_sleeping")
            await asyncio.sleep(60)
            continue
        max_pairs = _cfg_int(r, "klines:ws:max_pairs", 0)   # 0 = all active pairs
        pairs = _select_pairs(r, max_pairs)
        if not pairs:
            log.warning("kline_ws_no_pairs")
            await asyncio.sleep(30)
            continue
        try:
            await _run_stream(pairs, stop_after_s=rotate_s, max_pairs=max_pairs)
        except Exception as exc:
            log.warning("kline_ws_stream_error", error=str(exc)[:200])
            await asyncio.sleep(5)          # backoff before reconnect


if __name__ == "__main__":
    asyncio.run(main())
