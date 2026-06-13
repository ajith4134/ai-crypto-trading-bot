"""data/kline_ws.py — WS klines corpus feed (cont. 69x; sharded cont. 76).

Streams CLOSED mainnet candles for active pairs x {1m,5m,15m,30m,1h} from Binance
USDT-M futures and writes them to Redis sorted sets `klines:ws:{pair}:{itv}`
(score = open_time_ms, member = JSON [ts,o,h,l,c,v]). ml.klines_corpus.incremental_from_ws
merges these into the same CSV training corpus as the REST path — byte-identical — with
ZERO fapi REST, retiring the hourly /fapi/v1/klines top-up hammer (~1500 weight/min) that
re-walked the IP toward a -1003 ban.

Binance market-data WS streams do NOT consume the /fapi/v1 request-weight pool (they are
connection-limited only). Coarse TFs (4h/1d) stay on the daily data.binance.vision bulk —
a WS close for those is hours/days apart, useless for hourly freshness.

cont. 76 — MULTI-CONNECTION SHARDING (the staleness fix). Binance caps ONE connection at
1024 streams. ~500 active pairs x 5 TFs = ~2500 streams, so a single socket physically
cannot carry the universe: the old single-connection feed covered only the top ~200 pairs by
volume and, worse, its resubscribe loop kept ADDING streams until the total crossed 1024,
at which point Binance killed the socket with `1008 policy violation` — a reconnect storm
that left >half the universe's candle lists hours/days stale. We now shard the full stream
set across ceil(total / streams_per_conn) concurrent connections (deterministic
crc32(stream) % n_shards assignment, so a stream always lands on the same shard across
resubscribes), and each shard's resubscribe loop refuses to push its own socket past a hard
cap — so EVERY active pair x EVERY TF stays live and no connection ever trips the 1008 limit.

Config (Redis):
  klines:ws:enabled            "0" disables (sleep-loops)                  [default 1]
  klines:ws:max_pairs          0 = ALL active pairs; N = top-N by 24h vol  [default 0]
  klines:ws:keep               closed candles retained per pair/interval   [default 500]
  klines:ws:resubscribe_s      re-read active set + SUBSCRIBE new pairs    [default 45]
  klines:ws:streams_per_conn   target streams per shard connection         [default 900]
  klines:ws:hard_stream_cap    never SUBSCRIBE a shard past this (<1024)   [default 1000]

Beacons (Redis): klines:ws:subscribed_streams (total across shards),
  klines:ws:closed (total closed candles persisted), klines:ws:shards (active shard count),
  klines:ws:shard:{i}:streams (per-shard subscribed count).

Run as docker-compose service `kline_ws`: `python -m data.kline_ws`.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import zlib

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


def _shard_of(stream: str, n_shards: int) -> int:
    """Deterministic, stable shard assignment for a stream. crc32 keeps a stream on the
    SAME connection across resubscribes (so we never duplicate a stream onto two sockets),
    and spreads the ~2500 streams roughly evenly across the n_shards connections."""
    if n_shards <= 1:
        return 0
    return zlib.crc32(stream.encode("utf-8")) % n_shards


def _shard_streams(r, max_pairs: int, shard_idx: int, n_shards: int) -> list[str]:
    """The streams owned by this shard for the CURRENT active set."""
    streams = _streams_for(_select_pairs(r, max_pairs))
    if n_shards <= 1:
        return streams
    return [s for s in streams if _shard_of(s, n_shards) == shard_idx]


async def _resubscribe_loop(ws, subscribed: set[str], r, max_pairs: int,
                            shard_idx: int, n_shards: int, hard_cap: int,
                            interval_s: float, started: float,
                            stop_after_s: float) -> None:
    """Every `interval_s`, re-read the active set and SUBSCRIBE any newly-added streams that
    belong to THIS shard, so kline_ws tracks the ~20-min scanner rotation instead of waiting
    for the 12h connection cycle. Additive within a session (no UNSUBSCRIBE) to avoid
    candle-continuity gaps from transient churn — the 12h rotation re-bases the shard set.
    Hard-capped at `hard_cap` (< Binance's 1024/connection) so a growing universe can never
    push this socket into a 1008 policy-violation disconnect (the old single-socket failure)."""
    loop = asyncio.get_event_loop()
    sub_id = 10_000
    while time.time() - started < stop_after_s:
        await asyncio.sleep(interval_s)
        try:
            mine = await loop.run_in_executor(
                None, _shard_streams, r, max_pairs, shard_idx, n_shards)
        except Exception:
            continue
        if not mine:
            continue
        new = sorted(set(mine) - subscribed)
        if not new:
            continue
        room = hard_cap - len(subscribed)
        if room <= 0:
            log.warning("kline_ws_shard_at_cap", shard=shard_idx,
                        subscribed=len(subscribed), cap=hard_cap, pending=len(new))
            try:
                r.incr("klines:ws:capped_skips")
            except Exception:
                pass
            continue
        if len(new) > room:               # never exceed the per-connection hard cap
            new = new[:room]
        for i in range(0, len(new), _SUB_CHUNK):
            await ws.send(json.dumps({"method": "SUBSCRIBE",
                                      "params": new[i:i + _SUB_CHUNK],
                                      "id": sub_id}))
            sub_id += 1
            await asyncio.sleep(0.25)
        subscribed.update(new)
        log.info("kline_ws_resubscribed", shard=shard_idx,
                 added=len(new), total=len(subscribed))
        try:
            r.setex(f"klines:ws:shard:{shard_idx}:streams", 600, len(subscribed))
        except Exception:
            pass


async def _run_shard(shard_idx: int, n_shards: int, stop_after_s: float,
                     max_pairs: int) -> None:
    """One WebSocket connection carrying this shard's slice of the stream universe.
    Subscribes its streams, persists closed candles, and SUBSCRIBEs pairs that rotate into
    its shard mid-session — until stop_after_s elapses or the socket drops."""
    r = redis_client.get()
    keep = _cfg_int(r, "klines:ws:keep", 500)
    resub_s = float(_cfg_int(r, "klines:ws:resubscribe_s", 45))
    hard_cap = _cfg_int(r, "klines:ws:hard_stream_cap", 1000)
    streams = _shard_streams(r, max_pairs, shard_idx, n_shards)
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
        log.info("kline_ws_shard_connected", shard=shard_idx, of=n_shards,
                 streams=len(streams))
        try:
            r.setex(f"klines:ws:shard:{shard_idx}:streams", 600, len(subscribed))
        except Exception:
            pass

        refresh_task = asyncio.create_task(
            _resubscribe_loop(ws, subscribed, r, max_pairs, shard_idx, n_shards,
                              hard_cap, resub_s, started, stop_after_s))
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
                        r.incrby("klines:ws:closed", 200)
                    except Exception:
                        pass
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("kline_ws_shard_ended", shard=shard_idx, closed=n_closed)


async def main() -> None:
    r = redis_client.get()
    # Rotate every ~12h (under Binance's 24h forced disconnect) to refresh the universe
    # AND re-base the shard count to the current pair total.
    rotate_s = 12 * 3600
    while True:
        if (r.get("klines:ws:enabled") or "1") != "1":
            log.info("kline_ws_disabled_sleeping")
            await asyncio.sleep(60)
            continue
        max_pairs = _cfg_int(r, "klines:ws:max_pairs", 0)   # 0 = ALL active pairs
        pairs = _select_pairs(r, max_pairs)
        if not pairs:
            log.warning("kline_ws_no_pairs")
            await asyncio.sleep(30)
            continue

        total_streams = len(pairs) * len(_INTERVALS)
        spc = max(50, _cfg_int(r, "klines:ws:streams_per_conn", 900))
        n_shards = max(1, math.ceil(total_streams / spc))
        try:
            r.setex("klines:ws:shards", 600, n_shards)
            r.setex("klines:ws:subscribed_streams", 600, total_streams)
            r.set("klines:ws:closed", 0)
        except Exception:
            pass
        log.info("kline_ws_session_start", pairs=len(pairs),
                 total_streams=total_streams, shards=n_shards, per_conn=spc)

        # one concurrent connection per shard; gather so a single shard drop doesn't
        # abort the others — the 12h rotation re-establishes the whole set together.
        try:
            await asyncio.gather(
                *[_run_shard(i, n_shards, rotate_s, max_pairs) for i in range(n_shards)],
                return_exceptions=True,
            )
        except Exception as exc:
            log.warning("kline_ws_session_error", error=str(exc)[:200])
            await asyncio.sleep(5)          # backoff before reconnect


if __name__ == "__main__":
    asyncio.run(main())
