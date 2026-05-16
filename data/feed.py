"""
Section K: WebSocket Data Ingestion & Microstructure — K-01 to K-13.
Subscribes to all required Binance USDT-M streams; writes to Redis.
"""
import asyncio
import json
import time
from collections import deque
import numpy as np
import structlog
import redis_client
import redis_keys
from exchange.client import BinanceWSManager, BinanceClient

log = structlog.get_logger()

# Rolling windows for microstructure computation
_volume_buckets: dict[str, dict] = {}   # pair -> {buy_vol, sell_vol, total_vol}
_ofi_ticks: dict[str, deque] = {}       # pair -> deque of (buy_vol - sell_vol) ticks
_price_ticks: dict[str, deque] = {}     # pair -> deque of (price, volume) ticks
_last_message_time: dict[str, float] = {}  # stream -> last recv timestamp

_SILENCE_THRESHOLD = 30.0   # seconds with no message = disconnected (K-07)
_CANDLE_BUFFER_SIZE = 500    # last N candles per pair per interval


async def handle_mark_price(msg: dict) -> None:
    """K-01: Mark price and funding rate per pair."""
    r = redis_client.get()
    pair = msg.get("s", "")
    r.set(redis_keys.MARK_PRICE.replace("{pair}", pair), msg.get("p", "0"))
    r.set(redis_keys.FUNDING_RATE.replace("{pair}", pair), msg.get("r", "0"))
    r.publish(redis_keys.CH_PRICE_UPDATE, json.dumps({
        "pair": pair, "mark_price": msg.get("p"), "funding_rate": msg.get("r"),
    }))
    _last_message_time["mark_price"] = time.monotonic()


async def handle_ticker(msg: dict) -> None:
    """K-02: 24h ticker data per pair."""
    r = redis_client.get()
    pair = msg.get("s", "")
    r.set(redis_keys.TICKER_VOLUME_24H.replace("{pair}", pair), msg.get("v", "0"))
    r.set(redis_keys.TICKER_CHANGE_24H.replace("{pair}", pair), msg.get("P", "0"))
    r.set(redis_keys.LAST_PRICE.replace("{pair}", pair), msg.get("c", "0"))
    _last_message_time["ticker"] = time.monotonic()


async def handle_kline(msg: dict) -> None:
    """K-03: Completed kline candles — buffer last N per pair per interval."""
    k = msg.get("k", {})
    if not k.get("x"):   # only closed candles
        return
    r = redis_client.get()
    pair = k.get("s", "")
    interval = k.get("i", "1m")
    candle = json.dumps({
        "t": k["t"], "o": k["o"], "h": k["h"],
        "l": k["l"], "c": k["c"], "v": k["v"],
    })
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", interval)
    r.lpush(key, candle)
    r.ltrim(key, 0, _CANDLE_BUFFER_SIZE - 1)
    _last_message_time["kline"] = time.monotonic()


async def handle_depth(msg: dict) -> None:
    """K-04: Order book depth — bid/ask prices and imbalance."""
    r = redis_client.get()
    pair = msg.get("s", "")
    bids = msg.get("b", [])
    asks = msg.get("a", [])
    if bids:
        r.set(redis_keys.BID_PRICE.replace("{pair}", pair), bids[0][0])
    if asks:
        r.set(redis_keys.ASK_PRICE.replace("{pair}", pair), asks[0][0])

    bid_vol = sum(float(b[1]) for b in bids[:5])
    ask_vol = sum(float(a[1]) for a in asks[:5])
    total = bid_vol + ask_vol
    imbalance = (bid_vol - ask_vol) / total if total > 0 else 0
    r.set(redis_keys.BID_ASK_IMBALANCE.replace("{pair}", pair), imbalance)

    # K-10: OFI (Order Flow Imbalance)
    if pair not in _ofi_ticks:
        _ofi_ticks[pair] = deque(maxlen=100)
    buy_vol = bid_vol
    sell_vol = ask_vol
    ofi = (buy_vol - sell_vol) / total if total > 0 else 0
    _ofi_ticks[pair].append(ofi)
    r.set(redis_keys.OFI.replace("{pair}", pair), sum(_ofi_ticks[pair]) / len(_ofi_ticks[pair]))

    _last_message_time["depth"] = time.monotonic()


