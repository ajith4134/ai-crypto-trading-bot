# Phase 1 — Build the Instrument (decision ledger + backtest + real metrics)

Source of truth: `PROFESSOR_AUDIT.md` → INSTRUCTION MANUAL → PHASE 1 (lines 422-428).
Started 2026-06-04 (cont. professor session). Rules on: 9, 2, 1, 5, 10.

GATE (must hold to pass Phase 1): *any change's OOS effect on expectancy/DD is stateable + reproducible, else STOP.*

## VERIFIED FACTS (Rule 2 — read from source, not assumed)
- Phase 0 live since 2026-06-04 08:12 UTC (leverage locked 3-5x via Redis, 10 self-mod beat tasks frozen).
- **Decision-ledger DATA ALREADY EXISTS** — do NOT build a new capture table:
  - `signals` table: accepted(bool), rejection_reason(text, SINGLE reason), signal_strength + potential_score
    (FINAL score), market_regime, direction, pair, feature_vector(jsonb), direction_confidence,
    debate_verdict, predicted_peak_profit_pct, trade_id(FK→trades), brain_stage, is_paper, generated_at.
    Audit queried it: 329,632 rows, 7.11% accept rate.
  - `counterfactuals` table: signal_id(FK), would_have_won, peak_profit_pct, peak_loss_pct,
    trailing_sl_exit_pct, miss_tag (8 enum), pair, direction, signal_time.
  - `trades` table: net_pnl_usdt, status, exit_time, direction, market_regime, leverage, capital_usdt,
    feature_vector, signals_at_entry.
- Existing instrument (READ-ONLY, historical-trades only — both survivorship-biased):
  - `tools/professor_metrics.sql` — one-shot expectancy/PF/Sharpe/maxDD/edge-map/walk-forward-by-week + signals attribution.
  - `brain/ab_lab.py` — OOF logistic score over closed trades w/ feature_vector; F-033 AUC 0.548.
- Kline corpus: `data/historical/{PAIR}/{interval}.csv`, cols `timestamp(ms),open,high,low,close,volume`.
  511 pairs; intervals 1m/5m/15m/30m/1h/4h/1d; fresh (today); uid-999-owned. ~1100 1h bars/pair.
- DB DSN inside containers: `$DB_CONNECTION_STRING` = postgresql://botuser:***@postgres:5432/trading_bot.
- pretrainer/walk_forward.py exists (audit said "extend"); separate concern (model pretraining) — Phase-1 backtester is new.

## THE REAL GAP (honest, Rule 4)
- 1.1 ledger: ~80% there (data exists). Missing = clean unified ACCESS view + live write-path completeness check.
- 1.3 metrics: SQL is one-shot. Missing = REUSABLE module both ledger-report and backtester call.
- 1.2 backtest: GENUINELY MISSING. Existing tools replay only over trades that OPENED (survivorship). Need a
  no-lookahead walk-forward replay over the KLINE CORPUS that can simulate a pluggable score_fn the old core
  never took, with a real exit model → OOS expectancy/PF/Sharpe/maxDD. This is the keystone for the GATE.

## BUILD (this session) — all in `tools/professor/`, OUTSIDE live decision path (zero rebuild, run via docker exec)
- [x] `metrics.py` — pure fns: expectancy, profit_factor, sharpe, sortino, max_drawdown, win_rate(descriptive),
      summarize(pnls), walk_forward_stability(periods). Self-test reconciles F-028 (net -837, PF 0.982, exp -0.082).
- [x] `ledger.py` — creates idempotent SQL VIEW `decision_ledger` (signals⨝counterfactuals⨝trades, one row/candidate);
      Python reporter: accept-rate, kill distribution, edge-map, + WRITE-PATH COMPLETENESS check (R10: are rejects
      logged in the LAST N hours, not just historically?).
- [x] `backtest.py` — no-lookahead walk-forward kline replay; pluggable score_fn; live capital-ladder exit.
      **VALIDATED via edge-over-null (see below).** Baseline score_fn = OFI/momentum + regime (audit Phase-3.1).
