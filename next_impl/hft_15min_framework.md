# Next Impl — 15-min HFT/scalping framework gaps (from pasted research, 2026-06-01)

Source: user pasted GNN+LightGBM 15-min framework + 30s HFT framework + multi-stage
scanner framework. Cross-checked against disk. ~85% already built.

## Already built (do NOT re-implement)
- GNN sector-lead: `ml/gnn.py` (F24, GAT), `ml/gnn_multiscale.py` (F24M, 1m/5m/15m/1h fused)
- VPIN: `signals/engine.py:42`, EWMA-normalized, 10% composite weight
- OFI/OBI L1 + spread + jump_score: `signals/microstructure.py` (REST depth, 90s TTL)
- HMM regime: `ml/hmm.py` + hmm_regime_kill gate `engine.py:2346`
- XGBoost velocity predict-all: `prediction/xgb_predictor.py`, 30s refresh loop
- Funding-rate gate: `engine.py:788`
- Multi-stage scanner (vol → ADX → ML): `scanner/main.py`
- 48h time-barrier (Lopez de Prado) `risk/manager.py:889`; dead-trade exit `:931`

## Explicitly REJECTED
- 30s HFT regime: blueprint line 1779 "bot targets futures swing/momentum, not
  microsecond execution". Needs C++/Rust + sub-1ms inference — different product.
- LightGBM duplicate of XGBoost: identical architecture, ~3ms faster, not worth a dep.

## Implementing this session (all 3, confirmed)
1. **Hard minute time-stop** — `risk/manager.py`. New `bot:max_hold_minutes`
   (default 0 = OFF). Fires regardless of PnL/TP1 state, BEFORE the TP1-gated
   48h barrier. reason=`time_stop_minutes`. counter `trail:max_hold_minutes_count`.
   - DB: migration 030 extends trades_exit_reason_check. NOT a purge reason.
2. **Sympathy-pump (F24M)** — `signals/engine.py`. Parallel block to the F24
   leader-boost (engine.py:1795). Uses `gnn_multiscale.get_multiscale_leader(pair)`
   (leader, lead_candles, tf). If leader moved same dir as signal over lead window
   on that tf → ×1.12 boost. Gated is_active("F24M"). Advisory only.
3. **WebSocket order-book stream** — new `data/micro_ws.py` asyncio service.
   Binance combined diff-depth stream + canonical local-book maintenance
   (snapshot + buffered diffs, U/u sequence validation). Writes SAME `{pair}:micro:*`
   keys via shared feature fn refactored out of `compute_pair`. REST Celery scan
   stays as fallback. docker-compose service `micro_ws`. Capped to
   `micro:ws:max_pairs` (default 60) top scanner pairs for bounded memory.

## Checklist
- [x] F1 time-stop code + migration 030 + apply migration (verified in pg constraint)
- [x] F2 sympathy-pump block (F24M+F24 both ACTIVE — live, not dormant)
- [x] F3 micro_ws.py + refactor shared feature fn + compose service + entrypoint
- [x] websockets 16.0 + aiohttp 3.13.5 confirmed in image
- [x] deploy: restart brain+candlenet (bind-mount), up -d micro_ws
- [x] PROGRESS.md update (cont. 66)
- [x] Verify: micro_ws 60 pairs connected, 8500+ writes, micro:ts TTL 78-90s,
      brain sl_monitor ran 61 trades no error, 100 pairs fresh

## RESOLVED
- [x] USER chose "rebuild image now". Rebuilt trading-bot-app:latest x2 (build 1
      baked micro_ws+microstructure; build 2, pip CACHED, baked feed.py). Dropped
      micro_ws temp mounts. `docker compose up -d` recreated all 14 services on the
      baked image, healthy. Disk fine (44% used) — saved memory feedback-disk-not-full.

## BONUS FIX THIS SESSION — 30m candle data (data/feed.py)
- [x] Root cause: asyncio create_task GC footgun — 30m poll (tick % 360) GC'd by the
      synchronous GNN+TransferEntropy block right after it, every cycle. (1h was fine.)
- [x] Fix: _spawn() strong-ref helper + _poll_short_candles_and_log() per-TF logging.
      VERIFIED: 30m 0→100 pairs, `short_candles_polled interval=30m pairs=100`.

## REMAINING (future / separate decisions — keep file)
- [ ] SECONDARY DEFECT (found, NOT fixed): data/feed.py GNN/TE/PatchTST refresh is
      nested under `% 360` (30 min) but comments say "every 5 min" — 6× too slow.
      Moving to % 60 raises compute 6× → needs its own decision.
- [ ] v3 (optional): full pre-buffer-replay in micro_ws for zero resync thrash.
- [ ] Tuning (when ready to use scalping regime): set bot:max_hold_minutes>0;
      reset stale Redis key if pre-set (Redis-override-supersedes-default rule).

## Decisions (confirmed vs proposed)
- CONFIRMED: time-stop default OFF (0) — must not disrupt current swing posture.
- CONFIRMED: F24M boost ×1.12 < F24 ×1.15 to limit double-count when both fire.
- CONFIRMED: WS capped to 60 pairs; REST covers the tail; both write same keys.
- PROPOSED: WS depth level @100ms, limit=20 snapshot. Revisit if rate-limited.
