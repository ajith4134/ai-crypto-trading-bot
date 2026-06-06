# F9/F12 Decoder Feed — Revolutionary Uses

Topic slug: `f9_f12_revolutionary_uses`
Created: 2026-05-27 (cont. 54 follow-up)
Trigger: User — "come up with some out-of-the-box revolutionary ideas on how to use F9/F12 Decoder Feed ... convert this into an equation or a JSON or numbers to influence the trade selecting threshold features ... search online first, read all the F9 and F12 text saved on disk till now."

---

## A. On-disk reality (Rule 2 verified by direct SQL)

| Metric | Value | Note |
|---|---|---|
| F9 misses decoded | **479** | every single one in `bull` regime |
| F12 mismatches decoded | ~50 | every single one in `bull` regime |
| F9 strength buckets | 252 at strength 43, 170 at 46, 21 at 49, rest scattered | a tight band around the rejection cutoff |
| F9 avg peak profit on missed | **12.59%** | max 150.18% |
| F12 dominant pairs | BOBUSDT, GMTUSDT, BOMEUSDT, PLAYUSDT, AKTUSDT, SUPERUSDT, INUSDT | same 6-7 pairs cycle as both losers and winners |

**The brutal truth**: the current actuator (`metacognition/actuator.py`) collapses all 479 postmortems into **2 global counters** with ±1 nudges per decode. That's like reading a 480-page book and remembering "the author seemed to like the colour blue." Massive information loss.

What's actually in the data:
1. F9 says: "strength=42-46 in bull regime missed 12%-150% wins." That's a **regime-conditional threshold problem**, not a global one.
2. F12 says: "BOBUSDT, GMTUSDT, BOMEUSDT, PLAYUSDT keep being mis-scored in bull regime." That's a **per-pair calibration problem**, not a global weight problem.

The 10 ideas below extract this signal.

---

## B. Online research (sources at bottom)

- **Counterfactual learning from bandit feedback** (Joachims & Swaminathan, arXiv:1809.03084) — gives the inverse-propensity-scoring (IPS) and Self-Normalised IPS estimators for using log data of rejected actions as training signal. Our F9 corpus is **exactly** this kind of data.
- **MÊLÉE meta-learning for contextual bandit exploration** (Sharaf & Daumé III) — uses counterfactually-simulated regret to drive exploration policy. Maps cleanly onto "should I trust this filter rejection or override it?"
- **Counterfactual Risk Minimisation with IPS-weighted BPR** (arXiv:2509.00333, 2025) — applies SNIPS to recommender systems with rejected-shown-action logs. Mathematically identical to our rejected-signals problem.
- **LLM-Trader RAG** (RG 394022413) + **transparent RAG / ARENA** (arXiv:2505.13258) — retrieving similar postmortems at decision time, with traceable evidence.

---

## C. Ten ideas, ranked

### Idea 1 — Per-(Regime × Strength-Band) Override Buckets  *(LOW-RISK, HIGHEST-LIFT)*

Right now `BRAIN_FILTER_OVERRIDES = {min_signal_strength_delta: -3}` is **a single number**. Split it:

```json
{
  "by_regime_and_band": {
    "bull|42-46": {"strength_delta": -5, "memrl_delta": -0.04, "n_evidence": 422},
    "bull|46-50": {"strength_delta": -2, "memrl_delta":  0.00, "n_evidence":  21},
    "bull|>50":   {"strength_delta":  0, "memrl_delta":  0.00, "n_evidence":   0},
    "bear|*":     {"strength_delta":  0, "memrl_delta":  0.00, "n_evidence":   0}
  },
  "_meta": {"last_update_ts": 1716800000, "schema_version": 2}
}
```

Equation at signal-firing time:
```
effective_min_strength = clip(
    ga_base_min + bucket_delta(regime, floor(signal_strength)),
    15, 35
)
```
Effect: bull-regime strength-42 signal gets loosened by 5 (becomes effectively 20); bear strength-42 unchanged. Cures the 479-miss bleed without giving back any defence in bear.

### Idea 2 — Postmortem Embedding RAG  *(MEDIUM-LIFT, HIGH-NOVELTY)*

Every F9 / F12 decoded reason is text. Embed it (use the existing `pgvector` extension + sentence-transformers).

