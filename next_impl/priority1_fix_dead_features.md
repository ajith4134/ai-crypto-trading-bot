# Priority 1 — Fix the 5 Dead Features (audit 2026-06-06)

Topic slug: `priority1_fix_dead_features`
Opened: 2026-06-07 (cont. 73). Continues the audit answer's "5 Implementation Priorities".
User chose: **All of Priority 1, in sequence.**

## Confirmed dead (Rule 2/9/13 — measured, not assumed)
Last 3000 signals: `bid_ask_imbalance`, `exchange_netflow_z`, `liq_nearest_above_pct`,
`liq_nearest_below_pct` all avg-abs = **0.00000**; `pattern_cluster_id >= 0` in **0/3000**.
No Redis keys exist for any of them.

## Root causes (all confirmed at source / live)
1. **liq_nearest_above/below_pct + liq_cascade_prob** — `data/liquidation_levels.py`.
   Producer exists + scheduled (celery beat 300s). AUTO-DISABLED: `liq:disabled=1`,
   `liq:reject:coinalyze_http_404 = 30`. Bug: `_fetch_coinalyze` hits `/v1/liquidations`
   → **404** (wrong path). Correct = `/v1/liquidation-history`, symbol `{base}USDT_PERP.A`.
   The 404 makes reject_frac=1.0 → re-trips deadlock (is_disabled) even though the proxy
   path would succeed → permanent disable. **SELF-PERPETUATING.**
2. **exchange_netflow_z** — `data/onchain_netflow.py`. AUTO-DISABLED: `netflow:disabled=1`,
   22 failures (native_ok=0, proxy_ok=0). Etherscan key IS set. Needs failure diagnosis.
3. **bid_ask_imbalance** — `prediction/features.py:169` READS `{pair}:bid_ask_imbalance`;
   **NO producer writes it**. Structurally dead. Derive from existing `{pair}:micro:ofi_l1`
   / book data written by micro_ws (cont.70 Track 2).
4. **pattern_cluster_id** — `features.py:162` READS `pattern:cluster_id:{pair}`;
   **NO producer writes it**. Needs a clustering-assignment task.

## Sequence (highest-confidence first)
- [x] **Step 1 — liq: DONE + DEPLOYED (cont. 73, verified live).**
      - `_fetch_coinalyze` now hits `/v1/liquidation-history`, symbol `{base}USDT_PERP.A`,
        builds 2 directional clusters: long-liqs (`l`) below price, short-liqs (`s`) above,
        band = `_atr_band` (3× 1m ATR, 1% floor). New shared `_atr_band` helper (proxy reuses).
      - Gated Coinalyze to majors whitelist `_COINALYZE_PAIRS` (Redis-tunable set
        `liq:coinalyze_pairs`) — free tier 429s above a few req/cycle; rest use proxy.
      - FIXED self-perpetuating deadlock: provider rejects (Coinalyze 429/404) are now
        telemetry-only (`liq:reject:{reason}`); the deadlock numerator (`liq:reject_count`)
        counts only refresh_one PRODUCER failures (all_sources_failed). Proxy backstops every
        pair → reject_frac≈0 → no false auto-disable.
      - Deploy: added `./data/liquidation_levels.py` + `./data/onchain_netflow.py` bind-mounts
        to `celery_worker` (data/ was image-baked, not mounted; disk ~90% full → no rebuild).
        `docker restart trading-bot-celery_worker-1` to reload the process (`up -d` no-ops on
        bind-mount-only changes).
      - VERIFIED live: refresh_all 300/300 ok, 4.55s, is_disabled=False, call=300/reject=0.
        BTC/ETH src=coinalyze dir=long; alts src=proxy. `{pair}:liq_nearest_above/below_pct`
        now non-zero (was 0.0 on 3000 signals). Beat runs it every 300s, 600s TTL.
- [~] **Step 2 — netflow: BLOCKED ON DATA (not a pipeline bug).** Confirmed live: all 3
      providers need paid access — cryptoquant http_401, glassnode http_401, coinmetrics
      community `400 metric FlowNetExUSD not supported` (PRO-only). Proxy can't help (it
      derives alts from BTC native z, and BTC native fails). The added ETHERSCAN key is NOT
      wired into this module's providers. Reviving requires EITHER a paid feed OR a custom
      Etherscan CEX-hot-wallet netflow builder (substantial new module). LEFT DISABLED on
      purpose — clearing `netflow:disabled` would just re-fail+re-disable. Recommend as a
      separate scoped task. The audit's "broken pipeline not missing data" is WRONG here.
- [x] **Step 3 — bid_ask_imbalance: DONE + DEPLOYED + VERIFIED LIVE (cont. 73).**
      Added a TRUE L1 (top-of-book) imbalance in `signals/microstructure.py`
      `compute_book_features` (best-bid vs best-ask resting size — distinct from the
      top-N `ofi`), written to the canonical consumer key `{pair}:bid_ask_imbalance`
      in `write_micro` (TTL 90s, `.get` keeps old callers safe). Both producers covered:
      primary `micro_ws` (sub-second) via a new `./signals/microstructure.py` bind-mount,
      and the REST fallback worker `celery_worker_candlenet` (already had `./signals`).
      Deploy: `docker compose up -d micro_ws` (new mount) + `docker restart
      celery_worker_candlenet`. VERIFIED via `prediction.features.live_features`:
      bid_ask BTC 0.63 / ETH 0.77 / BNB 0.42 (non-zero, varied, ≠ ofi_l1). Was 0.0/3000.
- [ ] **Step 4 — pattern_cluster_id: SCOPED — genuine ML build (next session).**
      No producer writes `pattern:cluster_id:{pair}`; no cluster model file exists.
      `pattern/clusterer.py:fit_clusters` + `pattern/live_capture.py:capture_for_active_pairs`
      (embedding capture) exist, but the assignment loop (embed pair pattern → nearest
      cluster → write key) was never built (Phase-B TODO). Build = fit clusters on captured
      embeddings → persist model → periodic assign task → write `pattern:cluster_id:{pair}`.
      Triggers Rule 14 (shadow) + Rule 7 (new ML module). Do as a focused task.

## Status (cont. 73): 3 of 5 features REVIVED (liq_nearest_above/below, liq_cascade_prob,
## bid_ask_imbalance) + verified end-to-end. 1 blocked-on-data (netflow). 1 scoped (cluster).

## Deploy
All bind-mounted (`data/`, `prediction/`) → `docker-compose restart brain celery_worker celery_beat`.
No image rebuild. Paper mode — reversible.

## Verify (Rule 4/12)
Each step: re-query the 3000-signal avg-abs; emit a Redis counter on every reject path.
Rule 14 (shadow): feature revival changes signal_strength inputs → watch 50 cycles before
trusting PnL deltas.
