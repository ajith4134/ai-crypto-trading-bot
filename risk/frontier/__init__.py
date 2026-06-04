"""cont. 60 — Frontier exit-decision module.

Implements 15 SOTA SL/TP techniques from the 2024-2026 research pass. Each
sub-module exposes a `evaluate_*` function that takes (trade, mark, r) and
returns an `ExitDecision` dataclass that the sl_monitor consumes.

The Redis counter pattern: every fire path INCRs a telemetry counter so the
dashboard / governance can see which signals are actually triggering.

Architecture:
  exit_signals.py     — signal-driven exits (CVD div, filtered OBI, funding/premium, Hawkes, MM-Hawkes, BOCPD)
  exit_bands.py       — band-driven exits (Conformal, Mamba quantiles, Diffusion)
  exit_placement.py   — placement modifiers (DVOL scaler, Liquidation dark-side)
  exit_kill_switch.py — portfolio-level kills (GNN contagion)
  llm_council.py      — multi-agent LLM exit council
  ml_stubs.py         — PPO / Deep Hedging integration points (need training)

Usage from risk/manager.py:
    from risk.frontier import evaluate_all_exits
    decision = evaluate_all_exits(trade, mark, r, sl_level, ratchet_sl)
    if decision.force_close:
        _guarded_close(decision.reason); continue
    if decision.tighten_sl:
        ratchet_sl = apply_tighten(ratchet_sl, decision)
"""
from .exit_signals import (
    evaluate_cvd_divergence,
    evaluate_filtered_obi,
    evaluate_funding_premium,
    evaluate_hawkes,
    evaluate_mm_hawkes_spoof,
    evaluate_bocpd_killswitch,
)
from .exit_bands import (
    evaluate_conformal_bands,
    evaluate_mamba_quantiles,
    evaluate_diffusion_band,
)
from .exit_placement import (
    apply_dvol_scaling,
    apply_liquidation_dark_side,
)
from .exit_kill_switch import (
    evaluate_gnn_contagion_kill,
    evaluate_liq_cascade_kill,
)
from .llm_council import (
    evaluate_llm_exit_council,
)
from .ml_stubs import (
    evaluate_ppo_exit_policy,
    evaluate_deep_hedging_exit,
)
from .decision import ExitDecision, evaluate_all_exits

__all__ = [
    "ExitDecision",
    "evaluate_all_exits",
    "evaluate_cvd_divergence",
    "evaluate_filtered_obi",
    "evaluate_funding_premium",
    "evaluate_hawkes",
    "evaluate_mm_hawkes_spoof",
    "evaluate_bocpd_killswitch",
    "evaluate_conformal_bands",
    "evaluate_mamba_quantiles",
    "evaluate_diffusion_band",
    "apply_dvol_scaling",
    "apply_liquidation_dark_side",
    "evaluate_gnn_contagion_kill",
    "evaluate_liq_cascade_kill",
    "evaluate_llm_exit_council",
    "evaluate_ppo_exit_policy",
    "evaluate_deep_hedging_exit",
]
