"""MeanFieldIsingModule — PHYSICS (Ising model / mean-field games: herding & crowding).

A market is a lattice of interacting spins (traders), each long (+1) or short (-1). In
the mean-field Ising model the order parameter is the magnetization m = <spin> in
[-1,+1]: m~0 is the disordered (balanced) phase, |m|~1 is the ordered (herded) phase.
Above the critical coupling the lattice spontaneously magnetizes — everyone crowds the
same side — which is precisely the unstable, reversion-prone state of a crowded trade.

We reconstruct the magnetization from observable positioning/flow proxies (each a
"coarse-grained spin field"):
  - OFI (order-flow imbalance)         -> aggressor alignment
  - funding rate                       -> perpetual-swap crowding (who pays to hold)
  - open-interest change (z-scored)    -> are new entrants piling INTO the move
  - recent price momentum sign         -> the realized direction of the herd

Direction: herding is a contrarian signal once it is EXTREME (|m|>0.6 => crowded =>
fade), a mild continuation signal when moderate (trend still building), and noise near
m~0. Susceptibility (chi) — how strongly OI is growing into the alignment — sharpens the
crowding read. Abstains when no positioning inputs are present.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_RET_LOOKBACK = 6
_HORIZON_MIN = 45
_CROWD_HI = 0.60          # |m| above this = over-crowded -> contrarian
_CROWD_LO = 0.20          # |m| below this = effectively balanced


class MeanFieldIsingModule(Module):
    name = "ising"
    evidence_family = "crowding"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        spins: list[float] = []     # each in [-1,1]
        used: dict[str, float] = {}

        # OFI: already a signed imbalance; squash to [-1,1]
        if frame.ofi is not None and np.isfinite(frame.ofi):
            s = float(np.tanh(float(frame.ofi)))
            spins.append(s); used["ofi"] = round(s, 4)

        # funding: positive funding => longs crowded (pay shorts). Typical |funding|<=0.01.
        if frame.funding is not None and np.isfinite(frame.funding):
            s = float(np.tanh(float(frame.funding) / 0.0005))
            spins.append(s); used["funding"] = round(s, 4)

        # OI change z-score: piling in. Sign carries with the prevailing move (added below
        # as a magnitude weight on momentum), but its own z also tilts conviction.
        oi_z = None
        if frame.oi_change_z is not None and np.isfinite(frame.oi_change_z):
            oi_z = float(np.clip(frame.oi_change_z / 2.0, -1.0, 1.0))

        # realized momentum sign over the lookback
        mom_sign = 0.0
        closes = frame.closes(_TF)
        if closes is not None and len(closes) > _RET_LOOKBACK and float(np.min(closes)) > 0:
            recent = float(np.log(closes[-1]) - np.log(closes[-1 - _RET_LOOKBACK]))
            mom_sign = float(np.sign(recent)) or 0.0
            if mom_sign != 0.0:
                spins.append(mom_sign * 0.6)     # momentum is a weaker spin than flow
                used["momentum"] = round(mom_sign * 0.6, 4)

        if len(spins) < 2:
            return ModuleOutput.abstain(self.name, "insufficient_positioning", self.horizon_min)

        # mean-field magnetization
        m = float(np.clip(np.mean(spins), -1.0, 1.0))
        crowd = abs(m)

        # susceptibility proxy: OI growing INTO the alignment amplifies crowding fragility
        chi = 0.0
        if oi_z is not None and mom_sign != 0.0:
            chi = float(np.clip(oi_z * np.sign(m if m != 0 else mom_sign), -1.0, 1.0))
        crowd_eff = float(np.clip(crowd + 0.25 * max(chi, 0.0), 0.0, 1.0))

        if crowd_eff >= _CROWD_HI:
            direction = -float(np.sign(m)) * crowd_eff       # crowded -> fade
            regime = "herded_crowded"
            conviction = float(np.clip(0.5 + 0.5 * (crowd_eff - _CROWD_HI) / (1 - _CROWD_HI), 0.0, 1.0))
        elif crowd_eff >= _CROWD_LO:
            direction = float(np.sign(m)) * (crowd_eff * 0.6)  # mild trend, still building
            regime = "ordering"
            conviction = float(np.clip(0.25 + 0.4 * crowd_eff, 0.0, 0.6))
        else:
            direction = 0.0
            regime = "disordered_balanced"
            conviction = 0.1

        # more aligned proxies => more trustworthy mean field
        conviction = float(np.clip(conviction * (0.6 + 0.1 * len(spins)), 0.0, 1.0))

        expl = (f"Ising: magnetization m={m:+.2f} (crowd={crowd_eff:.2f}, {regime}), "
                f"chi={chi:+.2f}, spins={used} -> "
                f"{'long' if direction > 0 else 'short' if direction < 0 else 'flat'}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"tf": _TF, "magnetization": round(m, 4),
                      "crowding": round(crowd_eff, 4),
                      "susceptibility": round(chi, 4),
                      "n_spins": len(spins), "spins": used,
                      "oi_z": (None if oi_z is None else round(oi_z, 4))},
            explanation=expl)