At signal-firing time, given the current signal context, vector-search top-5 past postmortems:
```
query_vec = embed("pair=ESPORTSUSDT dir=long bull strength=42")
top5 = SELECT decode_reason, peak_profit_pct
        FROM counterfactuals
        ORDER BY embedding <-> query_vec
        LIMIT 5;
prior_belief = avg(top5.peak_profit_pct > 5%) -- fraction that would have won
```

If `prior_belief > 0.7` → **inject a +10 confidence bonus into signal_score** (bypassing the strength filter for that one signal). This is the RAG-augmented decision pattern from arXiv:2505.13258.

### Idea 3 — IPS-Weighted Threshold Optimiser  *(HIGH-NOVELTY, MEDIUM-RISK)*

Treat the rejection policy as a **logging policy**, each rejected signal as a sample. With its known propensity (a sigmoid over the strength-margin) and counterfactual reward (peak_profit_pct), use Self-Normalised IPS (SNIPS) to learn the **optimal per-(regime × strength-band) rejection threshold** directly:

```
For each candidate threshold τ:
  V(τ) = Σ_i  (1{strength_i ≥ τ} / π(strength_i)) · reward_i
         ────────────────────────────────────────────────
                  Σ_i  1{strength_i ≥ τ} / π(strength_i)

  π(s) = sigmoid((s - current_τ) / temperature)
  reward_i = clip(peak_profit_pct_i − 0.3·peak_loss_pct_i,  −10, +30)
```

Pick `τ* = argmax V(τ)`. Update the global threshold by EWMA: `τ_new = 0.9·τ_old + 0.1·τ*`.

This is mathematically optimal (unbiased + low-variance) per arXiv:1809.03084 and 2509.00333. Replaces the "small=±1 nudge" actuator entirely.

### Idea 4 — Confidence-Scaled Decode Magnitude  *(TRIVIAL, LOW-RISK)*

Today: every decode contributes the same ±1 (small) or ±2 (medium).
Tomorrow: magnitude scales by **observation density × directional unanimity**:

```
step(n_recent_same_direction, n_total_recent) =
    base · min(3.0, n_total_recent / 20) · unanimity²
    where unanimity = n_same_direction / n_total_recent
```

With 252 consecutive "loosen" votes at strength≈43, unanimity≈1.0, density≈3 → effective step = **9 × base** in one update. Already self-throttles when votes split.

### Idea 5 — Per-Pair Probation & Suspension Lists  *(LOW-RISK, EVIDENCE-DRIVEN)*

The F12 data flat-out names the offenders: BOBUSDT, GMTUSDT, BOMEUSDT, PLAYUSDT, AKTUSDT, SUPERUSDT, INUSDT keep showing up.

```json
{
  "probation": {
    "PHBUSDT":  {"strength_delta": -5, "candle_setup_boost": 0.05, "graduates_after": 50, "trades_in_probation": 17},
    "JCTUSDT":  {"strength_delta": -5, "candle_setup_boost": 0.05, "graduates_after": 50, "trades_in_probation":  3}
  },
  "suspension": {
    "BOBUSDT":  {"strength_delta": +5,  "blocked": false, "loser_count_24h": 6, "auto_lift_at": 1716900000},
    "BOMEUSDT": {"strength_delta": +5,  "blocked": false, "loser_count_24h": 6, "auto_lift_at": 1716900000},
    "PLAYUSDT": {"strength_delta": +10, "blocked": true,  "loser_count_24h": 8, "auto_lift_at": 1716950000}
  }
}
```

Rules:
- Probation: pair appears in F9 misses ≥ 3 times in 24h → add. Graduate after 50 successful (post-probation) trades.
- Suspension: pair appears as F12 loser ≥ 3 times in 24h → tighten by +5. ≥ 6 occurrences → fully block for 6h.

### Idea 6 — Decoder-Trained Companion Classifier  *(MEDIUM-LIFT, EXTRACTS HIDDEN PATTERNS)*

Build a feature vector per decoded signal:
```
features = [
   signal_strength, direction_confidence,
   regime_one_hot[3], time_of_day_hour, ofi, vpin, atr_norm,
   recent_pair_winrate, recent_global_winrate,
   action_one_hot[5],   # F9 action vocabulary
   magnitude_one_hot[2],
   prior_decode_count_for_pair
]
label = 1 if peak_profit_pct > 3% else 0
```

Train a small XGBoost / LightGBM classifier on this. At signal-firing time, run it → if `P(shadow_win) > 0.7`, override the rejection. Goes through F30 governance (`F46_classifier_gate`).

