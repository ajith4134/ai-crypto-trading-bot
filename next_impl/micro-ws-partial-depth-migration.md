# Next Impl — Microstructure: partial-book-depth WS for ALL pairs (kill production REST)

**Goal:** eliminate production `fapi.binance.com` REST weight for the microstructure layer
entirely, by moving ALL active pairs onto a self-contained WebSocket book feed — and fix the
micro_ws flapping/snapshot-failure at the same time. This is the durable follow-up to the
cont. 69x REST throttle (which only *bounds* the bleed; it does not remove it).

## Why (confirmed facts, cont. 69x 2026-06-03)
- Freshly rotated IP (34.85.74.56) re-accumulated production fapi weight 2 → ~1000+/min within
  an hour, **in paper mode**. Ban line ~2400/min. Root contributor = `signals.microstructure
  .scan_all` REST depth on a 15s beat across the full ~128-pair universe (~256 weight/scan).
- A throttle is now live (see PROGRESS cont. 69x) — bounds it but tail pairs still hit production
  REST. To take microstructure production-REST to **zero**, the WS producer must cover everything.

## Current architecture (read & confirmed)
- `data/micro_ws.py` — combined WS producer, covers TOP `micro:ws:max_pairs` (default 60) only.
  - `_WS_BASE = wss://fstream.binance.com/stream?streams=` (PRODUCTION — not testnet; confirmed
    line 55). No venue mismatch (earlier hypothesis disconfirmed).
  - `_DEPTH_SPEED = "@depth@100ms"` → **diff-depth** stream. REQUIRES a REST snapshot to seed each
    book (`_SNAPSHOT_URL = https://fapi.binance.com/fapi/v1/depth?...&limit=50`, line 56) + local
    sequence management (`OrderBook.apply_snapshot` / diff apply).
  - Observed failure mode (logs): `micro_ws_snapshot_failed error=` (empty) per pair, then
    `micro_ws_stream_error ... keepalive ping timeout` → reconnect → re-snapshot ALL pairs. So it
    FLAPS and re-burns production REST snapshots on every reconnect, and when books never seed the
    `{pair}:micro:*` keys go stale → `signals.microstructure` REST fallback fires → more weight.
  - Baked into the image (docker-compose line ~154: `data/micro_ws.py` NOT bind-mounted) → a change
    here needs a REBUILD + recreate of `micro_ws`, NOT just restart. CONFIRM before deploy.
- `signals/microstructure.py` — REST producer + the shared `compute_book_features(bids, asks, prev)`
  (pure math, takes `[[price,qty],...]`). BIND-MOUNTED into `celery_worker_candlenet` (restart-only).
- Consume side: engine/frontier read `{pair}:micro:*` and check `{pair}:micro:ts` freshness; stale =
  no micro gate (degrades gracefully). Producer-agnostic by design (write_micro shared).

## Proposed change (partial book depth)
Switch the WS from diff-depth to **partial book depth**: `@depth20@500ms` (or `@depth10@500ms`).
- Partial book streams push the FULL top-N bids/asks every interval — **no REST snapshot, no
  sequence numbers, no OrderBook state machine.** Payload `{"bids":[[p,q]..],"asks":[[p,q]..]}`
  feeds `compute_book_features` directly → `write_micro`. (top-20 is plenty: features use L1 OFI,
  spread, top-N VWAP-dev; `signals/microstructure._LEVELS`=10 today.)
- `@500ms` instead of `@100ms` cuts message volume ~5× → fixes the keepalive-ping-timeout flapping.
- Cover **ALL active pairs**, not just 60. 128 streams << Binance 1024-streams/connection limit.
  If volume is still heavy, split across 2 connections (e.g. 64 each) — keep them on PRODUCTION
  fstream regardless of `BINANCE_TESTNET` (microstructure wants mainnet liquidity; docstring
  signals/microstructure.py:13-15).

