# F55 — Kronos Crypto-Native Foundation Backbone

**Status:** Tier A — DEFERRED (design only this session, cont. 55).
**Research:** arXiv:2508.02739 — Kronos foundation model pretrained on
**12 billion K-lines** across 45 exchanges. Reports **+93% RankIC** over
leading time-series foundation models on crypto evaluation.

## Why deferred

Adding Kronos requires:
1. Downloading the HF weights (NeoQuasar/Kronos-base, ~3GB).
2. New `ml/kronos.py` (~400 LoC) — encoder + classification head wrapper.
3. Per-pair fine-tune over 7d candle history (Q-LoRA, ~10 min/pair on CPU).
4. Pretrainer step 14 to backfill all active pairs.
5. Inference latency budget — Kronos needs ~50ms/pair vs current 5-20ms
   for Mamba — may push the 1m-tick budget. Profiling needed first.

Tier S features cleared this session deliver more measurable ROI per LoC.
Kronos has academic SOTA numbers but no in-bot validation yet.

## When to implement

After cont. 55 Tier S goes live and we have ≥7 days of paper-trade comparison
data. If the F50c Mamba + F50g Chronos ensemble shows IC < 0.04 on direction
prediction, Kronos becomes the next priority. Otherwise it's parallel to
incremental F8 router tuning.

## Design sketch

- `ml/kronos.py` — HF weights loader + inference wrapper, same contract
  as `ml/foundation_forecast.py`.
- Writes `{pair}:kronos_forecast` with dir1 + mag1 + quantile bands.
- Bonus weight in signals/engine.py: **±15** (highest of the forecaster
  bonuses) — justified by the +93% RankIC paper number.
- Pretrainer step 14: `huggingface_hub.snapshot_download("NeoQuasar/Kronos-base")`.
- Governance: `register("F55", "Kronos Crypto Foundation Backbone", 0)`.

## Open questions

- Q-LoRA fine-tune per pair vs zero-shot? Paper claims strong zero-shot
  but per-pair adapter should help on lower-cap alts.
- Replace F19/F20 or ensemble? Ensemble first; replace only if Kronos
  contribution dominates after 30d.

## Session handoff

Not implemented yet. Open this file when re-opening the topic.
