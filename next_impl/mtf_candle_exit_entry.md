# MTF Candle-Based Entry & Exit — Audit + Proposed Additions

Topic slug: `mtf_candle_exit_entry`
Created: 2026-05-27 (cont. 53 prep)
Owner: anushadudekula71@gmail.com
Trigger prompt: User asked to monitor 1m/5m/15m of all 60 scanner pairs to pick entries, then continuously observe candles to drive TP/SL/trailing. Also said the "SL tail-gating only existed at 50% of peak profit" — that framing is **stale**; current code is the cont. 44 ratchet with 80%+ floor.

---

## A. On-disk reality (Rule 2 verified — read directly, not inferred)

### Entry side — already MTF-candle-driven
| Concern | File | Status |
|---|---|---|
| Scanner uses candle setup as a criterion (F50a) | `scanner/main.py:score_candle_setup` + `compute_composite` | **DONE** — weight 0.20 (rebalanced cont. 50). Reads `{pair}:15m:candle_forecast`, `{pair}:1h:candle_forecast`, `{pair}:15m:exhaustion_score`. |
| Multi-TF direction cascade (F50b) | `signals/multi_tf_cascade.py` (250 lines) | **DONE** — 1h/15m/5m votes (+1m logged, +4h veto). Unanimous = conf 100, 2-of-3 = 67, split = OFI tiebreak. |
| Magnitude filter on signal | `signals/engine.py:299-301` | **DONE** — `_mags = [abs(float(fc.get("mag1", 0.0))) for fc in _forecasts.values()]`, rejects tiny moves. |
| RL Entry Timing (F48 §Idea C) | `ml/entry_timing_agent.py` (386 lines) | **CODE READY, INACTIVE** — gated `paper_closed ≥ 1000`. PPO 22-dim state, actions wait/enter/skip. Falls back to immediate-enter when inactive. |
| CandleNet 1m/5m/15m forecast feed | `data/atr_producer.py`, `ml/candlenet.py`, pretrainer pipeline | **TRAINING** — cont. 49 pretrainer rebuild was ~5 h; ATR side-effect to `{pair}:atr` is what feeds the SL width. |

### Exit / Trailing side — already multi-path, 80%+ floor (NOT the old 50%)
File: `risk/manager.py` lines 451–637.

Three parallel ratchet candidates run each tick; **the tightest wins**:

1. **Path A — %-activation, vol-anchored (cont. 23 original)**
   - Fires when `peak_profit_pct >= activation_pct`.
   - `lock_frac` comes from F47 Brain-learner (`risk/trail_params.py`), **clamped 0.80–0.92**.
2. **Path B — $-tier activation (cont. 44 user-mandated)**
   - Fires when `fresh_peak_pnl >= tier_trigger_usdt` from `config.risk.profit_lock_tiers`.
   - Per-tier `lock_pct` is mandated ≥0.80, scaling up toward 0.92 for big peaks.
   - Fixed the leveraged-trade bug where 3.3 % peak on a $300 notional ($10) never armed A (activation typically 4.5 %) but is real money worth locking.
3. **Path C — Chandelier Exit (F51d cont. 51)**
   - `chandelier_sl = HH − ATR × mult` (long) / `LL + ATR × mult` (short).
   - Multiplier regime-aware: bull/bear 2.0×, unknown 2.5×, turbulent 3.0× (standard Charles Le Beau 2.5–3.0).
   - Only counts as a profit-protector — never widens below entry.

Progressive trail distance (when activated): `trail_mult = max(0.4, min(2.0, 1.5 / sqrt(profit_ratio)))` — tightens as profit grows, never widens.

