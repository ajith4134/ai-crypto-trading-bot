"""
Section R: Pair Scanner & Selector — R-01 to R-11.
Scans all USDT-M futures, scores on 5 criteria, maintains active pair list.
"""
import asyncio
import json
import structlog
import redis_client
import redis_keys
import config
from db import db_conn

log = structlog.get_logger()


def _norm(values: list[float]) -> list[float]:
    """Normalise a list of floats to 0–100."""
    if not values:
        return []
    mn, mx = min(values), max(values)
    if mx == mn:
        return [50.0] * len(values)
    return [(v - mn) / (mx - mn) * 100 for v in values]


def score_volume(ticker_data: dict[str, dict]) -> dict[str, float]:
    """R-02: Volume score (0–100) per symbol."""
    vols = {sym: float(d.get("volume", 0)) for sym, d in ticker_data.items()}
    normed = _norm(list(vols.values()))
    return {sym: normed[i] for i, sym in enumerate(vols)}


def score_volatility(ticker_data: dict[str, dict]) -> dict[str, float]:
    """R-03: Volatility score — daily range % normalised to 0–100."""
    def _range_pct(d: dict) -> float:
        h = float(d.get("high", 0))
        l = float(d.get("low", 1))
        c = float(d.get("close", 1))
        return (h - l) / c * 100 if c else 0

    vols = {sym: _range_pct(d) for sym, d in ticker_data.items()}
    normed = _norm(list(vols.values()))
    return {sym: normed[i] for i, sym in enumerate(vols)}


def score_spread(ticker_data: dict[str, dict]) -> dict[str, float]:
    """R-04: Spread score — inverted bid/ask spread (tighter = higher)."""
    def _spread(d: dict) -> float:
        bid = float(d.get("bidPrice", 0))
        ask = float(d.get("askPrice", 0))
        mid = (bid + ask) / 2
        return (ask - bid) / mid * 100 if mid else 999

    spreads = {sym: _spread(d) for sym, d in ticker_data.items()}
    inverted = {sym: -v for sym, v in spreads.items()}
    normed = _norm(list(inverted.values()))
    return {sym: normed[i] for i, sym in enumerate(inverted)}


def score_win_rate(symbols: list[str]) -> dict[str, float]:
    """R-05: Historical win rate on this pair from trades table."""
    scores = {}
    with db_conn() as conn:
        with conn.cursor() as cur:
            for sym in symbols:
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE net_pnl_usdt > 0) AS wins, COUNT(*) AS total "
                    "FROM trades WHERE pair = %s AND status = 'closed'",
                    (sym,),
                )
                row = cur.fetchone()
                wins, total = (row[0] or 0), (row[1] or 0)
                scores[sym] = (wins / total * 100) if total >= 5 else 50.0
    normed = _norm(list(scores.values()))
    return {sym: normed[i] for i, sym in enumerate(scores)}


def score_pnl(symbols: list[str]) -> dict[str, float]:
    """R-06: Total historical net PnL on this pair from pairs table."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, total_net_pnl_usdt FROM pairs WHERE symbol = ANY(%s)",
                (symbols,),
            )
            pnl_map = {row[0]: float(row[1] or 0) for row in cur.fetchall()}
    scores = {sym: pnl_map.get(sym, 0) for sym in symbols}
    normed = _norm(list(scores.values()))
    return {sym: normed[i] for i, sym in enumerate(scores)}


def compute_composite(
    syms: list[str],
    vol_s: dict, volatility_s: dict, spread_s: dict,
    winrate_s: dict, pnl_s: dict,
    weights: dict,
) -> dict[str, float]:
    """R-07: Composite score using Brain-learned weights."""
    scores = {}
    for sym in syms:
        scores[sym] = round(
            vol_s.get(sym, 0) * weights["volume"] +
            volatility_s.get(sym, 0) * weights["volatility"] +
            spread_s.get(sym, 0) * weights["spread"] +
            winrate_s.get(sym, 0) * weights["win_rate"] +
            pnl_s.get(sym, 0) * weights["pnl"],
            2,
        )
    return scores


def update_active_pairs(composite: dict[str, float], max_pairs: int) -> list[str]:
    """R-08: Select top-N pairs; write to Redis and pairs table."""
    sorted_pairs = sorted(composite, key=lambda s: composite[s], reverse=True)
    active = sorted_pairs[:max_pairs]

    r = redis_client.get()
    r.delete(redis_keys.ACTIVE_PAIRS)
    if active:
        r.sadd(redis_keys.ACTIVE_PAIRS, *active)

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE pairs SET is_active = FALSE")
            for sym in active:
                cur.execute("""
                    INSERT INTO pairs (symbol, is_active, composite_score)
                    VALUES (%s, TRUE, %s)
                    ON CONFLICT (symbol) DO UPDATE
                    SET is_active = TRUE, composite_score = EXCLUDED.composite_score,
                        last_scanned_at = NOW(), updated_at = NOW()
                """, (sym, composite[sym]))

    log.info("active_pairs_updated", count=len(active))
    return active


async def run_scan(exchange_client) -> list[str]:
    """R-01: Full market scan — called on startup and every 8 hours."""
    r = redis_client.get()
    brain_stage = int(r.get(redis_keys.BRAIN_STAGE) or 1)

    all_symbols = exchange_client.get_all_usdt_futures_symbols()

    ticker_data: dict[str, dict] = {}
    for sym in all_symbols:
        ticker_data[sym] = {
            "volume": float(r.get(redis_keys.TICKER_VOLUME_24H.replace("{pair}", sym)) or 0),
            "high": 0, "low": 0, "close": float(r.get(redis_keys.LAST_PRICE.replace("{pair}", sym)) or 0),
            "bidPrice": float(r.get(redis_keys.BID_PRICE.replace("{pair}", sym)) or 0),
            "askPrice": float(r.get(redis_keys.ASK_PRICE.replace("{pair}", sym)) or 0),
        }

    syms = list(ticker_data.keys())
    weights_raw = r.get(redis_keys.BRAIN_FEATURE_WEIGHTS)
    weights = json.loads(weights_raw) if weights_raw else {}
    # Fall back to config weights if Brain hasn't learned custom weights yet
    if not weights.get("volume"):
        weights = config.scanner.criteria_weights

    vol_s = score_volume(ticker_data)
    volatility_s = score_volatility(ticker_data)
    spread_s = score_spread(ticker_data)
    winrate_s = score_win_rate(syms)
    pnl_s = score_pnl(syms)

    # Stage 1-2: use full max_active_pairs (200) to maximise data generation speed
    # Stage 3-4: tighten to top half for quality over quantity
    max_pairs = config.trading.max_active_pairs if brain_stage <= 2 else max(60, config.trading.max_active_pairs // 2)
    composite = compute_composite(syms, vol_s, volatility_s, spread_s, winrate_s, pnl_s, weights)
    return update_active_pairs(composite, max_pairs)


async def scanner_loop(exchange_client) -> None:
    """R-10: Rescan every 8 hours."""
    while True:
        try:
            await run_scan(exchange_client)
        except Exception as exc:
            log.error("scanner_error", error=str(exc))
        await asyncio.sleep(config.scanner.rescan_interval_hours * 3600)


if __name__ == "__main__":
    import asyncio
    import db, redis_client, config
    from exchange.client import BinanceClient
    db.init_pool()
    redis_client.init()
    client = BinanceClient()
    asyncio.run(scanner_loop(client))
