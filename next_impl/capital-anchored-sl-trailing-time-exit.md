# Capital-Anchored SL + Capital-Anchored Trailing Activation + Smart Time-Exit

Owner ask (cont. 62+, 2026-05-29 session):

> When a trade is opened the initial SL should be **50% of the total capital
> allocated to the trade**. **Trailing should start after profit reaches at
> least 11% of the capital**. And **if a trade has been open > 30 min, is
> currently in loss, and the loss is only 1-2% (or < $1-3 USDT), time-exit it;
> if the loss is much larger, wait until it decreases before exiting**.

Triggering example: SXPUSDT SHORT, $21 capital, 5× lev, SL at -$3.45
(≈ 16% of capital — way tighter than the 50% the owner wants), held 4.6 h with
a -$0.45 loss and no time-exit firing.

---

## 1. Online research (per "check online to implement this idea")

| Topic | Source synthesis |
|---|---|
| Capital-anchored SL | The 1-2% rule prevails on *account*, not on *trade margin*. For the per-trade margin, exchanges and educators (Bybit/Binance/Kucoin 2026 guides) commonly advise that the SL be set so the worst case is a fraction of the margin — 30-50% is a common bracket for higher-leverage perps; liquidation maths puts 100% margin loss at `1/lev` adverse move. At 5× a 50%-of-margin SL = a 10% notional move; at 20× it = 2.5%. ✅ Plausible. |
| Activation-price trailing | Bybit/Binance/Kraken all expose an "activation price" so the trailing stop only starts moving once a profit threshold is hit. Recommended distance is "not too close" — i.e. at least one realised noise band above entry. Owner's 11%-of-capital threshold = 2.2% notional at 5× = ~ 1× ATR on a mid-vol alt. Inside best-practice range. ✅ |
| Time-exit with conditional waiting | Lopez de Prado's Triple Barrier (already cited in code at cont. 59 for the 48 h hold cap) explicitly supports an early *time barrier* for sideways trades. Recent research (Tradewink mean-reversion guide; arXiv 2501.16772) confirms shorter timeframes (< 1 h) are in a *reversion* regime — exiting a deeply negative trade at the 30-min mark crystallises a drawdown that statistically tends to recover; exiting only when the trade has *already* mean-reverted to near-flat is the documented best practice. ✅ Aligned with owner's "if much more, wait until loss decreases". |

Source links:
- Kucoin Risk Management 2026 — https://www.kucoin.com/blog/crypto-futures-risk-management-2026
- Bybit Trailing Stop (Perpetuals) — https://www.bybit.com/en/help-center/article/Trailing-Stop-Order-Perpetual-and-Futures-Trading
- Binance Trailing Stop — https://www.binance.com/en/support/faq/what-is-a-trailing-stop-order-360042299292
- QuantifiedStrategies — 5 Exit Strategies — https://www.quantifiedstrategies.com/trading-exit-strategies/
- Lopez de Prado Triple Barrier write-up — https://medium.com/@jpolec_72972/stop-loss-take-profit-triple-barrier-time-exit-advanced-strategies-for-backtesting-8b51836ec5a2
- arXiv 2501.16772 — Trends/Reversion across timescales

