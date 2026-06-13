"""InfoTheoryModule — MATH/STATS (information theory: entropy + transfer entropy).

Three information-theoretic measures of the price process (pure numpy):

  1. Permutation entropy (Bandt-Pompe): the Shannon entropy of the ordinal-pattern
     distribution of the series. It measures COMPLEXITY/predictability without any model
     assumptions — low PE = regular/forecastable, high PE ~ 1 = random. This is the gate:
     we only take a directional view when the series is predictable.

  2. Shannon entropy of the return-sign distribution: how balanced up vs down moves are
     (a one-sided recent distribution carries directional information).

  3. Transfer entropy volume -> return: TE quantifies the DIRECTED information flow from the
     volume process to the future return beyond the return's own past — i.e. "does volume
     lead price here?". High TE corroborates that the current volume-confirmed move will
     continue. (Cross-PAIR transfer entropy vs a market leader belongs in Phase 5 where the
     full-universe panel is resident in RAM; here we use the within-pair volume->price flow
     which needs only the frame's own candles.)

Direction = predictability-weighted recent momentum, corroborated by volume->price flow.
Conviction rises as permutation entropy falls (more predictable). Abstains on thin data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 50
_MAX_BARS = 200
_PE_ORDER = 3            # ordinal-pattern embedding dimension (3! = 6 patterns)
_RET_LOOKBACK = 6
_HORIZON_MIN = 60


class InfoTheoryModule(Module):
    name = "info_theory"
    evidence_family = "information"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        arr = frame.candles.get(_TF)
        if arr is None or len(arr) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        arr = arr[-_MAX_BARS:]
        closes = arr[:, 4].astype(float)
        vols = arr[:, 5].astype(float)
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        logp = np.log(closes)
        rets = np.diff(logp)
        if float(np.std(rets)) <= 0:
            return ModuleOutput.abstain(self.name, "degenerate_returns", self.horizon_min)

        pe = _permutation_entropy(logp, order=_PE_ORDER)        # [0,1], high=random
        predictability = float(np.clip(1.0 - pe, 0.0, 1.0))

        # Shannon entropy of the return-sign distribution (recent window)
        signs = np.sign(rets[-30:])
        up = float(np.mean(signs > 0))
        sign_entropy = _binary_entropy(up)                      # [0,1], 1=balanced
        directional_imbalance = float(np.clip((1.0 - sign_entropy), 0.0, 1.0)) * np.sign(up - 0.5)

        # Transfer entropy: volume -> return (does volume lead price?)
        te_vol_price = _transfer_entropy(vols[1:], rets)        # aligned: vol change vs ret

        # recent momentum sign
        recent = float(logp[-1] - logp[-1 - _RET_LOOKBACK])
        mom_sign = float(np.sign(recent)) or (float(np.sign(directional_imbalance)) or 1.0)

        # direction: follow recent momentum, scaled by predictability; corroborate with the
        # sign-imbalance; volume->price flow adds confidence (not direction).
        base = mom_sign * predictability
        # tilt by directional imbalance when it agrees with momentum
        if np.sign(directional_imbalance) == mom_sign:
            base *= (1.0 + 0.3 * abs(directional_imbalance))
        direction = float(np.clip(base, -1.0, 1.0))
        conviction = float(np.clip(predictability * (0.55 + 0.45 * min(te_vol_price * 2.0, 1.0)),
                                   0.0, 1.0))

        regime = ("predictable" if predictability >= 0.5 else
                  "random_walk" if predictability < 0.25 else "mixed")
        if regime == "random_walk":
            conviction *= 0.4

        expl = (f"InfoTheory: perm-entropy={pe:.3f} (predictability={predictability:.2f}), "
                f"sign-entropy={sign_entropy:.2f} (up-frac={up:.2f}), "
                f"TE(vol->price)={te_vol_price:.3f}; recent "
                f"{('+' if recent>=0 else '')}{np.expm1(recent)*100:.2f}% -> "
                f"{'long' if direction>0 else 'short' if direction<0 else 'flat'}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"tf": _TF, "permutation_entropy": round(pe, 4),
                      "predictability": round(predictability, 4),
                      "sign_entropy": round(sign_entropy, 4),
                      "up_fraction": round(up, 4),
                      "te_volume_price": round(float(te_vol_price), 4),
                      "recent_ret_pct": round(float(np.expm1(recent) * 100.0), 4)},
            explanation=expl)


def _permutation_entropy(x: np.ndarray, order: int = 3, delay: int = 1) -> float:
    """Bandt-Pompe permutation entropy, normalized to [0,1]. Counts the frequency of each
    ordinal pattern (the argsort permutation) over embedded windows."""
    n = x.size
    if n < order * delay + 1:
        return 1.0
    # build embedded vectors and map each to its ordinal-pattern index
    patterns = {}
    count = 0
    for i in range(n - (order - 1) * delay):
        window = x[i:i + order * delay:delay]
        # ordinal pattern = tuple of the argsort (ranks)
        key = tuple(np.argsort(window, kind="mergesort"))
        patterns[key] = patterns.get(key, 0) + 1
        count += 1
    if count == 0:
        return 1.0
    p = np.asarray(list(patterns.values()), dtype=float) / count
    h = -float(np.sum(p * np.log(p)))
    hmax = np.log(_factorial(order))
    return float(np.clip(h / hmax, 0.0, 1.0)) if hmax > 0 else 1.0


def _binary_entropy(p: float) -> float:
    """Shannon entropy of a Bernoulli(p), in [0,1] (base-2 normalized)."""
    p = float(min(max(p, 1e-12), 1 - 1e-12))
    h = -(p * np.log2(p) + (1 - p) * np.log2(1 - p))
    return float(np.clip(h, 0.0, 1.0))


def _transfer_entropy(source: np.ndarray, target: np.ndarray, bins: int = 2) -> float:
    """Plug-in transfer entropy TE(source -> target), lag 1, binary/low-bin discretization.

    TE = sum p(y1,y0,x0) log [ p(y1|y0,x0) / p(y1|y0) ]  (nats), normalized to ~[0,1].
    Discretizes each series into `bins` quantile bins. With short samples we keep bins small
    (2) for stable probability estimates."""
    n = min(source.size, target.size)
    if n < 30:
        return 0.0
    x = _quantize(source[-n:], bins)
    y = _quantize(target[-n:], bins)
    y1, y0, x0 = y[1:], y[:-1], x[:-1]
    m = y1.size
    if m < 20:
        return 0.0
    # joint and marginal counts
    from collections import Counter
    c_y1y0x0 = Counter(zip(y1.tolist(), y0.tolist(), x0.tolist()))
    c_y0x0 = Counter(zip(y0.tolist(), x0.tolist()))
    c_y1y0 = Counter(zip(y1.tolist(), y0.tolist()))
    c_y0 = Counter(y0.tolist())
    te = 0.0
    for (a, b, c), n_abc in c_y1y0x0.items():
        p_abc = n_abc / m
        p_a_given_bc = n_abc / c_y0x0[(b, c)]
        p_a_given_b = c_y1y0[(a, b)] / c_y0[b]
        if p_a_given_bc > 0 and p_a_given_b > 0:
            te += p_abc * np.log(p_a_given_bc / p_a_given_b)
    # normalize by log(bins) so it lands ~[0,1]
    return float(np.clip(te / np.log(bins), 0.0, 1.0)) if bins > 1 else 0.0


def _quantize(x: np.ndarray, bins: int) -> np.ndarray:
    """Map values into `bins` equal-frequency bins (quantile edges)."""
    x = np.asarray(x, dtype=float)
    if bins <= 2:
        return (x > np.median(x)).astype(int)
    qs = np.quantile(x, np.linspace(0, 1, bins + 1)[1:-1])
    return np.digitize(x, qs)


def _factorial(k: int) -> int:
    out = 1
    for i in range(2, k + 1):
        out *= i
    return out
