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

    def place_market_order(self, pair: str, side: str, qty: float) -> dict:
        _track_weight(1)
        return _backoff_call(
            self._client.futures_create_order,
            symbol=pair,
            side=side.upper(),
            type="MARKET",
            quantity=qty,
        )

    def place_limit_order(self, pair: str, side: str, qty: float, price: float) -> dict:
        _track_weight(1)
        return _backoff_call(
            self._client.futures_create_order,
            symbol=pair,
            side=side.upper(),
            type="LIMIT",
            quantity=qty,
            price=price,
            timeInForce="GTC",
        )

    def cancel_order(self, pair: str, order_id: int) -> dict:
        _track_weight(1)
        return _backoff_call(self._client.futures_cancel_order, symbol=pair, orderId=order_id)

    def get_order_status(self, pair: str, order_id: int) -> dict:
        _track_weight(1)
        return _backoff_call(self._client.futures_get_order, symbol=pair, orderId=order_id)

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