Conclusion: all three ideas are research-supported. None contradict
Blueprint §10.4 ("wide initial SL, never moves backward, every trade closed by
trailing SL"). They REFINE it with capital-anchored bounds instead of pure
volatility anchoring.

---

## 2. Current code on disk (verified, not from memory)

`risk/manager.py`:

- `compute_initial_sl(pair, direction, strategy_id)` lines 88-199:
  - `atr_distance = max(legacy_atr × atr_mult, mark × min_pct)` with
    `atr_mult ∈ {1.5, 2.5, 3.5}` (HMM regime) and `min_pct = 1%`
  - VPIN fallback path (current default): `atr_distance = mark × vol_unit × atr_mult`,
    `vol_unit ∈ [0.5%, 2.5%]` → `atr_distance ∈ [0.75%, 8.75%]` of mark.
  - DCA-room floor is bypassed (`dca_rounds_max=0`).
  - CandleNet mag floor + DVOL scaling + liquidation dark-side snap layered on.
  - **No capital-anchored term anywhere.** SXPUSDT example: 5× lev, ~3.45/21 = 16.4 % of capital → matches ~3.3 % notional move which is right inside the [0.75, 8.75] vol band. Code is behaving as designed; the design doesn't know about capital.

- Trailing activation lines 791-816:
  - `activation_pct = max(0.010, 1.5 × vol_unit)` → 1.0 - 3.75% NOTIONAL.
  - Per-trade Brain override via `trade:{id}:trail_activation_pct` ∈ [0.002, 0.05].
  - Profit-lock-tier Path B (cont. 44 mandate): $2/80%, $10/85%, $25/90%, $50/92%
    — dollar tiers, not capital %.
  - Path 0 Breakeven Shield (cont. 62): at 1×vol_unit peak profit, SL snaps to
    entry × (1 ± 13 bp). Adds the *no-loss guarantee* once armed.
  - **No capital-% activation rung.**

- Time barrier lines 612-650:
  - 48 h max hold via `bot:max_hold_hours`.
  - Trail-time-decay (cont. 62): Chandelier multiplier decays after 6 h.
  - **No 30-min dead-trade exit.**

`execution/paper.py:259` / `execution/live.py:259` `modify_sl(force=False)`:
monotonic guard — initial SL can be WIDER than auto-ratchet ever sets, but
auto-ratchet calls without `force=True` can never widen back out. So a 50 %
initial SL set at open will be progressively tightened by the ratchet, not
clipped at open. ✅

`config.yaml`:
- `capital.leverage_min: 5`, `leverage_max: 20`
- `capital.per_trade_min_pct: 5`, `per_trade_max_pct: 30`
- `risk.profit_lock_tiers: [[50,0.92],[25,0.90],[10,0.85],[2,0.80]]`

---

## 3. Confirmed (owner stated explicitly)

1. **Initial SL = 50% of allocated capital.** (interpretation: capital-anchored
   floor; never tighter than 50% capital loss).
2. **Trailing activates at ≥ 11% of capital profit.**
3. **Time-exit after 30 min** in loss when loss is "small" (≤ 1-2% capital
   OR ≤ $1-3 USDT absolute).
4. **Larger loss after 30 min → WAIT** for loss to mean-revert into the small-loss
   band, then exit.
5. Research-driven implementation (per "check online").

---

## 4. Proposed (need owner confirmation before code)

### 4.1 Initial SL — capital-anchored floor

```
# In compute_initial_sl, AFTER all current layers compute raw_sl:
# Translate "50% of capital loss" into a price distance, take the WIDER of
# the two (existing vol-driven raw_sl vs capital-driven raw_sl).
capital_loss_target  = 0.50 × capital_usdt                # $ owner-mandated
required_pct_notional = capital_loss_target / (capital × leverage)
                      = 0.50 / leverage                  # algebraically reduces
capital_sl_distance   = mark × required_pct_notional
final_sl_distance     = max(current_atr_distance, capital_sl_distance)
```

At 5× lev: capital_sl_distance = 10% of mark (≫ current ~3%).
At 10× lev: 5%.
At 20× lev: 2.5% — close to current; vol-driven wins on extreme vol.

**Why `max`, not replace**: keeps CandleNet/liquidation/regime widening (they
can be wider than 50%-capital on extreme-vol pairs) and never makes the SL
*tighter* than what the owner mandated.

**Knobs (Redis runtime, no redeploy):**
- `risk:capital_sl_frac_disabled` ("1" → disable)
- `risk:capital_sl_frac` (default 0.50; clamp [0.20, 0.90])

### 4.2 Trailing activation — add capital-% rung

Currently activation is `max(0.010, 1.5×vol_unit)` (notional %). Add a parallel
trigger:

```
capital_activation_pct_notional = 0.11 / leverage   # 11% capital → notional %
effective_activation_pct = min(current_activation_pct, capital_activation_pct_notional)
```

`min` chosen so trailing arms WHICHEVER threshold is reached first — owner's
intent is "no later than 11 % of capital", not "exactly". Profit-lock Path B
($2/80%) stays — covers tiny-capital trades where 11% is less than $2.

**Why not replace Path A**: vol-anchored activation is what makes high-vol
trades wait for a real move; the 11%-capital rung is a *backstop* for cases
where vol-anchored never arms. Owner's existing cont. 44 lesson ($5-$20 peak
bucket bled $15k) is exactly this gap.

**Knobs:**
- `risk:capital_activation_frac` (default 0.11; clamp [0.03, 0.50])
- `risk:capital_activation_disabled` ("1" → disable)

### 4.3 Smart time-exit — 30 min dead-trade rule

New block in `monitor_trailing_sl` between the existing 48 h time-barrier and
the frontier eval. Logic in plain English:

```
if hold_seconds >= 30 min
   AND peak_pnl_usdt <= small_peak_threshold      # never showed real profit
   AND current_pnl < 0                            # in loss now
   AND |current_pnl_usdt| <= small_abs_loss_usdt  # 3 USDT default
   AND |current_pnl_pct_capital| <= small_pct_capital   # 2% default
   AND brain has not frozen the trade             # respects TRADE_TRAIL_FROZEN
   → close("time_exit_dead_trade_small_loss")

else if hold_seconds >= 30 min
   AND peak_pnl_usdt <= small_peak_threshold
   AND current_pnl < -small_abs_loss_usdt
   → do NOTHING this tick. Wait. (mean-revert hypothesis)
   On the next tick where the small-loss band is satisfied → close.
```

