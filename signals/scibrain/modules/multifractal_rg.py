"""MultifractalRGModule — MATH (multifractal scaling / renormalization-group view of the price path);
emits a HORIZON-VALIDITY gate (Hot-Pair, design §6e Tier-B, role=gate+horizon). SHADOW research.

UNIQUE EVIDENCE vs the bank (§6g.2 — design demands it "prove value beyond Chaos+Wavelet+SOC"):
 • Chaos estimates ONE Hurst H (the q=2 structure-function exponent only) → a single trend/revert lens.
 • Wavelet localizes ENERGY per scale (which horizon carries the move), not the SCALING LAW.
 • StatPhysSOC measures tail/criticality, not self-similarity.
This module computes the FULL q-order structure-function spectrum S_q(τ)=⟨|X(t+τ)−X(t)|^q⟩ ~ τ^ζ(q) for
several q, hence the GENERALIZED Hurst H(q)=ζ(q)/q and the NONLINEARITY of ζ(q) — the one thing a single
H is blind to. A LINEAR ζ(q) ⇒ monofractal (one scaling law, the single-H picture is valid); a CONCAVE
ζ(q) ⇒ MULTIFRACTAL/intermittent (a whole spectrum of local exponents → a single horizon's prediction is
unreliable). It also detects a CROSSOVER scale (short- vs long-lag H differ ⇒ the scaling regime breaks
inside the window) and a ROUGH-VOLATILITY exponent (scaling of the |return| fluctuation path; H_vol≪0.5 =
rough). evidence_family='multifractal'.

ROLE = GATE+HORIZON (not direction): emits direction=0 and conviction = HORIZON_VALIDITY ∈ [floor,1] — the
existing fusion turns a direction-0/conviction>0 output into a multiplicative size damper. Wide
multifractal spectrum OR a strong scale-crossover ⇒ the prediction horizon is ambiguous ⇒ validity (and
thus size, once promoted) is damped; the recommended scaling horizon rides in features.

ADMISSION (§6g + Rule 14): shadow_only=True — RECORDED + evaluable, NEVER applied to a live pick; per the
design it stays shadow / gets PRUNED unless it proves incremental IC beyond Chaos+Wavelet+SOC. Pure numpy
(structure functions + log-log slopes), deterministic, abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "1h"            # only TF with enough history for a scaling range (≤30m carry ≤65 bars)
_MIN_BARS = 120
_WINDOW = 200
_QS = (1.0, 2.0, 3.0, 4.0)
_HORIZON_MIN = 60
_VALID_FLOOR = 0.2    # an ambiguous-scaling name is damped, not vetoed


def _structure_exponents(X: np.ndarray, lags: np.ndarray):
    """ζ(q) for each q in _QS = log-log slope of S_q(τ)=⟨|ΔX_τ|^q⟩ vs τ. Returns (zeta dict-array, logS)."""
    loglag = np.log(lags.astype(float))
    zeta = np.zeros(len(_QS))
    h2_by_lag = None
    logS = np.zeros((len(_QS), len(lags)))
    for li, tau in enumerate(lags):
        dincr = np.abs(X[tau:] - X[:-tau])
        for qi, q in enumerate(_QS):
            logS[qi, li] = np.log(np.mean(dincr ** q) + 1e-300)
    for qi in range(len(_QS)):
        zeta[qi] = float(np.polyfit(loglag, logS[qi], 1)[0])
    return zeta, logS, loglag


class MultifractalRGModule(Module):
    name = "multifractal_rg"
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        arr = frame.candles.get(_TF)
        if arr is None or len(arr) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min,
                                        role="gate", evidence_family="multifractal", shadow_only=True)
        c = arr[-_WINDOW:, 4]
        if not np.all(np.isfinite(c)) or float(np.min(c)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min,
                                        role="gate", evidence_family="multifractal", shadow_only=True)
        X = np.log(c)
        r = np.diff(X)
        if r.size < _MIN_BARS - 1 or r.std() <= 1e-12:
            return ModuleOutput.abstain(self.name, "flat_returns", self.horizon_min,
                                        role="gate", evidence_family="multifractal", shadow_only=True)

        n = X.size
        max_lag = max(8, n // 5)
        lags = np.unique(np.geomspace(1, max_lag, 12).astype(int))
        lags = lags[lags >= 1]
        if lags.size < 5:
            return ModuleOutput.abstain(self.name, "scaling_range_too_short", self.horizon_min,
                                        role="gate", evidence_family="multifractal", shadow_only=True)

        zeta, logS, loglag = _structure_exponents(X, lags)
        hq = zeta / np.array(_QS)                         # generalized Hurst H(q)=ζ(q)/q
        h2 = float(hq[1])                                 # standard Hurst (q=2)
        # multifractal WIDTH: spread of H(q). Monofractal ⇒ ~0; multifractal/intermittent ⇒ large.
        mf_width = float(hq.max() - hq.min())

        # CROSSOVER: H(2) on the short half of the lag range vs the long half — a break = regime change.
        mid = lags.size // 2
        try:
            h2_short = float(np.polyfit(loglag[:mid + 1], logS[1, :mid + 1], 1)[0] / 2.0)
            h2_long = float(np.polyfit(loglag[mid:], logS[1, mid:], 1)[0] / 2.0)
        except Exception:
            h2_short = h2_long = h2
        crossover = abs(h2_short - h2_long)

        # ROUGH-VOLATILITY exponent: scaling of the |return| fluctuation path (cumsum of demeaned |r|).
        av = np.abs(r)
        V = np.cumsum(av - av.mean())
        vlags = lags[lags < V.size]
        if vlags.size >= 4:
            vlog = np.array([np.log(np.mean(np.abs(V[t:] - V[:-t]) ** 2) + 1e-300) for t in vlags])
            h_vol = float(np.polyfit(np.log(vlags.astype(float)), vlog, 1)[0] / 2.0)
        else:
            h_vol = float("nan")

        # HORIZON-VALIDITY gate: wide multifractal spectrum OR strong crossover ⇒ a single-horizon
        # prediction is unreliable ⇒ damp. (Normalizers chosen so a clearly multifractal/cross-over
        # name lands well below 1 but is not vetoed.)
        validity = float(np.clip(
            1.0 - np.clip(mf_width / 0.6, 0, 1) * 0.6 - np.clip(crossover / 0.3, 0, 1) * 0.4,
            _VALID_FLOOR, 1.0))
        scaling = ("multifractal" if mf_width > 0.25 else "monofractal")
        # recommended horizon: if there's a crossover, the valid horizon is the SHORT-scale regime
        rec_horizon = "short_scale" if crossover > 0.15 else "full_window"
        persistence = "persistent/trending" if h2 > 0.55 else ("antipersistent/revert" if h2 < 0.45 else "random")

        return ModuleOutput(
            module=self.name,
            direction=0.0,                                # gate — no directional opinion
            conviction=validity,                          # [floor,1] horizon-validity size damper
            expected_move_pct=None,
            horizon_min=self.horizon_min,
            regime_tag=f"scaling_{scaling}",
            features={
                "tf": _TF,
                "hurst_q2": round(h2, 4),
                "multifractal_width": round(mf_width, 4),        # H(q) spread: 0 mono, >0 multi
                "generalized_hurst": [round(float(x), 4) for x in hq],
                "crossover_strength": round(float(crossover), 4),
                "h2_short_scale": round(h2_short, 4),
                "h2_long_scale": round(h2_long, 4),
                "rough_vol_hurst": (None if not np.isfinite(h_vol) else round(h_vol, 4)),
                "horizon_validity": round(validity, 4),
                "recommended_horizon": rec_horizon,
                "n_lags": int(lags.size),
            },
            explanation=(f"multifractal_rg: {scaling} (H(q) width {mf_width:.2f}), H₂={h2:.2f} "
                         f"({persistence}), crossover {crossover:.2f} → horizon_validity {validity:.2f}"),
            ok=True,
            role="gate",
            evidence_family="multifractal",
            shadow_only=True,
        )
