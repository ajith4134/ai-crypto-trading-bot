"""
Section K: Market Data Ingestion — K-01 to K-13.
Stage 1: REST polling every 5s for mark prices (all pairs, single call).
WebSocket streams added in Stage 2+ when performance needs increase.
"""
import asyncio, json, time
import numpy as np, structlog, requests
import redis_client, redis_keys

log = structlog.get_logger()

FUTURES_BASE   = "https://testnet.binancefuture.com"  # testnet; change to fapi.binance.com for live
POLL_INTERVAL  = 5   # seconds between mark price polls
_price_history: dict[str, list] = {}
_returns:       dict[str, float] = {}


def _poll_mark_prices(r) -> int:
    """K-01: Fetch all USDT-M mark prices in one REST call."""
    try:
        resp = requests.get(f"{FUTURES_BASE}/fapi/v1/premiumIndex", timeout=10)
        resp.raise_for_status()
        count = 0
        for item in resp.json():
            pair = item.get("symbol", "")
            if not pair.endswith("USDT"):
                continue
            price = item.get("markPrice", "0")
            funding = item.get("lastFundingRate", "0")
            r.set(redis_keys.MARK_PRICE.replace("{pair}", pair), price)
            r.set(redis_keys.FUNDING_RATE.replace("{pair}", pair), funding)
            r.set(redis_keys.LAST_PRICE.replace("{pair}", pair), price)
            p = float(price)
            if p > 0:
                hist = _price_history.setdefault(pair, [])
                hist.append(p)
                if len(hist) > 100:
                    _price_history[pair] = hist[-100:]
            count += 1
        return count
    except Exception as exc:
        log.error("mark_price_poll_failed", error=str(exc))
        return 0


def _poll_24h_tickers(r) -> None:
    """K-02: 24h ticker data (volume, change) — poll every 30s."""
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
    """K-09/K-13: OFI, VPIN, Turbulence Index from price history."""
    try:
        active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))
        for pair in active_pairs:
            hist = _price_history.get(pair, [])
            if len(hist) >= 2:
                ret = (hist[-1] - hist[-2]) / hist[-2] if hist[-2] > 0 else 0.0
                _returns[pair] = ret
                vol = float(r.get(redis_keys.TICKER_VOLUME_24H.replace("{pair}", pair)) or 1)
                r.set(redis_keys.OFI.replace("{pair}", pair),   round(ret, 8))
                r.set(redis_keys.VPIN.replace("{pair}", pair),  round(abs(ret), 8))
                r.set(redis_keys.AMIHUD.replace("{pair}", pair), round(abs(ret) / vol, 10) if vol else 0)
                r.set(redis_keys.KYLES_LAMBDA.replace("{pair}", pair), round(abs(ret), 8))

        if _returns:
            vals = list(_returns.values())
            arr  = np.array(vals, dtype=float)
            std  = np.std(arr)
            turb = float(np.mean(((arr - np.mean(arr)) / std) ** 2) ** 0.5) if std > 0 else 0.0
            r.set(redis_keys.TURBULENCE_INDEX, round(turb, 6))
    except Exception as exc:
        log.error("microstructure_error", error=str(exc))


async def data_loop() -> None:
    r = redis_client.get()
    tick_counter = 0
    log.info("data_feed_loop_started", poll_interval_s=POLL_INTERVAL)
    while True:
        count = _poll_mark_prices(r)
        if count and tick_counter % 6 == 0:   # every ~30s
            _poll_24h_tickers(r)
        if count and tick_counter % 2 == 0:   # every ~10s
            _compute_microstructure(r)
        if tick_counter % 60 == 0:
            log.info("data_feed_heartbeat", pairs_updated=count, tick=tick_counter)
        tick_counter += 1
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    import db
    db.init_pool()
    redis_client.init()
    asyncio.run(data_loop())
