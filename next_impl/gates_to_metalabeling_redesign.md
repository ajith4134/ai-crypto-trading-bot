# Gates → Meta-Labeling Redesign (production-grade decision layer)
## Date: 2026-06-08 | cont. 74 | Status: PLAN + Phase 0 building

Replace the hard binary veto **gauntlet** with a calibrated **probabilistic decision
architecture**. Approved direction (owner, 2026-06-08). Staged, shadow-first (Rule 14).

---

## Why (the gauntlet is a known anti-pattern)

The engine runs N independent hard AND-gates in sequence (low_vol, PCG, 4h cascade veto,
micro-jump veto, low_predicted_move, htf, netflow, OI/LS, sentiment block, MPP penalty,
adaptive `signal_too_weak`). Three structural failures:

1. **Compounding over-rejection.** If each of 8 quality gates passes 70% of GOOD trades,
   only 0.7^8 ≈ 6% survive. This IS the cont.74 no-trade deadlock.
2. **Uncalibrated.** "strength < 40" is an arbitrary line, not a probability — no coverage
   guarantee, no risk reasoning.
3. **Double-counting.** OFI and VPIN measure correlated order-flow; as separate gates they
   penalize the same factor twice.

ALTITUDE RULE (do not misapply the physics): Koopman / RMT / TDA / chaos belong in the
FEATURE layer (build better sub-scores). The DECISION layer (trade? size?) wants DECISION
THEORY: Bayes, conformal, Kelly, e-values. Keep the two layers separate.

---

## Target architecture — 3 layers (López de Prado meta-labeling)

- **Layer 1 — Side** (EXISTS): the directional signal (OFI-led, PCG/cascade/MPP flips).
- **Layer 2 — Calibrated P(profit)**: ONE secondary model ingests every gate feature +
  the 8 sub-scores and outputs a calibrated probability the trade nets > 0. The gates
  become INPUTS, not killers.
- **Layer 3 — Size = fractional Kelly(P(profit))**: weak signal → small size, not a veto.
  Position → 0 IS the soft veto. This single change structurally cures the deadlock.

Wrap with three rigorous tools:
| Need | Replacement | Gives |
|------|-------------|-------|
| `signal_too_weak` threshold | Conformal prediction → set {long}/{short}/{both=abstain} | distribution-free abstention; dial coverage |
| sequential AND-gauntlet | e-values: each gate → e-value, MULTIPLY | anytime-valid combined evidence (Ville) |
| 8-component weighted SUM | logarithmic opinion pool / product-of-experts | calibrated product; fixes overconfidence |

Per-gate principled map:
- 4h cascade veto → BOCPD regime-change probability (master notes Block 6)
- micro-jump veto → optimal-stopping / Hawkes intensity (entry timing)
- OFI/VPIN vetoes → transfer entropy / directed information (= Phase-2 TE plan)
- hist_acc → DONE (Kuramoto coherence, cont.74)

KEEP HARD (genuine risk constraints, must stay binary): liquidity floor, max position,
daily-loss limit, max-open-trades. Soft-gate only QUALITY judgments, never safety.

---

## Data substrate (verified 2026-06-08)

- 11,345 closed trades carry net_pnl (the LABEL). 11,194 also carry feature_vector.
- Recent 200 closed: feature_vector has 45–52 keys (avg 48) — RICH and usable.
- GAP: feature_vector does NOT contain the engine's 8 sub-scores (has_any_subscore=f) —
  the most decision-relevant inputs. → Phase 0 fixes this going forward.
- SELECTION BIAS (Rule 13): labels come from trades the OLD gauntlet allowed. Meta-labeling
  classically refines an existing primary signal, so training on taken-trades is valid for
  v1; it cannot judge trades the gauntlet killed. Document; revisit with rejected-candidate
  counterfactuals later.

---

## Staged rollout

### Phase 0 — Rich capture (BUILDING NOW, additive, zero decision change)
- engine.py decision snapshot → signals_at_entry now also stores the 8 sub-scores +
  composite + direction_conf (alongside the coherence/legacy A/B). Starts the labeled-data
  clock for the richer model. DONE in this commit.
- (later) optionally log REJECTED candidates' snapshots + a counterfactual outcome so
  Layer 2 can also learn the trades the gauntlet wrongly killed.

### Phase 1 — Meta-label model v1 (shadow)
- Train XGBoost/logistic P(profit) on the 11k labeled history (48 features + sub-scores as
  they accumulate). Calibrate (Platt/isotonic) so the output is a true probability.
- New module signals/meta_label.py: predict_p_profit(features) → [0,1]. Run in SHADOW:
  log p_profit alongside every live decision; change NOTHING. Reuse the coherence
  scoreboard pattern (signals_at_entry + a /signals/metalabel/scoreboard endpoint) to
  measure: does p_profit's IC vs realized PnL beat the current composite's? (Rule 14, ~50+
  trades.)

### Phase 2 — Conformal abstention (shadow → live gate)
- Wrap v1 with split-conformal: calibrate on a held-out window → emit {long}/{short}/
  {abstain}. Tunable coverage via Redis (metalabel:coverage, 0.9 default). Shadow first;
  then let it REPLACE the `signal_too_weak` line behind a toggle (metalabel:gate_enabled).

### Phase 3 — Kelly sizing cutover
- size = fractional_kelly(p_profit, payoff) replaces binary accept/reject. Half/Quarter
  Kelly (master notes Block 9). Shadow the sizing vs the current fixed capital_usdt; cut
  over per-pair once the scoreboard shows p_profit dominates. Hard risk caps STAY.

### Phase 4 — Retire/soft-convert the gauntlet
- Convert each remaining quality gate to an e-value feeding Layer 2 (or drop if subsumed).
- Replace the 8-weight SUM with a log-opinion-pool of calibrated sub-scores.
- BOCPD/Hawkes/TE swaps for cascade/micro/OFI gates as separate feature upgrades.

---

## Guardrails
- Every phase SHADOW-validated before touching live capital (Rule 14, 50+ cycles).
- Hard risk constraints never softened.
- Toggles for every new layer (metalabel:gate_enabled, metalabel:size_enabled,
  metalabel:coverage) — instantly reversible, mirrors the coherence toggle.
- Each new reject/abstain path emits a log + Redis counter (Rule 12).

## References
- Meta-labeling: López de Prado 2017. Conformal: Vovk/Shafer; MAPIE; PMLR v266 (2025).
- e-values/anytime-valid: Grünwald, Ramdas, Larsson (2024–2025).
- Log-opinion-pool / product-of-experts calibration (Algorithmic Bayesian Epistemology 2024).
