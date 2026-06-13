import time
import asyncio
from typing import Optional
from binance.client import Client
from binance import AsyncClient, BinanceSocketManager
import structlog
import config

log = structlog.get_logger()

_RATE_LIMIT_PER_MINUTE = 1200
_WEIGHT_WINDOW: list[tuple[float, int]] = []


def _track_weight(weight: int) -> None:
    """Block if consuming this weight would exceed 1200/minute."""
    now = time.monotonic()
    cutoff = now - 60.0
    global _WEIGHT_WINDOW
    _WEIGHT_WINDOW = [(t, w) for t, w in _WEIGHT_WINDOW if t > cutoff]
    total = sum(w for _, w in _WEIGHT_WINDOW)
    if total + weight > _RATE_LIMIT_PER_MINUTE:
        sleep_for = 60.0 - (now - _WEIGHT_WINDOW[0][0]) + 0.1
        log.warning("rate_limit_pause", sleep_seconds=round(sleep_for, 2))
        time.sleep(max(0, sleep_for))
    _WEIGHT_WINDOW.append((time.monotonic(), weight))


def _backoff_call(fn, *args, **kwargs):
    """Exponential back-off on HTTP 429: 1→2→4→8→…→60s."""
    delay = 1
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "Too Many Requests" in msg:
                log.warning("rate_limit_backoff", delay=delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            elif "401" in msg or "403" in msg or "Signature" in msg:
                log.error("auth_error", error=msg)
                raise
            elif "5" in msg[:3]:
                log.warning("server_error_backoff", delay=delay, error=msg)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                log.error("exchange_error", error=msg)
                raise


class BinanceClient:
    def __init__(self) -> None:
        self._client = Client(
            api_key=config.BINANCE_API_KEY,
            api_secret=config.BINANCE_API_SECRET,
            testnet=config.BINANCE_TESTNET,
            ping=False,
        )
        log.info("binance_client_ready", testnet=config.BINANCE_TESTNET)

    # --- REST interface (G-02) ---

    def get_mark_price(self, pair: str) -> dict:
        _track_weight(1)
        return _backoff_call(self._client.futures_mark_price, symbol=pair)

    def get_account_balance(self) -> list[dict]:
        _track_weight(5)
        return _backoff_call(self._client.futures_account_balance)

    def get_all_positions(self) -> list[dict]:
        _track_weight(5)
        return _backoff_call(self._client.futures_position_information)

    def get_position(self, pair: str) -> dict:
        _track_weight(5)
        positions = _backoff_call(self._client.futures_position_information, symbol=pair)
        return positions[0] if positions else {}

    def _get_step_size(self, pair: str) -> float:
        """Return the LOT_SIZE stepSize for `pair`. Cached per-process.

        Binance rejects orders whose quantity has more precision than the
        symbol's stepSize allows (APIError -1111). This helper looks up the
        step size from /fapi/v1/exchangeInfo and caches it.
        """
        if not hasattr(self, "_step_size_cache"):
            self._step_size_cache: dict[str, float] = {}
        if pair in self._step_size_cache:
            return self._step_size_cache[pair]
        try:
            info = _backoff_call(self._client.futures_exchange_info)
            for sym in info.get("symbols", []):
                step = 0.001  # safe default for major pairs
                for f in sym.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step = float(f.get("stepSize", 0.001))
                        break
                self._step_size_cache[sym["symbol"]] = step
        except Exception:
            pass
        return self._step_size_cache.get(pair, 0.001)

    def _round_qty(self, pair: str, qty: float) -> float:
        """Round `qty` DOWN to the pair's stepSize multiple. Never returns 0."""
        step = self._get_step_size(pair)
        if step <= 0:
            return qty
        import math
        rounded = math.floor(qty / step) * step
        # Round to step's decimal precision to remove float artifacts
        # (e.g. step=0.001 → 3 decimals)
        step_str = f"{step:.10f}".rstrip("0").rstrip(".")
        decimals = len(step_str.split(".")[1]) if "." in step_str else 0
        return round(rounded, decimals)

    def place_market_order(self, pair: str, side: str, qty: float,
                           reduce_only: bool = False) -> dict:
        """cont. 61 audit fix — Added `reduce_only` flag. CRITICAL:
        without reduce_only=True on close orders, Binance one-way mode
        treats a redundant close as a new opening trade. Audit 2026-05-29
        found a TRXUSDT short closed twice (SL + another trigger fired
        within 3s), the second BUY opened a 146-LONG that was never
        intended. Callers MUST pass reduce_only=True for close/partial
        orders (open and DCA orders correctly leave it False)."""
        _track_weight(1)
        qty_rounded = self._round_qty(pair, qty)
        if qty_rounded <= 0:
            raise ValueError(
                f"qty {qty} rounds to 0 at stepSize for {pair} — order too small")
        # cont. 69: minNotional guard on OPENS only (reduce_only closes must
        # always go through to flat the position regardless of notional). Binance
        # rejects sub-minNotional opens with -4164/-1013; fail loudly here so the
        # caller logs a clean reason instead of an opaque API error.
        if not reduce_only:
            try:
                mn = self._get_min_notional(pair)
                mark = float(self.get_mark_price(pair).get("markPrice") or 0)
                notional = qty_rounded * mark
                if mark > 0 and notional < mn:
                    raise ValueError(
                        f"notional {notional:.2f} < minNotional {mn:.2f} for "
                        f"{pair} — open too small")
            except ValueError:
                raise
            except Exception:
                pass  # exchange-info/mark fetch failed → let the order attempt proceed
        params = {
            "symbol": pair,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": qty_rounded,
        }
        if reduce_only:
            params["reduceOnly"] = True
        return _backoff_call(self._client.futures_create_order, **params)

    def place_limit_order(self, pair: str, side: str, qty: float, price: float) -> dict:
        _track_weight(1)
        qty_rounded = self._round_qty(pair, qty)
        if qty_rounded <= 0:
            raise ValueError(
                f"qty {qty} rounds to 0 at stepSize for {pair} — order too small")
        return _backoff_call(
            self._client.futures_create_order,
            symbol=pair,
            side=side.upper(),
            type="LIMIT",
            quantity=qty_rounded,
            price=self._round_price(pair, price),
            timeInForce="GTC",
        )

    # ── cont. 69: live SL must be an exchange-native STOP, not a LIMIT ────────
    def _get_tick_size(self, pair: str) -> float:
        """PRICE_FILTER tickSize for `pair` (cached). Binance rejects prices with
        more precision than tickSize (APIError -1111). Mirrors _get_step_size."""
        if not hasattr(self, "_tick_size_cache"):
            self._tick_size_cache: dict[str, float] = {}
        if pair in self._tick_size_cache:
            return self._tick_size_cache[pair]
        try:
            info = _backoff_call(self._client.futures_exchange_info)
            for sym in info.get("symbols", []):
                tick = 0.0001
                for f in sym.get("filters", []):
                    if f.get("filterType") == "PRICE_FILTER":
                        tick = float(f.get("tickSize", 0.0001))
                        break
                self._tick_size_cache[sym["symbol"]] = tick
        except Exception:
            pass
        return self._tick_size_cache.get(pair, 0.0001)

    def _round_price(self, pair: str, price: float) -> float:
        """Round `price` to the pair's tickSize. Stop/limit prices that violate
        tickSize are rejected with -1111."""
        tick = self._get_tick_size(pair)
        if tick <= 0:
            return price
        import math
        rounded = round(price / tick) * tick
        tick_str = f"{tick:.10f}".rstrip("0").rstrip(".")
        decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
        return round(rounded, decimals)

    def _get_min_notional(self, pair: str) -> float:
        """MIN_NOTIONAL for `pair` (cached). Binance rejects orders whose
        notional (qty × price) is below this (APIError -4164/-1013)."""
        if not hasattr(self, "_min_notional_cache"):
            self._min_notional_cache: dict[str, float] = {}
        if pair in self._min_notional_cache:
            return self._min_notional_cache[pair]
        try:
            info = _backoff_call(self._client.futures_exchange_info)
            for sym in info.get("symbols", []):
                mn = 5.0
                for f in sym.get("filters", []):
                    if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                        mn = float(f.get("notional", f.get("minNotional", 5.0)))
                        break
                self._min_notional_cache[sym["symbol"]] = mn
        except Exception:
            pass
        return self._min_notional_cache.get(pair, 5.0)

    def change_leverage(self, pair: str, leverage: int) -> dict:
        """Set per-symbol leverage on Binance BEFORE opening. Without this, live
        uses whatever leverage the account already has on the symbol (often the
        20x default), not the bot's intended per-trade leverage → wrong margin /
        liquidation distance and possible margin-reject. Idempotent on Binance."""
        _track_weight(1)
        lev = max(1, int(leverage))
        return _backoff_call(self._client.futures_change_leverage,
                             symbol=pair, leverage=lev)

    def place_stop_market_order(self, pair: str, side: str,
                                stop_price: float) -> dict:
        """Place an exchange-native STOP_MARKET that closes the WHOLE remaining
        position when `stop_price` triggers. closePosition=True means: no qty
        needed (always flats the position, so it stays correct after partial
        TPs), reduceOnly is implied, and Binance auto-cancels it when the
        position closes. Used for the trailing SL (cont. 69)."""
        _track_weight(1)
        return _backoff_call(
            self._client.futures_create_order,
            symbol=pair,
            side=side.upper(),
            type="STOP_MARKET",
            stopPrice=self._round_price(pair, stop_price),
            closePosition="true",
            workingType="MARK_PRICE",
        )

    def cancel_order(self, pair: str, order_id: int) -> dict:
        _track_weight(1)
        return _backoff_call(self._client.futures_cancel_order, symbol=pair, orderId=order_id)

    def get_open_orders(self, pair: str) -> list:
        """All currently-resting REGULAR orders for a symbol (weight=1)."""
        _track_weight(1)
        return _backoff_call(self._client.futures_get_open_orders, symbol=pair)

    def get_open_algo_orders(self) -> list:
        """cont. 70b — Binance routes futures STOP_MARKET/TAKE_PROFIT_MARKET
        trigger orders through the CONDITIONAL/algo system: the create response
        carries `algoId` (not `orderId`), they are INVISIBLE to
        futures_get_open_orders, and a second closePosition algo stop is rejected
        with -4130. List them here so the SL re-arm can cancel orphans."""
        _track_weight(1)
        res = _backoff_call(self._client.futures_get_open_algo_orders)
        if isinstance(res, dict):
            return res.get("orders", []) or []
        return res or []

    def cancel_algo_order(self, algo_id: int) -> dict:
        """Cancel a CONDITIONAL/algo trigger order by algoId (cont. 70b).
        futures_cancel_order(orderId) does NOT work on these."""
        _track_weight(1)
        return _backoff_call(self._client.futures_cancel_algo_order, algoId=algo_id)

    def get_account_trades(self, pair: str, order_id: int = None) -> list:
        """User trade fills for a symbol (weight=5), optionally for one orderId.
        cont. 70c — the real commission is here, NOT on the market-order ACK
        response (which carries commission=0), so live fees were recorded as 0."""
        _track_weight(5)
        kw = {"symbol": pair}
        if order_id is not None:
            kw["orderId"] = order_id
        return _backoff_call(self._client.futures_account_trades, **kw)

    def get_order_status(self, pair: str, order_id: int) -> dict:
        _track_weight(1)
        return _backoff_call(self._client.futures_get_order, symbol=pair, orderId=order_id)

    def get_open_interest(self, pair: str) -> float:
        """Return current open interest in base-asset units (weight=1)."""
        _track_weight(1)
        result = _backoff_call(self._client.futures_open_interest, symbol=pair)
        return float(result.get("openInterest", 0))

    # --- Priority 2 (cont. 74): OI velocity / Long-Short / Taker ratio ---
    # All are PRODUCTION-only /futures/data endpoints (verified absent on testnet);
    # the client here is built testnet=False. Each returns an ascending time series
    # so the caller derives z-scores inline (no Redis rolling window needed).

    def get_open_interest_hist(self, pair: str, period: str = "5m",
                               limit: int = 30) -> list[dict]:
        """OI history: each row has sumOpenInterest (base) + sumOpenInterestValue
        (USD notional). price = value/oi is derivable from the same call."""
        _track_weight(1)
        return _backoff_call(self._client.futures_open_interest_hist,
                             symbol=pair, period=period, limit=limit) or []

    def get_longshort_ratio(self, pair: str, period: str = "5m",
                            limit: int = 30, top: bool = False) -> list[dict]:
        """Long/short ratio series. top=False → global ACCOUNT ratio (retail
        crowd); top=True → top-trader POSITION ratio (smart money). Each row has
        longShortRatio, longAccount, shortAccount."""
        _track_weight(1)
        fn = (self._client.futures_top_longshort_position_ratio if top
              else self._client.futures_global_longshort_ratio)
        return _backoff_call(fn, symbol=pair, period=period, limit=limit) or []

    def get_taker_ratio(self, pair: str, period: str = "5m",
                        limit: int = 30) -> list[dict]:
        """Taker buy/sell volume series. Each row has buySellRatio, buyVol,
        sellVol — who is the aggressor (>1 = buyers lifting offers)."""
        _track_weight(1)
        return _backoff_call(self._client.futures_taker_longshort_ratio,
                             symbol=pair, period=period, limit=limit) or []

    # --- G-03: All USDT-M futures symbols ---

    def get_all_usdt_futures_symbols(self) -> list[str]:
        _track_weight(1)
        info = _backoff_call(self._client.futures_exchange_info)
        return [
            s["symbol"]
            for s in info["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING" and s["contractType"] == "PERPETUAL"
        ]

    # --- G-04: Historical klines ---

    def get_historical_klines(
        self,
        pair: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[list]:
        _track_weight(2)
        return _backoff_call(
            self._client.futures_historical_klines,
            symbol=pair,
            interval=interval,
            start_str=start_ms,
            end_str=end_ms,
            limit=1000,
        )


class BinanceWSManager:
    """G-08: Persistent async WebSocket stream manager."""

    def __init__(self, client: BinanceClient) -> None:
        self._rest = client
        self._async_client: Optional[AsyncClient] = None
        self._bsm: Optional[BinanceSocketManager] = None

    async def connect(self) -> None:
        self._async_client = await AsyncClient.create(
            api_key=config.BINANCE_API_KEY,
            api_secret=config.BINANCE_API_SECRET,
            testnet=config.BINANCE_TESTNET,
        )
        self._bsm = BinanceSocketManager(self._async_client)
        log.info("binance_ws_ready")

    async def close(self) -> None:
        if self._async_client:
            await self._async_client.close_connection()

    def get_socket_manager(self) -> BinanceSocketManager:
        if self._bsm is None:
            raise RuntimeError("WSManager not connected — call connect() first")
        return self._bsm

    async def _run_with_reconnect(self, stream_factory, handler, stream_name: str) -> None:
        """Run a stream, reconnecting with back-off on disconnect."""
        delay = 1
        while True:
            try:
                async with stream_factory() as stream:
                    delay = 1
                    async for msg in stream:
                        await handler(msg)
            except Exception as exc:
                log.warning("ws_disconnected", stream=stream_name, error=str(exc), reconnect_in=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
