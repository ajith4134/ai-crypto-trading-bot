import json
from exchange.client import BinanceClient

bc = BinanceClient()
pair = "ARBUSDT"
raw = 0.08706956

# tick size + filters
tick = bc._get_tick_size(pair)
rounded = bc._round_price(pair, raw)
print("tick_size      =", tick)
print("raw stop       =", raw)
print("rounded stop   =", rounded)

info = bc._client.futures_exchange_info()
for s in info["symbols"]:
    if s["symbol"] == pair:
        print("pricePrecision =", s.get("pricePrecision"))
        for f in s["filters"]:
            if f["filterType"] in ("PRICE_FILTER", "PERCENT_PRICE", "MIN_NOTIONAL", "NOTIONAL"):
                print("filter", f["filterType"], "=", json.dumps(f))
        break

# mark price for context
mp = bc._client.futures_mark_price(symbol=pair)
print("mark_price     =", mp.get("markPrice"))

# Reproduce the EXACT order as a TEST order (no real order created)
print("\n--- futures_create_TEST_order (no real order) ---")
try:
    resp = bc._client.futures_create_test_order(
        symbol=pair, side="SELL", type="STOP_MARKET",
        stopPrice=rounded, closePosition="true", workingType="MARK_PRICE",
    )
    print("TEST OK, resp =", json.dumps(resp))
except Exception as e:
    print("TEST RAISED:", type(e).__name__, "->", repr(e))

# Also show what the wrapper itself returns (this is what open_trade saw)
print("\n--- bc.place_stop_market_order return (REAL attempt) ---")
try:
    r = bc.place_stop_market_order(pair=pair, side="SELL", stop_price=raw)
    print("type =", type(r).__name__, "value =", json.dumps(r) if isinstance(r, dict) else str(r))
    oid = r.get("orderId") if isinstance(r, dict) else None
    print("orderId =", oid)
    if oid is not None:
        bc.cancel_order(pair, int(oid))
        print("(real order placed -> CANCELLED to keep investigation clean)")
except Exception as e:
    print("REAL RAISED:", type(e).__name__, "->", repr(e))
