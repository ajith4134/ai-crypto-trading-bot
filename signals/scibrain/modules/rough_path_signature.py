"""RoughPathSignatureModule — level-2 path signature (rough-path theory); trade event ORDER, not
just summary statistics (Hot-Pair, design §6e Tier-A).

UNIQUE EVIDENCE (§6g.2): every other module reduces the window to summary numbers (mean, vol, slope,
entropy) that are BLIND to order — "price rose then volume came" and "volume came then price rose"
have identical means/vols but opposite meaning. The path SIGNATURE (iterated integrals of a
multi-channel path) is the canonical object that DOES separate them: the level-2 antisymmetric term —
the Lévy AREA between two channels — is the signed area the path sweeps in their plane, i.e. WHICH
channel led. No other module in the bank uses iterated integrals / Lévy area / event order.

CHANNELS: the design's ideal path is [return, volume, OFI, OI], but the SensorFrame only carries
per-bar candles plus SINGLE-snapshot ofi/oi (no per-bar OFI/OI series). So we build the path from the
candle-derivable channels that ARE per-bar:
  c1 = log return                                   (price path)
  c2 = signed money flow = sign(return)·log1p(vol)  (per-bar order-flow proxy — the "OFI" stand-in)
  c3 = close-location-value in the bar range        (microstructure: did it close strong/weak)
(Per-bar OFI/OI from the live order book + an online-learned readout are documented v2 follow-ons.)

LEVEL-2 SIGNATURE (canonical, piecewise-linear path, Chen):
  Sig^{ij} = Σ_k (X^i_k−X^i_0)·ΔX^j_k + ½ Σ_k ΔX^i_k·ΔX^j_k ,  Lévy area A^{ij} = ½(Sig^{ij}−Sig^{ji}).
The flow↔return Lévy area is the lead-lag the design's "lead-lag transform" is designed to expose.

FALSIFIABLE HYPOTHESIS (order-flow-leads-price ⇒ continuation; price-leads-flow ⇒ exhaustion):
  signal = A_norm(flow,return) · netflow_norm    (continuous, bounded)
    flow leads (A>0) & net flow up   → long      |  flow leads (A>0) & net flow down → short
    price leads (A<0)                → fades the lagging move (sign flips)
  direction = tanh(SCALE·signal),  conviction ∝ |signal|·path_richness (abstain on flat/noise paths).

ADMISSION (§6g + Rule 14): shadow_only=True (observe authority) — recorded + IC-evaluable, never a
live vote until incremental IC is proven. Pure numpy (cumsums), deterministic, abstains safely.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "15m"
_MIN_BARS = 32
_WINDOW = 48            # bars of path used for the signature
_DIR_SCALE = 2.0
_CONV_SCALE = 0.5
_HORIZON_MIN = 30       # path-order / microstructure signal is short-horizon


def _standardize_increments(dx: np.ndarray) -> np.ndarray:
    sd = dx.std()
    return dx / sd if sd > 1e-12 else dx * 0.0


def _sig2(xi: np.ndarray, xj: np.ndarray) -> float:
    """Level-2 iterated integral Sig^{ij} of two cumulative paths xi, xj (Chen, piecewise-linear)."""
    dxi = np.diff(xi)
    dxj = np.diff(xj)
    run_i = xi[:-1] - xi[0]                         # X^i before each step
    return float(np.sum(run_i * dxj) + 0.5 * np.sum(dxi * dxj))


class RoughPathSignatureModule(Module):
    name = "rough_path_signature"
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        arr = frame.candles.get(_TF)
        if arr is None or len(arr) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min,
                                        role="direction", evidence_family="path_order",
                                        shadow_only=True)
        arr = arr[-_WINDOW:]
        o, h, l, c, v = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5]
        if not np.all(np.isfinite(c)) or float(np.min(c)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min,
                                        role="direction", evidence_family="path_order",
                                        shadow_only=True)

        # per-bar channel increments
        ret = np.diff(np.log(c))                                  # log returns (price)
        vol = v[1:]                                               # volume aligned to returns
        rng = (h - l)[1:]
        clv = np.where(rng > 0, (c[1:] - l[1:]) / rng, 0.5) - 0.5  # close-location-value, centered
        money_flow = np.sign(ret) * np.log1p(np.maximum(vol, 0.0))  # signed order-flow proxy

        if len(ret) < 16 or ret.std() <= 1e-12 or money_flow.std() <= 1e-12:
            return ModuleOutput.abstain(self.name, "flat_path", self.horizon_min,
                                        role="direction", evidence_family="path_order",
                                        shadow_only=True)

        # standardized increments → cumulative paths (channel scales comparable)
        d_ret = _standardize_increments(ret)
        d_flow = _standardize_increments(money_flow)
        d_clv = _standardize_increments(clv)
        x_ret = np.concatenate([[0.0], np.cumsum(d_ret)])
        x_flow = np.concatenate([[0.0], np.cumsum(d_flow)])
        x_clv = np.concatenate([[0.0], np.cumsum(d_clv)])

        # level-1: net signed increments (already standardized) → trend / accumulation
        net_ret = float(d_ret.sum())
        net_flow = float(d_flow.sum())

        # level-2 Lévy area between flow (i) and return (j): sign = which channel leads
        sig_fr = _sig2(x_flow, x_ret)
        sig_rf = _sig2(x_ret, x_flow)
        area = 0.5 * (sig_fr - sig_rf)
        # normalize area by the path "sizes" → ~[-1,1] lead-lag strength
        scale = (np.linalg.norm(d_flow) * np.linalg.norm(d_ret)) + 1e-9
        a_norm = float(np.clip(area / scale, -1.0, 1.0))
        netflow_norm = float(np.clip(net_flow / (np.sqrt(len(d_flow)) + 1e-9), -1.0, 1.0))

        # continuation when flow leads (a_norm>0): signal = a_norm·netflow_norm (signed, bounded)
        signal = a_norm * netflow_norm
        direction = float(np.tanh(_DIR_SCALE * signal))

        # richness gate: a path with little total variation carries no reliable order info
        richness = float(min(1.0, (np.abs(d_ret).sum() + np.abs(d_flow).sum()) / (2.0 * len(d_ret))))
        conviction = float(min(abs(signal) / _CONV_SCALE, 1.0) * richness)

        leads = "flow→price" if a_norm > 0 else "price→flow"
        stance = "long" if direction > 0 else "short"
        expl = (f"rough_path_sig(L2): Lévy area {a_norm:+.2f} ({leads}), net_flow {netflow_norm:+.2f} "
                f"→ {'continuation' if a_norm > 0 else 'exhaustion'} → {stance}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min,
            regime_tag=("trending" if a_norm > 0 else "mean_revert"),
            features={
                "tf": _TF,
                "levy_area_flow_return": round(a_norm, 4),
                "net_flow": round(netflow_norm, 4),
                "net_return": round(net_ret, 4),
                "leads": leads,
                "path_richness": round(richness, 4),
                "n_bars": int(len(ret)),
                "clv_drift": round(float(x_clv[-1]), 4),
            },
            explanation=expl,
            role="direction", evidence_family="path_order", shadow_only=True,
        )