### Edits (proposed — not yet done)
1. `data/micro_ws.py`:
   - `_DEPTH_SPEED = "@depth20@500ms"`.
   - Delete `_snapshot`/`_SNAPSHOT_URL` usage + `OrderBook` diff machinery in `_run_stream`; on each
     msg, parse `ev["b"]/ev["a"]` (partial-depth field names — VERIFY exact keys: partial book uses
     `bids`/`asks`, combined-stream `data` wrapper still applies) → `compute_book_features` →
     `write_micro`. Keep the `write_interval` throttle + `prev_ofi` map.
   - Read pairs = full `scanner:active_pairs` (drop the `micro:ws:max_pairs` 60-cap, or raise to 200).
2. After WS covers all pairs, set the REST scan to standby: `micro:rest:max_pairs=0` +
   `micro:rest:fresh_skip_s` high (e.g. 30) so REST only fills genuine WS gaps. Keep it as the
   documented fallback (don't delete).

## Open questions / to verify before coding
- [ ] Exact partial-depth payload field names over the combined `/stream` wrapper (`data.b`/`data.a`
      vs `bids`/`asks`). Pull one raw frame to confirm.
- [ ] Is `data/micro_ws.py` baked (rebuild) or bind-mounted? docker-compose ~line 154 says baked →
      plan a REBUILD of `micro_ws` (verify image after, per docker-COPY-cache gotcha).
- [ ] Event-loop headroom for 128 pairs @500ms partial-depth on the micro_ws container (it flapped at
      60 @100ms; 128 @500ms ≈ similar msg/s — measure, split connections if it still times out).
- [ ] Root of the empty-string `micro_ws_snapshot_failed` (likely fapi throttle/timeout) — becomes
      moot after migration but note it.

## Checklist — Step 1 (micro_ws partial depth) ✅ DONE + VERIFIED 2026-06-03
- [x] Partial-depth frame shape: fields `b`/`a`, bids high→low / asks low→high (same as REST) — fed
      compute_book_features directly, sliced to _LEVELS for byte-identical features.
- [x] Edit micro_ws.py — `@depth20@500ms`, deleted OrderBook/_snapshot/_SNAPSHOT_URL, all-pairs
      (max_pairs default 0), ping_timeout 20→60.
- [x] Rebuilt trading-bot-app:latest; verified IMAGE has new code + old snapshot code gone (docker run
      grep); recreated micro_ws; in-process confirmed.
- [x] Verified: micro_ws_connected pairs=136 stream=@depth20@500ms; writes ~125/s; 135/136 pairs fresh;
      no flap; production fapi weight 104→~6-8/min.
- [x] REST scan left as throttled fallback (micro:rest:* levers); not deleted.

## ⚠️ ALL /market streams need the ROUTED endpoint (learned cont. 69x)
Legacy unrouted path for /market streams was decommissioned 2026-04-23. kline/markPrice/aggTrade/
forceOrder return SILENT NO-DATA on /ws and /stream; use **`wss://fstream.binance.com/market/stream`**
(+ SUBSCRIBE or ?streams=). Only @depth (/public) works unrouted. See [[reference_binance_data_sources]].

## Steps 2+3 ✅ DONE — Live klines (corpus) via WS, 2026-06-03
- [x] **Live klines** → data/kline_ws.py NEW service: SUBSCRIBE `<sym>@kline_{1m,5m,15m,30m,1h}` for all
      active pairs on `/market/stream`; writes CLOSED candles to Redis ZSETs klines:ws:{pair}:{itv}.
      ml.klines_corpus.incremental_from_ws merges them into the CSV corpus (byte-identical, ZERO REST).
      celery update_klines_corpus_task prefers this (corpus:ws_klines_enabled=1); REST path gated behind
      corpus:incremental_enabled. Verified: mode=incremental_ws added=1916 tf_written=618, fapi weight ~94.
      Caveat: small corpus gap [daily-bulk-last .. kline_ws-start] until next data.binance.vision bulk_topup.

## Step (mark+funding) ✅ DONE + VERIFIED 2026-06-03 (cont. 69x item 1)
- [x] **Mark price + funding** → `!markPrice@arr@1s` on /market/stream. WS loop `_ws_mark_price_loop`
      ALREADY existed (cont. 47) but the RUNNING image was STALE — connected to the decommissioned
      `/ws/!markPrice@arr` path (silent no-data), so mark prices were actually coming from the 5s REST
      premiumIndex poll (production fapi weight in live mode). Source was fixed to /market/stream in
      cont. 69x but never redeployed.
- [x] Made REST `_poll_mark_prices` (premiumIndex) a STANDBY fallback: data/feed.py `data_loop` now
      polls REST only when the WS freshness beacon `feed:ws_mark:fresh` (TTL 30s) is stale; fallback
      logs `mark_rest_fallback` + sets `feed:mark:rest_fallback` counter (Silent Rejection Rule).
- [x] WS loop now also feeds in-process `_price_history` + `mark_window` on a throttled POLL_INTERVAL
      (5s) cadence so OFI/VPIN/Turbulence (F15/F26/F28) keep IDENTICAL semantics; MARK_PRICE/LAST_PRICE
      still update at full 1s WS rate. `feed:mark:source` = ws | rest_fallback.
- [x] data/feed.py is BAKED (data_feed mounts only /models) → REBUILT trading-bot-app:latest, verified
      IMAGE has new code + old /ws/ path gone (docker run grep), recreated data_feed.
- [x] Verified in-process: ws_mark_price_connected url=.../market/stream?streams=!markPrice@arr@1s;
      beacon=643 (full universe), source=ws; mark ticks ~1s (7/10 changed in 3s); OFI/VPIN fresh
      non-zero; REST premiumIndex fired ONCE at cold start (4s before WS connect) then never again →
      production fapi mark weight ~0 in steady state. (testnet endpoint now; flips to fstream prod in live.)
- NOTE: 24h ticker (`/fapi/v1/ticker/24hr`, _poll_24h_tickers) is STILL REST every 30s — out of scope
      for this item (mark+funding); a separate `!ticker@arr` WS migration if zeroing it matters.

## Step (liquidations) ✅ DONE + VERIFIED 2026-06-03 (cont. 69x item 2)
- [x] **Liquidations** → `!forceOrder@arr` on /market/stream (event-driven). NEW free WS source
      (replaces NO REST). NEW service data/liq_ws.py: rolling per-pair notional window →
      `{pair}:liq_flow_dir|liq_flow_intensity|liq_buy_notional|liq_sell_notional` + market-wide
      `liq:global_rate` (USD/min), short TTL (quiet pair → stale → neutral). Levers liq:ws:*.
      Semantics: SELL=long-liq→"short"(bearish), BUY=short-liq→"long"(bullish) — matches _summarise.
- [x] FULL F58 wiring (owner chose entry+exit). ENTRY: data/liquidation_levels.realtime_flow +
      _effective_cascade blend live prints into cluster levels (neutral→flow sets dir; agree→prob↑;
      conflict→prob×0.5); get_bonus routes through it (engine.py unchanged — already calls get_bonus).
      EXIT: risk/frontier/exit_kill_switch.evaluate_liq_cascade_kill (registered in decision.py chain) —
      adverse cascade → tighten_sl_mult 0.5 (intensity≥0.4) or force_close (intensity≥0.8 AND
      liq:global_rate≥$50M/min systemic). Levers liq:exit_*.
- [x] DEADLOCK-INDEPENDENCE FIX: F58 cluster providers (coinglass/coinalyze/proxy) have been
      auto-disabled ~2 days (liq:disabled=1, 30/30 reject — no API keys). The WS flow is an
      INDEPENDENT healthy source, so _effective_cascade zeroes the cluster half under is_disabled()
      but lets WS flow through (own switch liq:ws:disabled). Entry bonus is therefore LIVE on WS flow
      alone despite the cluster deadlock.
- [x] data/liq_ws.py bind-mounted (compose, no rebuild for producer); data/liquidation_levels.py +
      redis_keys.py are BAKED → rebuilt trading-bot-app:latest, recreated brain (risk/frontier via
      ./risk bind-mount). Started liq_ws service.
- [x] VERIFIED: liq_ws_connected !forceOrder@arr; liq:global_rate≈16k/min, per-pair buy/sell split
      correct; in brain — get_bonus +18/-18 via injected WS cascade (cluster still disabled), 0 when
      liq:ws:disabled=1; evaluate_liq_cascade_kill → tighten on adverse / None when aligned; brain clean.
- NOTE: cluster LEVELS half stays dormant (no provider keys) — pre-existing, out of scope; WS flow
      now carries the live liquidation entry signal. Paper-first per [[project_judge_replaces_entry_gates]].

## Step (live candles) ✅ DONE — verified 2026-06-04 (cont. 70d)
- [x] **Live candles for trading** ALREADY migrated in data/feed.py (`_poll_candles`/`_poll_short_candles`
      → `_collect_candles` builds CANDLES from klines:ws zset + WS-fed CSV corpus, REST gated cold-start
      fallback only). Levers feed:candles:ws_enabled / feed:candles:rest_fallback_enabled; beacons
      feed:candles:source:{itv} (ws|mixed|rest). The prior checklist mislabeled this as remaining.

## Step (candle REST leak) ✅ FIXED + VERIFIED 2026-06-04 (cont. 70d)
- [x] ROOT CAUSE: scanner rotates `scanner:active_pairs` every ~20 min, but data/kline_ws.py re-read the
      universe ONLY on its 12h connection rotation → pairs rotating IN got no WS klines for up to 12h, so
      `_poll_candles` fell back to production /fapi/v1/klines for them every cycle (the persistent
      "candles_rest_fallback pairs=2" bleed). Same churn as the active-pairs-count question.
- [x] FIX: data/kline_ws.py — added `_resubscribe_loop` (background task on the live socket) that diffs the
      active set every `klines:ws:resubscribe_s` (default 45s) and SUBSCRIBEs newly-added streams; additive
      within a session (12h rotation prunes). kline_ws.py is BIND-MOUNTED → `restart kline_ws` (no rebuild).
- [x] VERIFIED: kline_ws reconnected 33 pairs/165 streams, klines:ws:closed incrementing (read loop intact);
      candles_rest_fallback = 0 over 80s post-deploy (was firing ~every 5-min cycle before).

## Step (24h ticker) ✅ DONE + VERIFIED 2026-06-04 (cont. 70d)
- [x] **24h ticker** → `!ticker@arr` on /market/stream. NEW `_ws_ticker_loop` in data/feed.py writes the SAME
      keys (TICKER_VOLUME_24H ← base vol `v`, TICKER_CHANGE_24H ← pct `P`) so scanner volume sort + dead-pair
      filter are unchanged. REST /fapi/v1/ticker/24hr (weight 40) is now a STANDBY: data_loop calls it only
      when beacon feed:ws_ticker:fresh (TTL 60s) is stale, with `ticker_rest_fallback` log + counter.
- [x] data/feed.py is BAKED (data_feed mounts only models + historical:ro) → REBUILT trading-bot-app:latest,
      verified IMAGE has new code (docker run grep = 7 hits), recreated data_feed.
- [x] VERIFIED: ws_ticker_connected fstream.binance.com/market/stream?streams=!ticker@arr; feed:ticker:source
      =ws, feed:ws_ticker:fresh=145; one cold-start ticker_rest_fallback BEFORE WS connect then 0 recurrence.
      Production fstream (not testnet) is correct — market data needs real mainnet liquidity, zero weight.

## REMAINING (only by-design REST left)
- [ ] **Open interest** has NO Binance WS — keep openInterestHist REST (weight 0, separate 1000/5min pool;
      NOT the ban cause) or data.binance.vision daily metrics; Coinalyze (free 40/min) optional fallback.
      THIS IS BY DESIGN — not a leak. All other production fapi REST is now WS or gated cold-start fallback.
- [ ] Final live-mode confirmation: re-verify production fapi weight ~0 once TRADING_MODE=live is flipped
      (paper/testnet now). Then this file can be deleted.

## Handoff
cont. 69x throttle is LIVE (signals/microstructure.scan_all, Redis levers micro:rest:*). This file is
the *next* step to remove production REST entirely. Related: [[project_live_trading_blocked]] (never
aggressive fapi REST — this is the structural fix), [[reference_service_bind_mount_map]] (deploy
targets), [[feedback_docker_copy_cache]] (verify the IMAGE after the micro_ws rebuild).
