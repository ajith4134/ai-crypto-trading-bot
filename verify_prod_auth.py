#!/usr/bin/env python3
"""Read-only LIVE Binance futures auth verifier. Places NO orders.
Signs a GET /fapi/v2/account against fapi.binance.com using PROD_KEY/PROD_SECRET
from the environment, reports HTTP status + auth result + balance snapshot.
"""
import os, time, hmac, hashlib, json, urllib.parse, urllib.request

KEY = os.environ.get("PROD_KEY", "").strip()
SECRET = os.environ.get("PROD_SECRET", "").strip()
BASE = "https://fapi.binance.com"  # LIVE futures (not testnet)

if not KEY or not SECRET:
    print("ERROR: set PROD_KEY and PROD_SECRET env vars"); raise SystemExit(2)

ts = int(time.time() * 1000)
qs = urllib.parse.urlencode({"timestamp": ts, "recvWindow": 5000})
sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
url = f"{BASE}/fapi/v2/account?{qs}&signature={sig}"
req = urllib.request.Request(url, headers={"X-MBX-APIKEY": KEY})

try:
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode())
        print(f"HTTP {r.status}  AUTH OK  key={KEY[:4]}…")
        print(f"  canTrade        = {body.get('canTrade')}")
        print(f"  totalWalletBal  = {body.get('totalWalletBalance')}")
        print(f"  availableBalance= {body.get('availableBalance')}")
        print("  -> Production keys WORK. No order was placed.")
except urllib.error.HTTPError as e:
    raw = e.read().decode()
    print(f"HTTP {e.code}  AUTH FAILED  key={KEY[:4]}…")
    print(f"  body: {raw}")
    hints = {"-2015": "invalid key / IP not whitelisted / futures perm off",
             "-2014": "malformed/incorrect API key",
             "-1022": "bad signature (secret wrong)",
             "-2008": "invalid api-key id"}
    for code, msg in hints.items():
        if code in raw: print(f"  hint {code}: {msg}")
except Exception as e:
    print(f"NETWORK/OTHER ERROR: {type(e).__name__}: {e}")