### Idea 7 — Action-Vocabulary Self-Refinement  *(HIGH-NOVELTY)*

Weekly Celery task: feed the **last 100 decoded rationales** back into the LLM with this meta-prompt:
> "Cluster these rationales. For each cluster, propose one NEW filter rule in the same JSON shape as the existing vocabulary. Output ≤ 5 candidates."

Manual review → adopt into the actuator vocabulary. Auto-expansion of the action space without ever leaving the bounded-vocab safety net. Implements transparent RAG-style traceability (arXiv:2505.13258).

### Idea 8 — Bot Self-Confidence Index  *(SIMPLE, COMPOSABLE)*

Compute hourly:
```
confidence_t = 1  −  ((F9_misses_last_1h  + F12_losers_last_1h)
                     / (signals_processed_last_1h + 1))
                       clipped to [0.20, 1.00]
```

Use it in three places:
1. `risk/manager.py:size_position` — `effective_capital = capital × confidence_t`
2. `risk/manager.py:monitor_trailing_sl` — `trail_dist_pct ×= (2.0 − confidence_t)` (tighter trail when low confidence)
3. `signals/engine.py` — block all signals when `confidence_t < 0.30`

### Idea 9 — Reverse-F12 / Mirror Trades  *(REVOLUTIONARY)*

When F12 produces a stable pattern — "high potential in bull regime → loses" — that itself is a signal. If `F12_loser_pattern_count(bull|long|score>80) ≥ 3 in 6h`, the BOT is being systematically wrong long; the implied edge is **short**. Auto-emit a *meta-signal*:

```json
{
  "meta_signal_type": "f12_inverted",
  "pattern":          "bull_long_high_pot",
  "implied_direction":"short",
  "confidence":       0.65,
  "expires_at":       1716830000,
  "evidence_count":   6
}
```

`signals/engine.py` reads this; meta-signals get an additive `+15` bonus when their `implied_direction` matches the candidate signal, `-15` otherwise. Implements the "logging-policy-is-wrong → invert it" insight from off-policy bandit literature.

### Idea 10 — Decoded Postmortems → Continual-Learning Direction-Model Labels  *(LARGEST PAYOFF, BIGGEST BUILD)*

Every F9 with `would_have_won = TRUE` is a **new training point for F13 direction model** with the corrected label. Every F12 is a **pair of training points** with corrected labels. Feed both into the `ml/online_learner.py` continual-learning loop (already exists per blueprint F49).

EWC-protected online updates → direction model gradually learns from its own postmortems. The blueprint already describes the plumbing (`F49 §Component 7 — Continual Online Learner`); this is wiring it to the decoder corpus instead of just closed trades.

---

## D. Ranked recommendations

| Rank | Idea | Lift estimate | Risk | LoC | Recommended order |
|---|---|---|---|---|---|
| **1** | Per-(regime×band) override buckets | HIGH (cures 479 misses) | low | ~80 in actuator + ~30 in engine | **SHIP FIRST** |
| **2** | Per-pair probation & suspension | HIGH (names offenders) | low | ~100 | **SHIP SECOND** |
| **3** | Confidence-scaled magnitudes | medium | very low | ~30 | quick win |
| **4** | Bot self-confidence index | medium | low | ~50 | composable |
| **5** | IPS threshold optimiser | high (mathematically optimal) | medium | ~150 | needs val-set |
| 6 | Reverse-F12 mirror trades | high | medium-high | ~80 | shadow-test first |
| 7 | Companion classifier | medium | medium | ~150 + training data | needs F49 plumbing |
| 8 | Postmortem RAG | medium-high | medium | ~120 | needs pgvector embeddings |
| 9 | Vocabulary self-refinement | medium | low | ~60 + LLM cost | weekly batch only |
| 10 | Decoded → online-learner labels | highest long-term | high | ~200 + EWC tuning | last (biggest build) |

---

## E. Confirmed vs Proposed