**Knobs (all Redis runtime):**
- `risk:dead_trade_min_age_s` (default 1800 = 30 min)
- `risk:dead_trade_small_abs_usdt` (default 3.0)
- `risk:dead_trade_small_pct_capital` (default 0.02)
- `risk:dead_trade_peak_threshold_usdt` (default 0.5 — "never showed real profit"; >$0.50
   peak means trade did make a move and should be governed by ratchet, not this rule)
- `risk:dead_trade_disabled` ("1" → off)

**Silent-rejection compliance**: emit
`log.info("dead_trade_time_exit_wait", ...)` on the wait-branch (so the
"why didn't it exit yet" question is answerable from logs), and
`r.incr("trail:dead_trade_exit_count")` / `trail:dead_trade_wait_count`.

**Interaction with existing safeguards**:
- Path 0 Breakeven Shield: only arms once peak ≥ 1×vol_unit profit; for a true
  dead trade peak never hits that → no conflict.
- 48 h time barrier: still fires for trades that *did* reach profit but never
  hit TP1; this new rule fires *earlier and only* for the dead-loss case.
- Brain freeze (`trade:{id}:trail_frozen`): respected — skip the exit when frozen.

### 4.4 Files touched (estimate)

- `risk/manager.py` — three localised additions (compute_initial_sl, trailing
  activation block, new dead-trade block). ~80-120 LOC.
- `config.yaml` — optional default-value entries under `risk:` for clarity
  (still runtime-overridable from Redis).
- `redis_keys.py` — new key constants for the Redis knobs.
- No DB migration; no executor change.

### 4.5 Honest Rule-4 production-grade checklist

| Check | Status |
|---|---|
| Coverage of owner's 3 asks | ✅ all three addressed |
| Consume-side: who reads the new SL/activation | ✅ same path (`monitor_trailing_sl` + `modify_sl` monotonic guard) |
| Producer-side: who writes them | ✅ `compute_initial_sl` at trade open; `monitor_trailing_sl` per-tick for activation; new block for time-exit |
| Silent-rejection rule | ✅ both branches log + incr counters |
| Brain override surface | ✅ `trail_frozen` honoured; new knobs are Redis-runtime |
| Existing safeguards | ✅ Path 0 / 48h / ratchet untouched |
| Simplifications | None — capital-anchored math is straight algebra, no shortcuts |
| RL deadlock | N/A (not an RL gate) |

---

## 5. Rule 5 — Blueprint redesign needed?

Blueprint §10.4: "wide initial SL, distance determined by the Brain based on
pair volatility". The owner mandate is **capital-anchored**, not
volatility-anchored. **Proposed compromise: ADD a capital-anchored floor as a
new lower bound layered on top of the volatility-driven base.** Both axioms
hold; volatility still drives the *shape*, capital provides the *floor*.

If the owner wants the capital floor to be the **only** input (replacing all
volatility logic), that is a bigger redesign and the blueprint needs an
amendment first. **Need confirmation.**

Time-exit is additive to §10.4 (which is silent on time); no blueprint conflict.

---

## 6. Owner decisions (CONFIRMED 2026-05-29)

- [x] **D-1**: Initial SL = `max(vol-driven, 50%-capital)`. Wider wins.
- [x] **D-2**: Trailing activation = `min(vol-anchor, 11%-capital)`. First-to-fire.
- [x] **D-3**: **Looser** thresholds — 45 min / ≤$2 abs / ≤1.5 % capital /
      ≤$0.50 peak.
- [x] **D-4**: Force-close at **6 h** regardless if loss never mean-reverts.
- [x] **D-5**: Applies to paper + live (one code path).

## 7. Implementation (in flight this session)

Files touched:
1. `risk/manager.py` — add `_apply_capital_sl_floor` helper, capital activation
   rung in trailing block, dead-trade time-exit block.
2. `signals/engine.py` — call capital-floor helper after capital+leverage finalised.
3. `risk/hedge.py` — call capital-floor helper for hedge open.
4. `redis_keys.py` — new knob constants.
5. `config.yaml` — document the runtime defaults.

Then rebuild `brain` + `data_feed` containers per the bind-mount rule.

Delete this file after Rule-2 verification of the live behaviour.

---

## 7. Session handoff

When owner answers D-1…D-5, this file gets a **Decisions** subsection and
implementation goes into `risk/manager.py` only. Rebuild the `brain` and
`data_feed` containers (per the bind-mount rule) once code lands; no migration.

Delete this file after Rule-2 verification of the implementation in the
running container.

Linked memories: [[feedback_profit_lock]], [[feedback_silent_rejection]],
[[feedback_blueprint_first]], [[feedback_blueprint_grade_check]],
[[feedback_redesign_when_blueprint_fails]].
