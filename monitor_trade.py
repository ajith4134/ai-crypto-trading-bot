#!/usr/bin/env python3
"""Read-only live-position monitor. Blocks until the exchange state changes vs
a saved snapshot, prints the change, exits. NO orders placed. Re-launched each
event by the harness. State persisted to /tmp/mon_state.json so successive runs
continue the lifecycle (open -> ladder -> TP/SL -> close)."""
import os, time, hmac, hashlib, json, urllib.parse, urllib.request

K = os.environ["BINANCE_API_KEY"]; S = os.environ["BINANCE_API_SECRET"]
BASE = "https://fapi.binance.com"
STATE = "/tmp/mon_state.json"
POLL = 20; TIMEOUT = 2400  # 40-min heartbeat


def signed(path, params=None):
    p = dict(params or {}); p["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(S.encode(), qs.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{BASE}{path}?{qs}&signature={sig}",
                                 headers={"X-MBX-APIKEY": K})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def snapshot():
    pos = {p["symbol"]: {"amt": float(p["positionAmt"]),
                         "entry": float(p["entryPrice"]),
                         "upnl": float(p["unRealizedProfit"])}
           for p in signed("/fapi/v2/positionRisk") if float(p["positionAmt"]) != 0}
    orders = {}
    for o in signed("/fapi/v1/openOrders"):
        orders.setdefault(o["symbol"], []).append(
            {"type": o["type"], "side": o["side"],
             "stop": o.get("stopPrice"), "price": o.get("price"),
             "reduceOnly": o.get("reduceOnly")})
    for v in orders.values():
        v.sort(key=lambda x: (x["type"], str(x["stop"]), str(x["price"])))
    return {"pos": pos, "orders": orders}


def sig_of(snap):
    # signature excludes upnl (noise) — fire only on open/close/qty/order changes
    pos = {k: {"amt": v["amt"]} for k, v in snap["pos"].items()}
    return json.dumps({"pos": pos, "orders": snap["orders"]}, sort_keys=True)


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return None


prev = load()
cur = snapshot()
if prev is None:
    json.dump(cur, open(STATE, "w"))
    prev = cur
start = time.time()
while time.time() - start < TIMEOUT:
    cur = snapshot()
    if sig_of(cur) != sig_of(prev):
        # describe the change
        events = []
        pold, pnew = prev["pos"], cur["pos"]
        for sym in pnew:
            if sym not in pold:
                p = pnew[sym]
                side = "LONG" if p["amt"] > 0 else "SHORT"
                events.append(f"OPENED {sym} {side} qty={abs(p['amt'])} entry={p['entry']}")
        for sym in pold:
            if sym not in pnew:
                # closed — fetch realized pnl
                try:
                    inc = signed("/fapi/v1/income",
                                 {"symbol": sym, "incomeType": "REALIZED_PNL", "limit": 5})
                    rp = sum(float(i["income"]) for i in inc[-3:])
                except Exception:
                    rp = "?"
                events.append(f"CLOSED {sym}  realized_pnl≈{rp} USDT")
        # order/ladder changes for symbols still open
        for sym in pnew:
            if sym in pold and cur["orders"].get(sym) != prev["orders"].get(sym):
                od = cur["orders"].get(sym, [])
                desc = ", ".join(f"{o['type']}@{o['stop'] or o['price']}" for o in od)
                events.append(f"LADDER {sym} orders now: [{desc}]  upnl={pnew[sym]['upnl']}")
        if not events:
            events.append("state changed (orders): " + sig_of(cur)[:300])
        print("EVENT " + " | ".join(events))
        print("SNAPSHOT " + json.dumps(cur))
        json.dump(cur, open(STATE, "w"))
        raise SystemExit(0)
    time.sleep(POLL)

# heartbeat (no change within window)
opn = list(cur["pos"].keys())
print(f"HEARTBEAT no_change open={opn or 'FLAT'} " + json.dumps(cur["pos"]))
raise SystemExit(0)