- [ ] CONFIRMED vs PROPOSED: exit-model fidelity (bracket vs live capital-ladder) — PROPOSED simple bracket first;
      upgrade to ladder semantics once baseline runs. Slippage/fees model — PROPOSED taker fee 0.05%/side flat.

## CRITICAL LEARNING — backtester v1 was INVALID; v2 fix = EDGE-OVER-NULL (2026-06-04)
- v1 reported baseline exp +$0.17 / PF 1.19 / 79% WR — looked great. DISCONFIRMING placebo (random direction)
  scored +$0.21, BEATING the real signal. => the "edge" was a STRUCTURAL ARTIFACT of the capital-ladder exit
  (tight trailing lock vs wide stop on drifting/autocorrelated 15m data prints + PnL for ANY entry).
- FIX: headline metric = EDGE = base_net - null_net, where null_net = analytic coin-flip expectation on the
  SAME bar = 0.5*(long_net + short_net). The exit-model+market bias lands in BOTH base and null and CANCELS.
  Placebo EDGE == 0 by construction (validated: placebo edge +$0.005, t=+0.26 — insignificant).
- VALIDATED RESULT: baseline OFI+regime EDGE = -$0.026, t=-1.27 (NO real direction skill). This INDEPENDENTLY
  corroborates F-033 (OOF AUC 0.548) via corpus replay — a different method, same verdict.
- GATE (now operational): a Phase-2 change beats baseline iff it raises EDGE expectancy to a SIGNIFICANT
  positive (t>=2) that is stable across walk-forward folds, without worsening EDGE maxDD. NULL/BASE shown for
  transparency but are NOT the scoreboard — EDGE is.
- Rule-5 note (deviation owned): spec said "extend pretrainer/walk_forward.py"; built standalone brain/professor/
  backtest.py instead (walk_forward.py is model-pretraining, different concern). Flagged, not silent.

## DECISIONS (confirmed)
- Instrument lives beside old core (Owner Q2 answer pending but audit verdict = A/B beside old). NO live-path edits.
- Winrate is DESCRIPTIVE ONLY everywhere (F-006 / 1.3). North star = expectancy, PF, Sharpe/Sortino, maxDD, OOS stability.

## PHASE 2.1 — COLLAPSED CORE built + GATE verdict (2026-06-04)
- `brain/professor/core.py`: ONE scoring fn (base + Σcomponents − Σpenalties), regime counted ONCE (single
  multiplier term), ONE threshold, EVERY term logged (Decision.terms). A/B-able on the GATE via make_score_fn.
  4 regime→direction POLICIES: momentum_only / with_trend / f029_no_bear_short / long_bias.
- GATE RESULT (15m, 40 pairs, 4 folds): ALL policies indistinguishable from coin-flip.
  momentum −0.020(t−0.94) | with_trend −0.030(t−1.41) | f029_no_bear_short +0.008(t+0.39, 75% folds+) |
  long_bias −0.023(t−1.10). Best=f029 but NOT significant.
- DIRECT F-029 test (per-regime long vs short exp on clean corpus, Feb-Jun 2026, regime-balanced 48%bear/46%bull):
  bear short−long = −0.090 (t−1.31, RIGHT SIGN but not sig); bear+short is +0.213 ABSOLUTE (mildly positive!),
  bear+long +0.303. turbulent+short −0.258 (worst cell). => the audit's −$3524 bear+short was LARGELY OLD-CORE
  SELECTION BIAS (which bear+short trades the gauntlet picked) and/or live costs my idealized exit omits
  (funding/squeeze-slippage) — NOT a tradeable regime law. "Exclude bear+short → +$2687" does NOT survive the GATE.