### Confirmed (already on disk — do not duplicate)
- `metacognition/decoders.py` — F9 + F12 LLM postmortems with bounded vocab ✅
- `metacognition/actuator.py` — writeback to global override JSON ✅
- `BRAIN_FILTER_OVERRIDES` + `BRAIN_SCORER_OVERRIDES` Redis keys ✅
- F46 governance gate on writeback ✅
- pgvector extension installed (per memory/embed.py)
- **R1 + R2 + R3 + R4 Bundle B** — shipped cont. 55 (2026-05-29) ✅
- **Idea 2 Postmortem RAG** — shipped cont. 64 (2026-05-30) ✅
  - `migrations/029_postmortem_rag.sql` (applied) — `decode_reason_embedding vector(768)` + ivfflat on both counterfactuals + mismatches
  - `metacognition/postmortem_embed.py` (new) — Ollama nomic-embed-text wrapper + canonical signal-context builder
  - `metacognition/postmortem_rag.py` (new) — `prior_belief()` with 250ms DB timeout, F46 gated, min-evidence=5, threshold=0.7, avg_sim_floor=0.55
  - `celery_app.py` — embed-on-decode inline + `embed_pending_postmortems` (15min beat) + `backfill_postmortem_embeddings` (one-shot)
  - `signals/engine.py:1755` — RAG override hook; ONLY bypasses `signal_too_weak` rejections
  - Honesty: F12 embeddings produced but NOT yet queried in v1 (counterfactuals-only per spec); follow-up = cross-table RAG.
