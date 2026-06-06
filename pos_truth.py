import os, time, hmac, hashlib, json, urllib.parse, urllib.request
K = os.environ["BINANCE_API_KEY"]; S = os.environ["BINANCE_API_SECRET"]

def signed(path, p=None):
    p = dict(p or {}); p["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(S.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = urllib.request.Request("https://fapi.binance.com" + path + "?" + qs + "&signature=" + sig,
                               headers={"X-MBX-APIKEY": K})
    return json.loads(urllib.request.urlopen(r, timeout=15).read())

acct = signed("/fapi/v2/account")
print("totalWalletBalance    =", acct["totalWalletBalance"])
print("totalUnrealizedProfit =", acct["totalUnrealizedProfit"])
print("availableBalance      =", acct["availableBalance"])
print("=== open positions ===")
for p in signed("/fapi/v2/positionRisk"):
    if float(p["positionAmt"]) != 0:
        amt = float(p["positionAmt"]); mark = float(p["markPrice"])
        line = "  {sym} amt={amt} entry={entry} mark={mark} notional={notl:.2f} uPnL={upnl} lev={lev} liq={liq} margin={mt}".format(
            sym=p["symbol"], amt=amt, entry=p["entryPrice"], mark=mark, notl=amt * mark,
            upnl=p["unRealizedProfit"], lev=p["leverage"], liq=p["liquidationPrice"], mt=p["marginType"])
        print(line)
print("=== recent ARB fills ===")
for t in signed("/fapi/v1/userTrades", {"symbol": "ARBUSDT", "limit": 10}):
    print("  {side} qty={qty} price={price} realizedPnl={rp} comm={c}{ca}".format(
        side=t["side"], qty=t["qty"], price=t["price"], rp=t["realizedPnl"],
        c=t["commission"], ca=t["commissionAsset"]))