- CONCLUSION: entry-direction lever is weak (corroborates F-033 by a 2nd independent method). Per audit Phase-2
  implication (F-034/F-025), pivot value to EXITS + the one untested entry lever = CROSS-SECTIONAL momentum
  (xsmom_rank was F-033's most-stable feature; needs cross-pair time-aligned replay — not yet built).

## CROSS-SECTIONAL MOMENTUM tested (brain/professor/xsmom.py, 2026-06-04)
- Market-neutral long-short (long top-q strongest, short bottom-q weakest); beta cancels => exit/drift artifact
  structurally absent; null = random baskets ~0. Non-overlapping rebalances (honest t-stats). 183 pairs, 15m, 4mo.
- Swept L∈{4,8,16,48} × H∈{4,16,48} × q∈{0.1,0.2} × {momentum,reversal}. RESULT: NO config |t|>=2 (best ±1.74).
  Weak REVERSAL lean at long horizon (L16/H48: +0.398%/reb, t+1.74, 3/4 folds, net +0.99% after cost). Short
  horizon H=4 spreads ~0.01% are KILLED by ~1%/reb turnover cost. xsmom not tradeable here.
- => triangulates F-033: xsmom "most stable" = small + stable + NOT significant.

## CONVERGENT CONCLUSION (3 independent methods agree — entry-direction has no robust edge)
1) F-033 trade-feature logistic OOF AUC 0.548. 2) per-pair direction GATE: all policies t<1. 3) xsmom GATE best t±1.74.
Caveats (R9): idealized costs, OHLCV-only features, single 4-mo window. Actionable: STOP hunting entry alpha from
price/regime/cross-section. Durable levers = SIMPLIFY (collapse gauntlet), RISK/SELECTIVITY (F-034), EXITS (unmeasured).

## PHASE 2.1 SHADOW DEPLOYED (live, log-only A/B — 2026-06-04 10:03Z)
- `brain/professor/shadow.py`: collapsed core on LIVE signals — keeps engine's proposed direction, replaces the
  ~40-gate accept/reject with ONE logged threshold on score=base+Σcomp−Σpen (regime ONCE). Separate table
  `shadow_decisions`. Flag `professor:shadow_enabled=1` (default OFF), threshold `professor:shadow_threshold` (0.55).
- HOOK: signals/engine.py after `signal_id = write_signal(signal)` (~line 2542) — guarded try/except, fail-open,
  runs AFTER write_signal, ignores `accepted` → CANNOT affect live. AST-verified. Applied via `docker restart
  trading-bot-brain-1` (signals/ + brain/ bind-mounted, NO rebuild).
- VERIFIED: 1:1 coverage (shadow rows == signals), 0 shadow errors, 0 trades disrupted, soar loop healthy.
- EARLY (tiny n=10): ALL live-rejected but shadow-take → threshold 0.55 too permissive (takes ~100% vs gauntlet ~5%).
- CALIBRATION TODO (next session, after ~few hundred rows accumulate, hours): set threshold to ~P90 of shadow_score
  so accept-rate ~10-15% (F-034 top-decile); then read `python -m brain.professor.shadow --report` once
  counterfactuals.cf_win% populates → A/B verdict: does 1 threshold match/beat the 40-gate gauntlet's expectancy?
  Calib query: `SELECT percentile_cont(0.9) WITHIN GROUP (ORDER BY shadow_score) FROM shadow_decisions;`
  Tune live with `redis-cli set professor:shadow_threshold <v>` (no redeploy). To DISABLE: `set professor:shadow_enabled 0`.

## SESSION HANDOFF
- Entry-direction lever EXHAUSTED on current instrument. NEXT FORK:
  (A) EXIT lever (audit's top-claimed value, F-034/F-025 775 dead lines) — REQUIRES a NEW instrument: the EDGE-over-null
      GATE is BLIND to exits (long/short share the exit → cancels). Need benchmark-relative absolute-return metric.
  (B) Structural collapse: wire brain/professor/core.py into live engine BESIDE the 40-gate gauntlet, shadow A/B in
      paper. Simplification + reproducibility regardless of edge. Touches live code (reversible, paper only).
