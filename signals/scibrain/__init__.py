"""SciBrain — the Scientist-Brain Launchpad core (2026-06-08).

A modular circuit/CPU of PhD-level math + physics + quantum modules that scores
trading candidates with full transparency and Ollama self-interrogation. Built
to the BLOCK-16 architecture in memory/discussion_master_notes.md and the design
in next_impl/scientist_brain_launchpad.md.

Pipeline (the "CPU"):
    SensorBus  ─ live features → SensorFrame (one per symbol)
      → Module bank (each PhD concept → a typed ModuleOutput)   [the transistors]
      → Meta-Router (which modules fire per regime)              [logic gates]   (Phase 3)
      → Fusion ALU (combine → Decision: dir/conviction/size)     [the ALU]
      → Ollama Interrogator (bull/bear "why this direction?")    (Phase 1 L4)
      → SciBrain opener → engine executor                        (behind scibrain:enabled)

The `scibrain:enabled` authority switch controls whether SciBrain originates trades.
Every decision records applied, gated, suppressed, abstained, advisory, and
counterfactual influences so non-causal evidence is never presented as a cause.
No module ever raises into the pipeline — a failed module returns a low-conviction
neutral ModuleOutput so the circuit degrades gracefully (BLOCK-16 robustness).

VS-1 ships: contracts + SensorBus + Koopman (physics) + BOCPD modules + Fusion ALU.
"""
from __future__ import annotations

from .contracts import Decision, ModuleOutput, SensorFrame

__all__ = ["SensorFrame", "ModuleOutput", "Decision"]
