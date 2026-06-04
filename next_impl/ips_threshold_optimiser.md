# Idea 3 — IPS / SNIPS Threshold Optimiser

Topic slug: `ips_threshold_optimiser`
Created: 2026-05-30 (cont. 64 follow-up — after Idea 2 RAG shipped)
Trigger: Owner — "continue implementing Deferred". Picked Idea 3 next.

---

## A. The bleeding edge (Rule 2 verified)

After R1+R3 (cont. 55) the actuator nudges `min_signal_strength_delta`
per (regime × strength-band) bucket by `sign × base_step × density ×
unanimity² × regret_mult`. Each decoded miss contributes ±1 to ±5.

Top decoded bucket on 2026-05-30: `bull|40` has **408** decoded F9
samples; `bull|44` has 182. The accumulated `min_signal_strength_delta`
for these buckets is whatever incremental nudges have stuck.

**Problem:** the ±1 nudge is *gradient-free* — it has no notion of
"what threshold maximises future reward given the historical sample of
rejected signals". It walks in the direction the LLM voted, but never
asks "is this the *optimal* threshold for this bucket given everything
we've seen?". With 590 decoded samples in the dominant bucket, we have
enough evidence to ANSWER that question directly.

---

## B. SNIPS in one paragraph

The rejection policy is a logging policy. Each rejected signal `i` is
a sample with:

- `s_i`         = signal_strength at rejection
- `r_i`         = counterfactual reward (peak_profit % minus a haircut on peak_loss %)
- `π(s_i)`      = sigmoid((s_i − current_τ) / temperature) — propensity of accepting under current policy
- `1{s_i ≥ τ}`  = the indicator for "would the candidate threshold τ have accepted this?"

Self-Normalised IPS (Joachims & Swaminathan 2018, arXiv:1809.03084):
```
                Σ_i  (1{s_i ≥ τ} / π(s_i)) · r_i
V(τ) =  ────────────────────────────────────────
                Σ_i  1{s_i ≥ τ} / π(s_i)
```

Numerically:
- The denominator is the "effective sample size at this τ" — keeps the
  estimator stable when few samples cross.
- The optimal threshold is `τ* = argmax_τ V(τ)`.
- We don't replace the existing nudge actuator — we *blend* SNIPS into
  it: `delta_new = 0.9 × delta_old + 0.1 × (τ* − ga_base)`. Smooth
  EWMA convergence; per-decode nudges still react in real time, SNIPS
  pulls the trajectory toward the corpus-optimal each night.

---

## C. Data sources (Rule 2 verified)

For each bucket key `<regime>|<band_lo>`:

| Field | Source |
|---|---|
| `s_i` | `signals.signal_strength` |
| `regime_i` | `signals.market_regime` |
| `band_i` | `floor(s_i / 4) * 4` |
| `peak_profit_pct_i` | `counterfactuals.peak_profit_pct` |
| `peak_loss_pct_i` | `counterfactuals.peak_loss_pct` |
| `r_i` | `clip(peak_profit − 0.3·|peak_loss|, −10, +30)` |
| `current_τ` | GA `min_signal_strength` + current bucket delta |

Query (single SQL, joins signals × counterfactuals × brain_state for GA):
```sql
SELECT s.signal_strength, c.peak_profit_pct, c.peak_loss_pct
  FROM counterfactuals c
  JOIN signals s ON s.id = c.signal_id
 WHERE c.miss_decoded = TRUE
   AND s.market_regime = %s
   AND s.signal_strength >= %s
   AND s.signal_strength < %s
```

Min evidence: **n ≥ 100 decoded rows per bucket** (statistically valid
for SNIPS per arXiv:1612.01205; lower than the original 1000 the
Idea 3 deferral cited — that number was conservative).

---

## D. Algorithm

```python
def compute_optimal_delta(rows, current_tau, ga_base, temperature=3.0):
    """Returns (optimal_tau_minus_ga_base, n_evidence) or None."""
    if len(rows) < MIN_EVIDENCE:
        return None

    strengths = np.array([r["strength"] for r in rows])
    rewards   = np.clip(
        np.array([r["peak_profit"] - 0.3 * abs(r["peak_loss"])
                  for r in rows]),
        -10.0, 30.0,
    )

    # Propensities under the CURRENT policy. Sigmoid of (s − τ) / T.
    # Floor 0.05 prevents division by zero on extreme tails.
    propensities = np.maximum(
        1.0 / (1.0 + np.exp(-(strengths - current_tau) / temperature)),
        0.05,
    )

    # Grid: ±10 strength units around current τ in steps of 0.5.
    candidates = np.arange(current_tau - 10.0, current_tau + 10.5, 0.5)

    best_tau, best_v = current_tau, -float("inf")
    for tau in candidates:
        accept_mask = (strengths >= tau).astype(float)
        weights     = accept_mask / propensities
        denom       = weights.sum()
        if denom < 5.0:  # effective sample size floor
            continue
        v = (weights * rewards).sum() / denom
        if v > best_v:
            best_v, best_tau = v, tau

    return float(best_tau - ga_base), len(rows)
```

