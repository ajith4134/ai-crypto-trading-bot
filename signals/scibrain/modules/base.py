"""Base class for SciBrain modules — enforces the typed contract + safe degradation."""
from __future__ import annotations

import dataclasses

import structlog

from ..contracts import ModuleOutput, SensorFrame

log = structlog.get_logger()


class Module:
    """A PhD-concept scorer. Subclasses implement `_compute`; `evaluate` wraps it
    so a raising/NaN module degrades to an abstain instead of crashing the circuit
    (BLOCK-16 robustness; house rule: never a silent crash)."""

    name: str = "base"
    horizon_min: int = 60
    # §5a/§6g identity declared ONCE per module class (the bank constructs ModuleOutput by
    # keyword and historically left these at the contract default → all "unspecified"). `evaluate`
    # stamps them so the §6g.330 family-correlation penalty + influence manifest can group modules.
    # A module that sets evidence_family explicitly on its own output (e.g. the shadow bank) keeps it.
    role: str = "direction"
    evidence_family: str = "unspecified"

    def _compute(self, frame: SensorFrame) -> ModuleOutput:  # pragma: no cover - abstract
        raise NotImplementedError

    def _stamp_identity(self, out: ModuleOutput) -> ModuleOutput:
        """Fill role/evidence_family from the class declaration when the output still carries the
        contract default. Never overrides an output that declared its own (shadow modules do)."""
        repl = {}
        if out.role == "direction" and self.role != "direction":
            repl["role"] = self.role
        if out.evidence_family == "unspecified" and self.evidence_family != "unspecified":
            repl["evidence_family"] = self.evidence_family
        return dataclasses.replace(out, **repl) if repl else out

    def evaluate(self, frame: SensorFrame) -> ModuleOutput:
        try:
            out = self._compute(frame)
        except Exception as exc:
            log.warning("scibrain_module_error", module=self.name,
                        symbol=frame.symbol, error=str(exc)[:160])
            return self._stamp_identity(
                ModuleOutput.abstain(self.name, f"error:{str(exc)[:60]}", self.horizon_min))
        # defend the contract: clamp ranges, kill NaNs
        d = out.direction
        c = out.conviction
        if d != d or c != c:  # NaN check
            return self._stamp_identity(
                ModuleOutput.abstain(self.name, "nan_output", self.horizon_min))
        d = max(-1.0, min(1.0, float(d)))
        c = max(0.0, min(1.0, float(c)))
        if d != out.direction or c != out.conviction:
            # clamp needed — rebuild via dataclasses.replace so role/evidence_family/shadow_only
            # are PRESERVED (a hand-built ModuleOutput() here dropped them → a shadow_only module
            # whose conviction got clamped silently leaked into the live vote).
            out = dataclasses.replace(out, direction=d, conviction=c)
        return self._stamp_identity(out)
