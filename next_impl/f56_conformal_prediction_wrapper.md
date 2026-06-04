# F56 — Conformal Prediction Uncertainty Wrapper

**Status:** SHIPPED cont. 56 (2026-05-28).

## Implementation summary
- `ml/conformal_wrapper.py` (~230 LoC). Split conformal predictor (Romano
  et al. 2019; Angelopoulos & Bates 2023). Reuses F49
  `model:{name}:prediction_log` as calibration source — no new logging.
- `update_intervals(alpha=0.10)` — nightly Celery beat. Computes per-model
  nonconformity-score 90 % quantile = `conformal:width:{model}`.
- `kelly_multiplier(active_models)` — geometric mean of (1 − width) across
  warmed-up forecasters. Clamped [0.25, 1.0]. Wired into
  `risk/manager.py:compute_kelly_capital` (`f_frac_scaled = f_frac *
  conformal_mult`). Cold-start safe (returns 1.0 when no model warmed up).
- `should_abstain(direction_conf)` — True when `|conf/100 − 0.5| < max_width`
  across warmed models. Wired into `signals/engine.py:accept_or_reject`
  as a soft abstain at the very end of the gate chain.
- Beat task: `conformal-interval-refresh` (daily 04:15 UTC) in `celery_app.py`.

Cold-start guarantee: until any model accumulates ≥ 30 (pred, outcome)
pairs, kelly_multiplier=1.0 and should_abstain=False. Zero regression.

## Session handoff
- Health — `redis-cli get conformal:health`
- Per-model width — `redis-cli keys 'conformal:width:*'`
- Governance — `redis-cli get feature:F56:contribution`

File cleared after Rule-2 verification post-implementation.
