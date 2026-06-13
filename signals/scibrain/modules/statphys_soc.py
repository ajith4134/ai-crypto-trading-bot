"""StatPhysSOCModule — PHYSICS (statistical mechanics / Self-Organized Criticality).

Markets self-organize toward criticality (Bak-Tang-Wiesenfeld sandpile): near a
critical point the return distribution is heavy-tailed, fluctuations correlate over
long ranges, and small perturbations trigger avalanches (crashes). This module
measures how close a symbol is to that critical state and emits an early-warning.

Criticality score [0,1] fuses four measured signatures (pure numpy):
  1. Hill tail-index alpha of |returns| — small alpha = heavy power-law tail = fragile.
  2. Critical Slowing Down (the canonical early-warning from dynamical-systems theory):
     a RISING rolling variance AND a RISING rolling lag-1 autocorrelation = the system
     is losing resilience and approaching a tipping point.
  3. Vol-of-vol — volatility clustering / avalanche onset (std of rolling vol / mean).
  4. Downside skew — negative skew = crash asymmetry building.

Direction: SOC is a RISK detector. We map it to a directional vote by FADING the
overextended recent move, gated on criticality and downside asymmetry: an extended
up-move into a heavy-tailed, slowing-down, negatively-skewed state is a crash warning
(short); a capitulation down-move into a critical state is a mean-reversion bounce
(long). Low criticality -> low conviction. Pure numpy; abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 60        # sensor bus serves ~65 5m bars; 60 leaves enough returns for Hill+CSD
_MAX_BARS = 240
_RET_LOOKBACK = 6
_HORIZON_MIN = 60


class StatPhysSOCModule(Module):
    name = "statphys_soc"
    evidence_family = "criticality"  # §6g.330 family-correlation penalty
    role = "risk"
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-_MAX_BARS:]
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        logp = np.log(closes)
        rets = np.diff(logp)
        if len(rets) < _MIN_BARS - 1 or float(np.std(rets)) <= 0:
            return ModuleOutput.abstain(self.name, "degenerate_returns", self.horizon_min)

        # 1) Hill tail index on |returns| (heavy tail => small alpha)
        alpha = _hill_alpha(np.abs(rets))
        # normal-ish tails alpha>=3.5; very heavy alpha<=2 => heaviness ~ [0,1]
        tail_heavy = float(np.clip((3.5 - alpha) / 2.0, 0.0, 1.0)) if alpha is not None else 0.0

        # 2) Critical slowing down: rising rolling variance AND rolling lag-1 autocorr
        var_trend = _rolling_trend(rets, _rolling_var)
        ac_trend = _rolling_trend(rets, _rolling_ac1)
        # both rising => approaching tipping point; map each to [0,1] then combine
        csd = float(np.clip(0.5 * max(var_trend, 0.0) + 0.5 * max(ac_trend, 0.0), 0.0, 1.0))

        # 3) Vol-of-vol (volatility clustering / avalanche onset)
        rv = _rolling_apply(rets, _rolling_std, win=10)
        if rv.size and float(np.mean(rv)) > 0:
            volvol = float(np.clip(np.std(rv) / (np.mean(rv) + 1e-12), 0.0, 1.0))
        else:
            volvol = 0.0

        # 4) Downside skewness of returns (negative => crash asymmetry)
        skew = _skew(rets)
        downside = float(np.clip(-skew / 2.0, 0.0, 1.0))   # only negative skew counts

        # criticality: weighted blend of the four signatures
        criticality = float(np.clip(
            0.34 * tail_heavy + 0.34 * csd + 0.18 * volvol + 0.14 * downside,
            0.0, 1.0))

        regime = "critical_unstable" if criticality >= 0.6 else (
            "near_critical" if criticality >= 0.4 else "subcritical_stable")

        # Direction: fade the overextended recent move, gated by criticality.
        recent = float(logp[-1] - logp[-1 - _RET_LOOKBACK])
        recent_sign = float(np.sign(recent)) or 1.0
        # up-move into a critical, negatively-skewed state = stronger crash short.
        asym = 1.0 + (downside if recent_sign > 0 else 0.0)   # amplify shorts on neg skew
        direction = float(np.clip(-recent_sign * criticality * asym, -1.0, 1.0))
        conviction = float(np.clip(criticality * (0.55 + 0.45 * min(abs(recent) * 80.0, 1.0)),
                                   0.0, 1.0))
        if regime == "subcritical_stable":
            conviction *= 0.35

        # crash_warning: the headline risk number for the dashboard radar. High only when
        # the signature is genuinely crash-shaped (critical + extended-up + downside skew).
        crash_warning = float(np.clip(
            criticality * (0.5 + 0.5 * (downside if recent_sign > 0 else 0.0)), 0.0, 1.0))

        alpha_s = f"{alpha:.2f}" if alpha is not None else "n/a"   # alpha is None when the
        # tail is too short/flat to estimate (e.g. PAXG-style near-zero returns); formatting
        # None with :.2f crashed the module → forced abstain → lost the SOC vote. Guarded.
        expl = (f"SOC: criticality={criticality:.2f} ({regime}); Hill alpha="
                f"{alpha_s} (heavy={tail_heavy:.2f}), CSD={csd:.2f}, volvol={volvol:.2f}, "
                f"skew={skew:+.2f}; recent {('+' if recent >= 0 else '')}"
                f"{np.expm1(recent)*100:.2f}% -> "
                f"{'long' if direction > 0 else 'short' if direction < 0 else 'flat'}; "
                f"crash_warning={crash_warning:.2f}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"tf": _TF,
                      "criticality": round(criticality, 4),
                      "crash_warning": round(crash_warning, 4),
                      "hill_alpha": (None if alpha is None else round(alpha, 4)),
                      "tail_heaviness": round(tail_heavy, 4),
                      "csd": round(csd, 4),
                      "var_trend": round(var_trend, 4),
                      "ac_trend": round(ac_trend, 4),
                      "vol_of_vol": round(volvol, 4),
                      "skew": round(skew, 4),
                      "downside_asym": round(downside, 4),
                      "recent_ret_pct": round(float(np.expm1(recent) * 100.0), 4)},
            explanation=expl)


def _hill_alpha(x: np.ndarray) -> float | None:
    """Hill estimator of the power-law tail exponent of the largest order statistics.
    alpha = 1 / mean(log(x_(i)/x_(k))) over the top-k. Smaller alpha => heavier tail."""
    x = x[np.isfinite(x) & (x > 0)]
    n = x.size
    if n < 30:
        return None
    k = max(10, int(0.1 * n))      # top 10% (>=10) order statistics
    k = min(k, n - 1)
    xs = np.sort(x)[::-1]
    top = xs[:k]
    thresh = xs[k]
    if thresh <= 0:
        return None
    logs = np.log(top / thresh)
    m = float(np.mean(logs))
    if m <= 1e-12:
        return None
    return float(1.0 / m)


def _skew(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 8:
        return 0.0
    mu = float(np.mean(x))
    sd = float(np.std(x))
    if sd <= 0:
        return 0.0
    return float(np.mean(((x - mu) / sd) ** 3))


def _rolling_apply(x: np.ndarray, fn, win: int) -> np.ndarray:
    n = x.size
    if n < win + 1:
        return np.asarray([])
    return np.asarray([fn(x[i:i + win]) for i in range(0, n - win + 1)])


def _rolling_var(w: np.ndarray) -> float:
    return float(np.var(w))


def _rolling_std(w: np.ndarray) -> float:
    return float(np.std(w))


def _rolling_ac1(w: np.ndarray) -> float:
    w = w - w.mean()
    d = float(np.dot(w, w))
    if d <= 0:
        return 0.0
    return float(np.dot(w[:-1], w[1:]) / d)


def _rolling_trend(x: np.ndarray, stat_fn, win: int = 12) -> float:
    """Sign-and-strength of the time trend of a rolling statistic, in [-1,1].
    +1 => the statistic is steadily rising (the critical-slowing-down signature)."""
    series = _rolling_apply(x, stat_fn, win)
    if series.size < 6:
        return 0.0
    t = np.arange(series.size, dtype=float)
    t -= t.mean()
    s = series - series.mean()
    denom = float(np.sqrt(np.dot(t, t) * np.dot(s, s)))
    if denom <= 0:
        return 0.0
    return float(np.clip(np.dot(t, s) / denom, -1.0, 1.0))   # Pearson r as trend strength