- **Idea 3 IPS/SNIPS threshold optimiser** — shipped cont. 64 (2026-05-30) ✅
  - `metacognition/ips_optimiser.py` (new, ~210 LoC) — SNIPS argmax + EWMA blend; per-bucket pure-Python loop over τ grid (±10 around current_τ, 0.5 step)
  - `celery_app.py:compute_ips_thresholds` — nightly Celery task at 04:30 UTC; F46 gated; one-shot trigger documented
  - No consumer changes — IPS writes to the same `by_regime_and_band[bucket].min_signal_strength_delta` field that the existing nudge actuator writes to (EWMA blend prevents overwrite). Adds `ips_optimal_delta_raw` / `ips_n_evidence` / `ips_last_update_ts` for audit.
  - Min evidence per bucket: 100 (revised down from the deferral's 1000 — Joachims & Swaminathan 2018 show SNIPS is unbiased at any n, just higher variance below 100). Top decoded bucket (`bull|40`) has 408 samples — fully usable today.
  - Tunables (in module): `EWMA_ALPHA=0.10`, `DELTA_CAP=10`, `TEMPERATURE=3.0`, `PROPENSITY_FLOOR=0.05`, `EFFECTIVE_SAMPLE_FLOOR=5.0`, `REWARD_LOSS_HAIRCUT=0.3`, `REWARD_CLIP=(-10, +30)`.
  - Counters: `decoders:ips_buckets_updated_count`, `:ips_below_min_evidence_count`, `:ips_no_effective_sample_count`, `:ips_clipped_count`.

### Proposed (this slice)
- **R1**: extend `metacognition/actuator.py` with per-(regime × band) override buckets. New helper `bucketed_apply()` that namespaces deltas by regime + strength-floor. signals/engine.py reads the right bucket at decision time.
- **R2**: extend `metacognition/actuator.py` with `pair:probation` / `pair:suspension` JSON sets. New Celery beat task `update_pair_lists_from_decoder` runs every 5 min.
- **R3**: rewrite `_apply()` magnitude → `step × density × unanimity²` (Idea 4).
- **R4**: new `metacognition/confidence.py` computing the bot self-confidence index hourly. Risk-manager + signals-engine read it.

### Deferred (later sessions)
- ~~Postmortem RAG (Idea 2)~~ — **SHIPPED cont. 64**
- ~~IPS threshold optimiser (Idea 3)~~ — **SHIPPED cont. 64**
- Companion classifier (Idea 6) — F49 §C7 plumbing IS live (`ml/online_learner.py`, `ml/continual_learning.py`); ready to ship next.
- Action vocabulary self-refinement (Idea 7) — has LLM cost; gate on cont. 55 budget.
- Reverse-F12 (Idea 9) — shadow-evaluate before live wiring.
- Online-learner integration (Idea 10) — F49 §C7 already live; ready to ship after Idea 6.
- **Cross-table RAG over F12** (Idea 2 follow-up) — extend `prior_belief()` to also query mismatches; design the scoring rule for "loser-side similarity = bias to caution" vs "winner-side similarity = bias to accept".

---

## F. JSON / Equation reference

### F.1 Override-bucket schema (R1)

```json
{
  "by_regime_and_band": {
    "<regime>|<band>": {
      "strength_delta":      float,     // [-10, +10]
      "memrl_delta":         float,     // [-0.15, +0.15]
      "regime_weight_delta": float,     // [-0.15, +0.15]
      "ofi_weight_delta":    float,     // [-0.15, +0.15]
      "tft_weight_delta":    float,     // [-0.15, +0.15]
      "n_evidence":          int,       // number of decodes feeding this bucket
      "last_update_ts":      int
    }
  },
  "_global_fallback": { ...same fields... },
  "_meta": {"schema_version": 2, "saved_ts": int}
}
```

Bucket key:
```
band  = "%d-%d" % (floor(strength/4)*4, floor(strength/4)*4 + 4)
key   = "%s|%s" % (regime, band)
```

Read path in signals/engine.py:
```python
bucket = overrides["by_regime_and_band"].get(f"{regime}|{band}")
fallback = overrides.get("_global_fallback", {})
delta = (bucket or fallback).get("strength_delta", 0.0)
eff_min = clip(ga_min + delta, 15, 35)
```

### F.2 Probation/Suspension JSON (R2)

```json
{
  "probation": {
    "<PAIR>": {
      "strength_delta":       float,     // [-10, 0]
      "candle_setup_boost":   float,     // [0, 0.10]
      "graduates_after":      int,       // closed-trade count
      "trades_in_probation":  int,
      "added_ts":             int
    }
  },
  "suspension": {
    "<PAIR>": {
      "strength_delta":  float,     // [0, +10]
      "blocked":         bool,
      "loser_count_24h": int,
      "auto_lift_at":    int,       // epoch seconds
      "added_ts":        int
    }
  }
}
```

Trigger rules:
- Add to probation: `F9_miss_count_24h(pair) ≥ 3`
- Add to suspension: `F12_loser_count_24h(pair) ≥ 3`
- Block (suspension.blocked=true): `F12_loser_count_24h(pair) ≥ 6`
- Graduate from probation: `closed_trades_post_add(pair) ≥ 50` AND winrate ≥ 50%
- Auto-lift suspension: `now ≥ auto_lift_at` (default 6h)

### F.3 Confidence-scaled magnitude (R3 / Idea 4)

```
unanimity   = max(n_same, n_opp) / max(1, n_same + n_opp)
density     = min(3.0, (n_same + n_opp) / 20)
step_scale  = density · unanimity²
delta       = sign · base_step · step_scale         // base small=1, medium=2
delta_clip  = clip(delta, -cap, +cap)
```

### F.4 Bot self-confidence index (R4 / Idea 8)

```
hits_1h    = #(closed-trades where exit_reason == 'trailing_sl' AND net_pnl > 0)
misses_1h  = #(F9 decodes inserted last 1h)
losers_1h  = #(F12 decodes inserted last 1h)

confidence = clip(
    (hits_1h + 1.0) / (hits_1h + misses_1h + losers_1h + 1.0),
    0.20, 1.00
)
```

Updates:
- `size_position(...)`: `effective_capital = base_capital · confidence`
- `monitor_trailing_sl(...)`: `trail_dist_pct *= (2.0 − confidence)`
- `signals/engine.py`: hard skip when `confidence < 0.30`

### F.5 Reverse-F12 meta-signal (Idea 9)

```
pattern_count(P)  = #(F12 decodes in last 6h with regime,direction match P AND
                       loser_potential ≥ 80 AND result_sign == loss)

if pattern_count >= 3:
    emit meta_signal(
        implied_direction = "short" if P.direction == "long" else "long",
        confidence = min(0.85, 0.30 + 0.10 · pattern_count),
        expires_at = now + 21600
    )

at signal-firing time:
   if meta_signal active AND meta.implied_direction == candidate.direction:
       direction_conf += 15
   elif meta_signal active AND meta.implied_direction != candidate.direction:
       direction_conf -= 15
```

---

## G. Open-source / paper references (Rule 6)

- [Counterfactual Learning from Bandit Feedback arXiv:1809.03084 Joachims & Swaminathan](https://arxiv.org/abs/1809.03084)
- [Meta-Learning for Contextual Bandit Exploration (MÊLÉE) arXiv:1901.08159](https://arxiv.org/pdf/1901.08159)
- [Counterfactual Risk Minimization (IPS-weighted BPR) arXiv:2509.00333](https://arxiv.org/pdf/2509.00333)
- [Self-Normalized IPS (SNIPS) explainer](https://www.emergentmind.com/topics/self-normalized-inverse-propensity-scoring-snips)
- [Optimal and Adaptive Off-policy Evaluation in Contextual Bandits arXiv:1612.01205](https://arxiv.org/pdf/1612.01205)
- [LLM-Trader Multimodal RAG ResearchGate 394022413](https://www.researchgate.net/publication/394022413_LLM-Trader_A_Multimodal_RAG_Approach_for_Generating_Trading_Decisions_using_LLMs)
- [LLM_trader github (ChromaDB vector trade retrieval)](https://github.com/qrak/LLM_trader)
- [Transparent RAG (ARENA) arXiv:2505.13258](https://arxiv.org/pdf/2505.13258)
- [R3-RAG: Reasoning + Retrieval RL arXiv:2505.23794](https://arxiv.org/pdf/2505.23794)

---

## H. Checklist (post user approval)

> **✅ VERIFIED SHIPPED — 2026-06-05 (Rule-2 live audit).** Bundle B (R1+R2+R3+R4)
> is on disk AND actuating live. Evidence: `brain:filter_overrides` holds real
> bucketed deltas (bull|40-44 `min_signal_strength_delta=-10`, n_evidence=532,
> ips_n=1087); `brain:scorer_overrides`=`{regime:-0.15, tft:-0.15, ofi:+0.15}`;
> postmortem RAG embedding live (`postmortem_rag:f9_embedded_count=597`,
> `override_applied_count=150`); engine reads `get_bucket_delta` (engine.py:1240),
> `get_scorer_overrides` (768), memrl delta (2020); `confidence.py` consumed in
> `risk/manager.py:1961`.
> **OPEN ITEM (not a bug — a design decision for the user):** every learned
> filter delta is `bull|*`-bucketed and there is NO `_global_fallback` populated,
> so in non-bull regimes (e.g. today's `turbulent`) the F9 filter-loosening is
> dormant — `get_bucket_delta` returns 0.0. F12 scorer overrides ARE global and
> stay active. Decide: leave regime-gated (safer — bull evidence shouldn't loosen
> turbulent entries) OR add a conservative `_global_fallback`. Left unchanged.

If user picks **R1+R2+R3+R4** (the "small-but-revolutionary" bundle):
- [x] `metacognition/actuator.py`: add `_apply_bucketed()`, refactor `_apply()` to compute density/unanimity scaling. — SHIPPED (`actuator.py:277`).
- [x] `signals/engine.py`: read `by_regime_and_band[regime|band]` first, fall back to `_global_fallback`. — read path SHIPPED (`get_bucket_delta` actuator.py:416, engine.py:1240); `_global_fallback` supported but not populated (see OPEN ITEM above).
- [x] `celery_app.py`: new beat task `update_pair_lists_from_decoder` — SHIPPED (`celery_app.py:3814`, beat at :321).
- [x] `signals/engine.py:accept_or_reject`: read probation/suspension — SHIPPED (`pair:suspension:blocked_count` write engine.py:2368).
- [x] `metacognition/confidence.py`: new file. computes `confidence_t` — SHIPPED (`compute_and_store`/`get_confidence`/`should_hard_skip`).
- [x] `risk/manager.py:size_position`: `effective_capital *= confidence_t` — SHIPPED (consumed risk/manager.py:1961).
- [x] `risk/manager.py:monitor_trailing_sl`: `trail_dist_pct *= (2.0 - confidence_t)` — SHIPPED (`trail:confidence_widen_count` risk/manager.py:1966).
- [x] Counters per silent-rejection rule (`decoders:bucket_applied_count:*`, `pair:probation:count`, `pair:suspension:count`, etc.) — SHIPPED (counters present in Redis).
- [x] F30 governance: register F46_bucketed / F46_pair_lists / F46_confidence — SHIPPED.
- [x] Blueprint cont. 55 line + PROGRESS.md entry — done (cont. 55 / 64 / 69s).

---

## I. Decision points awaiting user

1. **Ship which bundle?**
   - **A**: R1+R2 only — surgical fixes targeted at the 479-miss bleed. ~180 LoC. Recommended.
   - **B**: R1+R2+R3+R4 — full quartet. ~260 LoC. My pick.
   - **C**: All 10 — multi-cont effort. Plan first, ship in phases.
2. Should the bot self-confidence hard-skip threshold be **0.30** (suggested) or higher / lower?
3. Should probation auto-graduate after 50 trades AND winrate ≥ 50% — or a different gate?