EWMA blend in the writeback:
```python
new_delta = 0.9 * old_delta + 0.1 * optimal_delta_relative_to_base
new_delta = clip(new_delta, -10.0, +10.0)  # match R1 cap
```

---

## E. Integration

**No consumer changes.** `signals/engine.py` already reads via
`metacognition.actuator.get_bucket_delta(regime, strength,
"min_signal_strength_delta")`. IPS writes to the *same* field; the
EWMA blend means the nudge writes and IPS writes coexist without
overwriting.

Per-bucket schema add:
```json
"bull|40": {
  "min_signal_strength_delta": -3.2,   // existing; now nudged AND ips-blended
  "n_evidence": 408,                   // existing; per-decode nudge count
  "last_update_ts": ...,               // existing
  "ips_optimal_delta_raw": -5.0,       // NEW; pre-EWMA, for audit
  "ips_n_evidence": 408,               // NEW; corpus size at last IPS run
  "ips_last_update_ts": ...            // NEW
}
```

**Counters (Redis):**
- `decoders:ips_buckets_updated_count` — per IPS run
- `decoders:ips_below_min_evidence_count` — buckets skipped
- `decoders:ips_no_effective_sample_count` — effective sample size below 5
- `decoders:ips_clipped_count` — proposed blend hit the ±10 cap

---

## F. Schedule + safety

- Celery beat `compute-ips-thresholds` — nightly at `04:30 UTC` (after
  the 02:30/03:00/04:00 ML retrains so it doesn't compete for CPU).
- F46 gated. Returns `{"status": "f46_inactive"}` when off.
- One-shot manual trigger:
  ```
  docker exec trading-bot-celery_worker-1 python -c \
    "from celery_app import compute_ips_thresholds as t; \
     print(t.delay().get(timeout=600))"
  ```
- Reversion: any bucket can be manually reset by editing
  `brain:filter_overrides` JSON in Redis.

---

## G. Confirmed vs Proposed

### Confirmed (already on disk)
- `metacognition/actuator.py:get_bucket_delta` — read path. No change needed. ✅
- `metacognition/actuator.py:_apply_bucketed` — per-decode write path. No change needed. ✅
- `BRAIN_FILTER_OVERRIDES` Redis key + v2 bucketed schema. ✅
- F46 governance hook. ✅
- pgvector / counterfactuals / signals tables. ✅

### Proposed (this slice)
- `metacognition/ips_optimiser.py` (new, ~150 LoC) — `compute_optimal_delta`
  + `run_full_pass()` that iterates all bucket keys.
- `celery_app.py` — new task `compute_ips_thresholds` + beat entry.
- `redis_keys.py` — no new keys (writes via existing `BRAIN_FILTER_OVERRIDES`).

### Deferred (later)
- Per-bucket temperature tuning (currently fixed 3.0). Could be learned
  from the bandwidth of the strength distribution in the bucket.
- Bandwidth-aware reward shaping (peak_profit_pct minus a regime-
  conditional haircut on peak_loss_pct).

---

## H. References

- [Counterfactual Learning from Bandit Feedback — Joachims & Swaminathan, arXiv:1809.03084](https://arxiv.org/abs/1809.03084)
- [Self-Normalized IPS (SNIPS) explainer — EmergentMind](https://www.emergentmind.com/topics/self-normalized-inverse-propensity-scoring-snips)
- [Counterfactual Risk Minimisation w/ IPS-weighted BPR — arXiv:2509.00333](https://arxiv.org/pdf/2509.00333)
- [Optimal and Adaptive Off-policy Evaluation in Contextual Bandits — arXiv:1612.01205](https://arxiv.org/pdf/1612.01205)

---

## I. Checklist

- [ ] `metacognition/ips_optimiser.py` (new).
- [ ] `celery_app.py` — `compute_ips_thresholds` task + beat at 04:30 UTC.
- [ ] PROGRESS.md cont. 64 follow-up entry.
- [ ] `next_impl/f9_f12_revolutionary_uses.md` — mark Idea 3 SHIPPED.
- [ ] Rebuild `celery_worker` + `celery_beat`.
- [ ] One-shot manual trigger to populate `ips_optimal_delta_raw` immediately.

---
