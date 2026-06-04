"""data/liq_ws.py — real-time liquidation flow feed (cont. 69x item 2).

Streams Binance USDT-M `!forceOrder@arr` (all-market forced-liquidation prints)
and maintains a rolling per-pair notional window in Redis. This is the LIVE
event-flow counterpart to data/liquidation_levels.py's cluster LEVELS estimator
— the latter projects WHERE liquidations sit; this measures liquidations
HAPPENING NOW and in which direction. It is a NEW free data source (WS streams
do not consume the /fapi request-weight pool) and replaces no REST call.

Direction convention (matches liquidation_levels._summarise / get_bonus):
  * A forced order with side=SELL closes a LONG → forced selling → bearish
    pressure → contributes to a "short" cascade.
  * side=BUY closes a SHORT → forced buying → bullish → "long" cascade.

Per flush (every `liq:ws:flush_s` seconds) each pair with recent prints gets:
  {pair}:liq_buy_notional   USD of short-liqs (BUY) in the rolling window
  {pair}:liq_sell_notional  USD of long-liqs  (SELL) in the rolling window
  {pair}:liq_flow_dir       long | short | neutral
  {pair}:liq_flow_intensity [0,1]  total / liq:ws:intensity_scale_usd, capped
plus a market-wide `liq:global_rate` (USD/min). All keys carry a short TTL so a
quiet pair decays to "stale → neutral" on the consumer side automatically.

Consumers (cont. 69x item 2, full F58 wiring):
  * data.liquidation_levels._effective_cascade / get_bonus  — entry bonus
  * risk.frontier.exit_kill_switch.evaluate_liq_cascade_kill — exit

Config (Redis):
  liq:ws:enabled              "0" disables (sleep-loops)            [default 1]
  liq:ws:window_s             rolling window seconds                [default 60]
  liq:ws:flush_s              aggregate→Redis cadence seconds       [default 2]
  liq:ws:min_notional_usd     below this window total → neutral     [default 50000]
  liq:ws:intensity_scale_usd  window total mapped to intensity 1.0  [default 2000000]
  liq:ws:dominance            one side ≥ this× the other for a dir   [default 1.5]

Run as docker-compose service `liq_ws`: `python -m data.liq_ws`.
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

# ROUTED /market endpoint — the legacy unrouted /ws path for /market streams
# (forceOrder is a /market stream) was decommissioned 2026-04-23 and returns
# silent no-data. PRODUCTION fstream regardless of BINANCE_TESTNET: liquidation
# flow wants real mainnet venue (testnet has almost none). See [[reference_binance_data_sources]].
_WS_MARKET = "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr"


def _cfg_float(r, key: str, default: float) -> float:
    try:
        v = r.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _flush(r, events: dict, cfg: dict) -> None:
    """Prune to the rolling window, aggregate per pair, write Redis + global rate."""
    now = time.time()
    window_s = cfg["window_s"]
    min_notional = cfg["min_notional"]
    scale = cfg["scale"]
    dominance = cfg["dominance"]
    cutoff = now - window_s
    ttl = int(window_s + 30)

    global_total = 0.0
    pipe = r.pipeline(transaction=False)
    dead = []
    for pair, evs in events.items():
        # Drop events older than the window.
        evs[:] = [e for e in evs if e[0] >= cutoff]
        if not evs:
            dead.append(pair)
            continue
        buy = sum(e[2] for e in evs if e[1] == "BUY")    # short-liqs → bullish
        sell = sum(e[2] for e in evs if e[1] == "SELL")  # long-liqs  → bearish
        total = buy + sell
        global_total += total

        if total < min_notional:
            direction = "neutral"
        elif buy >= sell * dominance:
            direction = "long"
        elif sell >= buy * dominance:
            direction = "short"
        else:
            direction = "neutral"
        intensity = min(1.0, total / scale) if scale > 0 else 0.0

        pipe.setex(redis_keys.LIQ_FLOW_BUY_NOTIONAL.replace("{pair}", pair), ttl, round(buy, 2))
        pipe.setex(redis_keys.LIQ_FLOW_SELL_NOTIONAL.replace("{pair}", pair), ttl, round(sell, 2))
        pipe.setex(redis_keys.LIQ_FLOW_DIR.replace("{pair}", pair), ttl, direction)
        pipe.setex(redis_keys.LIQ_FLOW_INTENSITY.replace("{pair}", pair), ttl, round(intensity, 4))

    # Market-wide rate, normalised to USD/min.
    global_rate = global_total * (60.0 / window_s) if window_s > 0 else 0.0
    pipe.setex(redis_keys.LIQ_GLOBAL_RATE, ttl, round(global_rate, 2))
    pipe.setex(redis_keys.LIQ_WS_CONNECTED, ttl, int(now))
    pipe.execute()

    for p in dead:
        events.pop(p, None)


async def _flusher(r, events: dict, cfg_holder: dict, stop_after_s: float) -> None:
    """Periodic aggregation loop — runs alongside the socket reader."""
    started = time.time()
    while time.time() - started < stop_after_s:
        await asyncio.sleep(cfg_holder["flush_s"])
        try:
            _flush(r, events, cfg_holder)
        except Exception as exc:
            log.warning("liq_ws_flush_error", error=str(exc)[:160])


async def _run_stream(stop_after_s: float) -> None:
    r = redis_client.get()
    cfg = {
        "window_s": _cfg_float(r, "liq:ws:window_s", 60.0),
        "flush_s": max(1.0, _cfg_float(r, "liq:ws:flush_s", 2.0)),
        "min_notional": _cfg_float(r, "liq:ws:min_notional_usd", 50_000.0),
        "scale": _cfg_float(r, "liq:ws:intensity_scale_usd", 2_000_000.0),
        "dominance": _cfg_float(r, "liq:ws:dominance", 1.5),
    }
    events: dict[str, list] = {}
    started = time.time()
    n_events = 0

    async with websockets.connect(_WS_MARKET, ping_interval=20, ping_timeout=60,
                                  max_queue=4096, close_timeout=5) as ws:
        log.info("liq_ws_connected", stream="!forceOrder@arr", window_s=cfg["window_s"])
        flusher = asyncio.create_task(_flusher(r, events, cfg, stop_after_s))
        try:
            async for raw in ws:
                if time.time() - started > stop_after_s:
                    break
                try:
                    m = json.loads(raw)
                    ev = m.get("data", m)
                    o = ev.get("o") or {}
                    sym = (o.get("s") or "").upper()
                    side = o.get("S") or ""
                    if not sym or not sym.endswith("USDT") or side not in ("BUY", "SELL"):
                        continue
                    # Filled qty `z` × avg price `ap` = real liquidated notional;
                    # fall back to orig qty `q` / price `p` if a field is absent.
                    qty = float(o.get("z") or o.get("q") or 0)
                    px = float(o.get("ap") or o.get("p") or 0)
                    notional = qty * px
                    if notional <= 0:
                        continue
                except (TypeError, ValueError, AttributeError):
                    continue
                events.setdefault(sym, []).append((time.time(), side, notional))
                n_events += 1
                if n_events % 100 == 0:
                    try:
                        r.incrby(redis_keys.LIQ_WS_EVENTS_TOTAL, 100)
                    except Exception:
                        pass
        finally:
            flusher.cancel()
            try:
                await flusher
            except (asyncio.CancelledError, Exception):
                pass
        log.info("liq_ws_stream_ended", events=n_events)


async def main() -> None:
    r = redis_client.get()
    # Rotate every ~12h (under Binance's 24h forced disconnect).
    rotate_s = 12 * 3600
    while True:
        if (r.get("liq:ws:enabled") or "1") != "1":
            log.info("liq_ws_disabled_sleeping")
            await asyncio.sleep(60)
            continue
        try:
            await _run_stream(stop_after_s=rotate_s)
        except Exception as exc:
            log.warning("liq_ws_stream_error", error=str(exc)[:200])
            await asyncio.sleep(5)   # backoff before reconnect


if __name__ == "__main__":
    asyncio.run(main())