Per-trade overrides honored: `redis_keys.TRADE_TRAIL_ACT_OVERRIDE`, `TRADE_TRAIL_DIST_OVERRIDE` (Brain's voice, highest precedence).

Magnitude-driven TP (NOT SL): `risk/manager.py:compute_dynamic_tp` already exists — TP1 from `mag1_avg` across 1m/5m/15m CandleNet, TP2 from `mag3_avg`. Stored on each trade as `trade:{id}:mag1_pct` / `mag3_pct`.

**Conclusion: the "50% peak" trailing is gone. Current floor is 80 %. The user's prompt is based on outdated info.**

---

## B. Real gap analysis — what's missing vs the user's idea

User's ask, translated:
1. *Scan 60 pairs across 1m/5m/15m to pick the trade.* → already done (F50a + F50b + entry_timing_agent).
2. *Wait for the perfect entry.* → entry_timing_agent does exactly this once `paper_closed ≥ 1000`. **Pre-1000 we just enter on signal — no candle-close confirmation.**
3. *After entry, observe candles continuously to drive TP/SL/trailing.* → the ratchet does this per tick, but **it doesn't react to fresh candle-close events specifically.** It treats every tick equally.

So the genuine gaps are:

| # | Gap | Severity | Fix complexity |
|---|---|---|---|
| **G1** | No candle-close-confirmation entry filter for pre-1000-trade phase | medium | small (~30 LoC in signals/engine.py) |
| **G2** | Reversal-triggered ratchet tightening: when 1m + 5m CandleNet dir flips opposite to our trade direction, ratchet should jump (e.g. 80 → 95 %) | high | small (~40 LoC in risk/manager.py) |
| **G3** | Exhaustion-triggered ratchet tightening: when both 1m + 5m exhaustion scores are elevated (in our favour) → tighten to 90 % | medium | small (~25 LoC) |
| **G4** | 15m close-direction veto on holding the position: if 15m closes opposite for N consecutive bars, force close at current ratchet | medium | small (~30 LoC) |
| **G5** | No per-trade live MTF monitor that triggers on **new candle close events** (currently risk/manager scans every tick = noisy) | low-med | medium (~80 LoC; subscribe to candle stream) |

G2 is the highest-value, lowest-risk addition. It directly addresses the user's worry about "small profit many and one huge even bigger loss" — the huge loss usually happens when the trend has clearly reversed on multiple TFs and the ratchet still sits at the 80 % floor of a $50 peak, letting price wander back to $0.

---

## C. Confirmed vs Proposed decisions

### Confirmed (user already mandated, NOT touching)
- Profit-lock floor stays ≥ 80 %. No tier or floor is ever lowered. (memory: feedback_profit_lock.md)
- DCA stays disabled permanently. (memory: feedback_dca_disabled.md)
- Full-deploy mode (`bot:full_deploy_mode=1`) remains on. (memory: feedback_full_deploy_mode.md)
- Every reject/skip path must emit log + counter. (memory: feedback_silent_rejection.md)
- Any RL gate needs a deadlock detector. (memory: feedback_rl_deadlock_detector.md)

### Proposed (need user OK before code)
- **P1 — Multi-TF reversal ratchet** (Gap G2). When `{pair}:1m:candle_forecast.dir1` AND `{pair}:5m:candle_forecast.dir1` both invert relative to trade direction by `> NEUTRAL_BAND + 0.05`, override `lock_frac → max(current, 0.95)`. Only tightens (mandate-safe). Emits `trail:reversal_tighten_count` + per-trade reason log.
- **P2 — Exhaustion ratchet** (Gap G3). When `exh_1m + exh_5m >= 4.0` AND trade is in profit AND exhaustion direction == our direction (i.e. the move is exhausting in our favour), tighten `lock_frac → max(current, 0.90)`. Counter `trail:exhaustion_tighten_count`.
- **P3 — 15m close-direction veto** (Gap G4). If last `N=3` closed 15m candles all close opposite to trade direction (1m/5m noise filtered out), close at current SL via `engine.close_trade(reason="mtf_15m_reversal_confirmed")`. Counter `trail:mtf_15m_force_close_count`.
- **P4 — Candle-close entry confirmation, pre-1000** (Gap G1). For paper_closed < 1000, before opening a trade, require the most recent 1m candle close to be in the signal direction (close above open for long, below for short). Bypass when CandleNet `dir1 > 0.7` already (model is high-conviction). Counter `signal:reject:candle_close_confirm`.

All four are **additive tightenings / additive filters** — never relax an existing constraint, fully compatible with the cont. 44 mandate and the silent-rejection rule.

### Deferred (not in this slice)
- D1 — Live MTF candle-close subscriber per open trade (Gap G5). Adds a new Celery beat task or Redis pub/sub consumer. Bigger surface area; do later only if P1–P4 don't deliver enough lift.
- D2 — Replacing the F47 Brain-learner with a full PPO exit policy (FinRL/crypto-rl-style). Heavy lift; gated on P1–P4 telemetry showing residual loss buckets.

---

## D. Open-source / online references (saved per Rule 6)

- QuantPedia — *How to Design a Simple Multi-Timeframe Trend Strategy on Bitcoin* (candle-close trailing improves Sharpe 1.07 / Calmar 0.87): https://quantpedia.com/how-to-design-a-simple-multi-timeframe-trend-strategy-on-bitcoin/
- FMZ Quant — *ATR Dynamic Trailing Stop Loss Quantitative Trading Strategy*: https://medium.com/@FMZQuant/atr-dynamic-trailing-stop-loss-quantitative-trading-strategy-3edb43e21e0c
- StockCharts ChartSchool — *Chandelier Exit* (anchors our F51d): https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/chandelier-exit
- LuxAlgo — *HTF Reversal Divergences*: https://www.luxalgo.com/library/indicator/htf-reversal-divergences/
- TradingView — *Candle Based Trend Reversal (Multi-Timeframe)*: https://www.tradingview.com/script/XWkb7qnN-Candle-Based-Trend-Reversal-Multi-Timeframe/
- BingX learn — *Multiple Timeframe Analysis for Crypto Entries and Exits*: https://bingx.com/en/learn/article/how-to-use-multiple-timeframe-analysis-for-better-entry-and-exit-points-in-crypto-trading
- Altrady — *Candle-Close cooldowns for SL triggers*: https://support.altrady.com/en/article/smart-orders-stop-loss-including-optional-cooldowns-time-and-candle-close-new-1hqv471/
- GitHub — `sadighian/crypto-rl` (DDQN limit-order-book exits): https://github.com/sadighian/crypto-rl
- GitHub — `AI4Finance-Foundation/FinRL` (PPO/SAC exit policies): https://github.com/AI4Finance-Foundation/FinRL
- GitHub — `berendgort/FinRL_Crypto` (46 % less overfitting): https://github.com/berendgort/FinRL_Crypto
- arXiv ref already in blueprint §F48: 2605.00875 CNN, 2502.19349 CryptoPulse, 2504.17079 Transformer+GRU.

Consensus across sources: **multi-TF agreement gating exits** + **ATR-anchored trail floor** + **candle-close confirmation** is the production-grade combo. Our current code already has two of three (multi-TF on entry, ATR-anchored Chandelier on exit). The candle-close-confirmation lens is the missing piece — that's exactly what P1/P3 add.

---

## E. Checklist (post-approval)

- [ ] User picks which of P1, P2, P3, P4 to ship in cont. 53 (default: all four).
- [ ] Add config keys to `config.yaml` → `risk.mtf_reversal_lock_frac: 0.95`, `risk.exhaustion_lock_frac: 0.90`, `risk.mtf_15m_reversal_bars: 3`, `signal.candle_close_confirm_until_paper_closed: 1000`.
- [ ] Implement P1 in `risk/manager.py` between the Path-A block (~L506) and the Path-C block (~L525) — adds a Path D: MTF reversal candidate.
- [ ] Implement P2 in `risk/manager.py` adjacent to P1 — Path E.
- [ ] Implement P3 in `risk/manager.py` outside the ratchet block (it's a force-close path, not an SL move).
- [ ] Implement P4 in `signals/engine.py` next to the magnitude filter (~L299).
- [ ] Add Redis counters per silent-rejection rule: `trail:reversal_tighten_count`, `trail:exhaustion_tighten_count`, `trail:mtf_15m_force_close_count`, `signal:reject:candle_close_confirm`.
- [ ] Add deadlock detector for P4 (per RL-deadlock-detector rule): auto-disable when reject rate > 80 % over 50+ signals.
- [ ] Blueprint update: append cont. 53 line to Section 32.1 (changelog) noting Paths D + E + MTF veto + pre-1000 candle-close filter.
- [ ] Rebuild brain + celery_worker. Verify counters increment within 1 h of recreate.
- [ ] Update PROGRESS.md with cont. 53 entry covering all four paths + telemetry checks.

---

## F. Session handoff

If a new session picks this up:
1. **Stale framing alert**: the user originally said "SL trailing exists only at 50 % of peak". That's the *cont. 23 original*. The current code (cont. 44) is **80–92 % via three parallel paths**. Lead any reply by correcting this politely, then propose the four additions above.
2. **Don't touch** the 80 % floor, the tier table, or DCA — all user-mandated and memory-locked.
3. **Honesty (Rule 4)**: any code shipped must be production-grade. The four P-paths are intentionally small (~120 LoC total) — that's correct for additive guards, not a simplification.
4. **Rule 5**: if implementing P1–P4 reveals that the ratchet's 3-path structure is hard to extend, redesign the dispatcher (e.g. a list-of-candidates pattern) BEFORE adding more paths. Right now adding two more inline branches is fine.

---

## G. Decision points awaiting user

1. Ship all four P1–P4, or a subset?
2. Cont. 53 — same session, or open it as the next session and stop here?
3. Should P3 (15m force-close after 3 opposite closes) use the current SL price (safer) or close immediately at mark (faster)? Recommend SL — matches the "trailing SL is the only exit" blueprint mandate.

---

## H. SHIPPED — cont. 53 (2026-05-27)

User authorised override of cont. 44 0.80 floor + asked for all four P-paths + regime-adaptive design from honest review.

**Shipped (all six paths):**
- ✅ Regime-adaptive lock_frac (Paths A + B): bull/bear [0.40, 0.55], turbulent [0.65, 0.75], chop/unknown [0.80, 0.92]
- ✅ Chandelier widened: bull/bear 2.0× → 3.5×
- ✅ Path D — MTF reversal ratchet (1m + 5m dir1 flip → 0.95 snap)
- ✅ Path E — Exhaustion ratchet (exh_1m + exh_5m ≥ 4.0 → 0.90 snap)
- ✅ Path F — 15m close-direction veto (3 opposite closes → force-close at SL)
- ✅ P4 — Pre-1000 candle-close entry confirm with RL-deadlock detector

**Files:**
- `risk/manager.py` (~150 LoC added: helper, hoisted regime read, scaling in A/B, Chandelier widen, Paths D/E/F)
- `signals/engine.py` (~60 LoC added: P4 block)
- `BOT_BLUEPRINT.md` (cont. 53 changelog line)
- Memory `feedback_profit_lock.md` (full rewrite)
- `PROGRESS.md` cont. 53 entry

**Verify (post-rebuild):**
```
redis-cli get current_regime
redis-cli get trail:regime_scaled_count
redis-cli get trail:mtf_reversal_tighten_count
redis-cli get trail:exhaustion_tighten_count
redis-cli get trail:mtf_15m_force_close_count
redis-cli get signal:p4:total_calls
redis-cli get signal:p4:disabled    # should NOT be "1"
```

**Kill switches:**
- `risk:regime_scaling_disabled=1` → revert to cont. 44 static 0.80 floor
- `signal:p4:disabled=1` → P4 reject-loop auto-set (manual delete to re-enable)

**File clears after Rule 2 verification of running counters (do not delete until counters confirm code path active).**
