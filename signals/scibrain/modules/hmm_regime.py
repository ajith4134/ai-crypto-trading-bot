"""HMMRegimeModule — STATS (Hidden Markov Model market-regime integration).

Reuses the pretrained Gaussian HMM at models/hmm_regime.pkl (the same model ml/hmm.py
serves the global regime from) instead of fitting per call. The HMM's latent states are
calibrated to {bear, bull, turbulent, unknown} by sorting the state means (lowest mean =
bear, highest = bull) and tagging the highest-variance state turbulent — the identical
scheme ml/hmm.py._calibrate uses. A degenerate model (any |state mean| > 0.5, i.e. trained
on an unfiltered glitch return) is REJECTED → the module abstains, so a bad checkpoint can
never inject a phantom regime.

The model is trained on DAILY fractional returns, so we feed each pair's own daily-scale
returns (aggregated from the 1h candle buffer) — never intraday returns, which would be a
train/serve scale mismatch (the documented failure mode). `predict_proba` gives the current
posterior over regimes; the regime-weighted expected daily return is the (slow, context)
directional vote, conviction scaled by how concentrated the posterior is. Abstains on a
missing/degenerate model or thin history.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_MODEL_PATHS = (Path("models/hmm_regime.pkl"), Path("/app/models/hmm_regime.pkl"))
_DEGENERATE_MEAN = 0.5     # |state mean| > 50%/day ⇒ garbage component ⇒ reject (matches ml/hmm.py)
_OUTLIER_RET = 0.5
_BARS_PER_DAY = 24         # aggregate 1h candles -> daily
_MIN_DAILY_RETS = 6
_HORIZON_MIN = 240         # regime is a slow, multi-day context


class HMMRegimeModule(Module):
    name = "hmm_regime"
    evidence_family = "regime"  # §6g.330 family-correlation penalty
    role = "context"
    horizon_min = _HORIZON_MIN

    _model = None
    _label_map: dict | None = None
    _means: np.ndarray | None = None
    _healthy: bool | None = None
    _loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        type(self)._loaded = True
        model = None
        for p in _MODEL_PATHS:
            if p.exists():
                try:
                    with open(p, "rb") as f:
                        model = pickle.load(f)
                    break
                except Exception:
                    model = None
        if model is None:
            type(self)._healthy = False
            return
        try:
            means = model.means_.flatten()
        except Exception:
            type(self)._healthy = False
            return
        if np.any(np.abs(means) > _DEGENERATE_MEAN):
            type(self)._healthy = False        # degenerate component ⇒ reject
            return
        order = np.argsort(means)
        label_map = {int(order[0]): "bear", int(order[-1]): "bull"}
        for s in order[1:-1]:
            label_map[int(s)] = "unknown"
        try:
            label_map[int(np.argmax(model.covars_.flatten()))] = "turbulent"
        except Exception:
            pass
        type(self)._model = model
        type(self)._means = means
        type(self)._label_map = label_map
        type(self)._healthy = True

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        self._ensure_loaded()
        if not self._healthy or self._model is None:
            return ModuleOutput.abstain(self.name, "model_unavailable_or_degenerate",
                                        self.horizon_min)

        closes_1h = frame.closes("1h")
        if closes_1h is None or len(closes_1h) < _BARS_PER_DAY * (_MIN_DAILY_RETS + 1):
            return ModuleOutput.abstain(self.name, "insufficient_1h_history", self.horizon_min)
        if float(np.min(closes_1h)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        # aggregate 1h -> daily-scale closes (every 24th bar), then fractional daily returns
        daily_closes = closes_1h[::-1][::_BARS_PER_DAY][::-1]      # keep newest, 24h-spaced
        daily_rets = np.diff(daily_closes) / daily_closes[:-1]
        daily_rets = daily_rets[np.isfinite(daily_rets) & (np.abs(daily_rets) <= _OUTLIER_RET)]
        if daily_rets.size < _MIN_DAILY_RETS:
            return ModuleOutput.abstain(self.name, "insufficient_daily_returns", self.horizon_min)

        try:
            post = self._model.predict_proba(daily_rets.reshape(-1, 1))[-1]   # current posterior
        except Exception as exc:
            return ModuleOutput.abstain(self.name, f"predict_failed:{str(exc)[:40]}",
                                        self.horizon_min)

        exp_daily_ret = float(np.dot(post, self._means))          # regime-weighted drift
        dom_state = int(np.argmax(post))
        regime = self._label_map.get(dom_state, "unknown")
        posterior_conf = float(np.max(post))                      # how sure of the regime

        direction = float(np.clip(np.tanh(exp_daily_ret / 0.03), -1.0, 1.0))
        # slow context signal: keep conviction modest, gated by posterior concentration
        conviction = float(np.clip(posterior_conf * min(abs(exp_daily_ret) / 0.02, 1.0) * 0.6,
                                   0.0, 0.55))
        if regime in ("turbulent", "unknown"):
            conviction *= 0.4

        expl = (f"HMM: regime={regime} (posterior={posterior_conf:.2f}), "
                f"E[daily ret]={exp_daily_ret*100:+.2f}% over {daily_rets.size} days -> "
                f"{'long' if direction>0 else 'short' if direction<0 else 'flat'}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"regime": regime, "posterior_conf": round(posterior_conf, 4),
                      "exp_daily_ret_pct": round(exp_daily_ret * 100.0, 4),
                      "n_daily_returns": int(daily_rets.size),
                      "state_posterior": [round(float(p), 3) for p in post]},
            explanation=expl)
