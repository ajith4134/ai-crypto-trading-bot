"""Base class for SciBrain Universe-Core modules — typed contract + safe degradation.

Unlike a per-symbol `Module` (consumes a `SensorFrame`, returns ONE `ModuleOutput`), a
`UniverseModule` consumes the shared read-only `UniverseFrame` and returns a dict
{symbol -> ModuleOutput} (one per-symbol vote per symbol in the frame). `evaluate` wraps
`_compute` so a raising/NaN module degrades to an EMPTY dict (no votes) instead of crashing
the cycle (BLOCK-16 robustness; house rule: never a silent crash).
"""
from __future__ import annotations

import structlog

from ..contracts import ModuleOutput, UniverseFrame

log = structlog.get_logger()


class UniverseModule:
    """A cross-market PhD-concept scorer over the whole `UniverseFrame`."""

    name: str = "universe_base"
    horizon_min: int = 60

    def _compute(self, frame: UniverseFrame) -> dict[str, ModuleOutput]:  # pragma: no cover
        raise NotImplementedError

    def evaluate(self, frame: UniverseFrame) -> dict[str, ModuleOutput]:
        try:
            out = self._compute(frame)
        except Exception as exc:
            log.warning("scibrain_universe_module_error", module=self.name,
                        error=str(exc)[:160])
            return {}
        if not isinstance(out, dict):
            return {}
        # defend the contract per emitted vote: drop NaNs, clamp ranges (mirrors Module.evaluate)
        clean: dict[str, ModuleOutput] = {}
        for sym, mo in out.items():
            try:
                d, c = float(mo.direction), float(mo.conviction)
            except (TypeError, ValueError):
                continue
            if d != d or c != c:          # NaN
                continue
            d = max(-1.0, min(1.0, d))
            c = max(0.0, min(1.0, c))
            if d == mo.direction and c == mo.conviction:
                clean[sym] = mo
            else:
                clean[sym] = ModuleOutput(
                    module=mo.module, direction=d, conviction=c,
                    expected_move_pct=mo.expected_move_pct, horizon_min=mo.horizon_min,
                    regime_tag=mo.regime_tag, features=mo.features,
                    explanation=mo.explanation, reliability_ic=mo.reliability_ic, ok=mo.ok,
                    role=mo.role, evidence_family=mo.evidence_family,
                    shadow_only=mo.shadow_only,
                )
        return clean