async def handle_user_data(msg: dict) -> None:
    """K-05: User data stream — balance and order fills."""
    r = redis_client.get()
    event = msg.get("e")
    if event == "ACCOUNT_UPDATE":
        for asset in msg.get("a", {}).get("B", []):
            if asset.get("a") == "USDT":
                r.set(redis_keys.ACCOUNT_BALANCE, asset.get("wb", "0"))
    elif event == "ORDER_TRADE_UPDATE":
        if msg.get("o", {}).get("X") == "FILLED":
            r.publish("trade_event", json.dumps(msg))
    _last_message_time["user_data"] = time.monotonic()


def _compute_vpin(pair: str) -> float:
    """K-09: VPIN = |V_buy - V_sell| / V_total per volume bucket."""
    bucket = _volume_buckets.get(pair, {"buy": 0, "sell": 0, "total": 0})
    total = bucket["total"]
    if total == 0:
        return 0.0
    return abs(bucket["buy"] - bucket["sell"]) / total


def _compute_turbulence(all_returns: dict[str, float]) -> float:
    """K-13: Turbulence Index = Mahalanobis distance of current returns."""
    if len(all_returns) < 5:
        return 0.0
    vals = list(all_returns.values())
    try:
        arr = np.array(vals)
        mean = np.mean(arr)
        std = np.std(arr)
        if std == 0:
            return 0.0
        z_scores = (arr - mean) / std
        return float(np.sqrt(np.mean(z_scores ** 2)))
    except Exception:
        return 0.0


async def _silence_detector(stream_name: str, timeout: float = _SILENCE_THRESHOLD) -> None:
    """K-07: Detect stream silence and log for reconnection."""
    while True:
        await asyncio.sleep(timeout)
        last = _last_message_time.get(stream_name, 0)
        if time.monotonic() - last > timeout:
            log.warning("stream_silent", stream=stream_name, silence_seconds=timeout)


async def update_microstructure_loop() -> None:
    """Periodically compute Kyle's Lambda (K-11), Amihud (K-12), turbulence (K-13)."""
    r = redis_client.get()
    price_history: dict[str, list] = {}
    returns: dict[str, float] = {}

    while True:
        try:
            active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))
            for pair in active_pairs:
                mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
                if pair in price_history and price_history[pair]:
                    prev = price_history[pair][-1]
                    if prev > 0:
                        ret = (mark - prev) / prev
                        returns[pair] = ret
                        # K-12: Amihud Illiquidity
                        vol = float(r.get(redis_keys.TICKER_VOLUME_24H.replace("{pair}", pair)) or 1)
                        amihud = abs(ret) / vol if vol > 0 else 0
                        r.set(redis_keys.AMIHUD.replace("{pair}", pair), amihud, ex=300)
                        # K-09: VPIN
                        vpin = _compute_vpin(pair)
                        r.set(redis_keys.VPIN.replace("{pair}", pair), vpin, ex=60)

                if pair not in price_history:
                    price_history[pair] = []
                price_history[pair].append(mark)
                if len(price_history[pair]) > 100:
                    price_history[pair] = price_history[pair][-100:]

            # K-13: Global turbulence
            if returns:
                turbulence = _compute_turbulence(returns)
                r.set(redis_keys.TURBULENCE_INDEX, turbulence)

        except Exception as exc:
            log.error("microstructure_update_error", error=str(exc))

        await asyncio.sleep(60)


async def run(ws_manager: BinanceWSManager) -> None:
    """K-01 to K-13: Start all stream handlers."""
    bsm = ws_manager.get_socket_manager()
    await asyncio.gather(
        update_microstructure_loop(),
        _silence_detector("mark_price"),
        _silence_detector("kline"),
        _silence_detector("depth"),
    )


if __name__ == "__main__":
    import asyncio
    import db, redis_client
    from exchange.client import BinanceClient, BinanceWSManager
    db.init_pool()
    redis_client.init()
    client = BinanceClient()
    ws = BinanceWSManager(client)
    asyncio.run(ws.connect())
