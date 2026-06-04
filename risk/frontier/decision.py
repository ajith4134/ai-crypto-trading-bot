"""cont. 60 — Shared ExitDecision dataclass and orchestrator.

ExitDecision is a small immutable record returned by every frontier feature.
The orchestrator `evaluate_all_exits` runs all features in sequence and
folds their decisions into a final exit / SL-modifier verdict.

Folding rules:
  * force_close wins immediately (any feature can kill — first to fire owns
    the close reason)
  * tighten_sl_mult: minimum across all features (tightest wins)
  * loosen_sl_mult:  maximum across all features (loosest wins)
  * sl_floor / sl_ceiling: applied to the final ratchet_sl
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import structlog

log = structlog.get_logger()


@dataclass
class ExitDecision:
    # Hard exit. First feature to set this wins.
    force_close: bool = False
    reason: str = ""
    # Multiplicative modifier on the Chandelier/trail distance.
    #   <1.0 tightens, >1.0 loosens, 1.0 = no change.
    tighten_sl_mult: float = 1.0
    loosen_sl_mult:  float = 1.0
    # Absolute SL floor (for long: minimum SL, can't be lower) / ceiling
    # (for short: maximum SL, can't be higher). 0 means unset.
    sl_floor: float = 0.0
    sl_ceiling: float = 0.0
    # Metadata for telemetry / logs.
    features_fired: list[str] = field(default_factory=list)
    notes: dict = field(default_factory=dict)


def fold(a: ExitDecision, b: ExitDecision) -> ExitDecision:
    """Combine two ExitDecisions following the folding rules above."""
    if a.force_close:
        return a
    if b.force_close:
        return b
    out = ExitDecision()
    out.tighten_sl_mult = min(a.tighten_sl_mult, b.tighten_sl_mult)
    out.loosen_sl_mult  = max(a.loosen_sl_mult,  b.loosen_sl_mult)
    out.sl_floor   = max(a.sl_floor,   b.sl_floor)
    out.sl_ceiling = max(a.sl_ceiling, b.sl_ceiling) if (a.sl_ceiling and b.sl_ceiling) else (a.sl_ceiling or b.sl_ceiling)
    out.features_fired = list(set(a.features_fired) | set(b.features_fired))
    out.notes = {**a.notes, **b.notes}
    return out


def evaluate_all_exits(trade: dict, mark: float, r, sl_level: float,
                       direction: str, ratchet_sl: Optional[float] = None) -> ExitDecision:
    """Run every frontier exit feature and fold their decisions.

    Failures in any single feature degrade silently — features are advisory
    contributions, not gates. Each feature wraps its own logic in try/except.
    """
    # Late imports keep cold-start cost low and avoid circular deps.
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
    from .exit_kill_switch import evaluate_gnn_contagion_kill, evaluate_liq_cascade_kill
    from .llm_council import evaluate_llm_exit_council
    from .ml_stubs import (
        evaluate_ppo_exit_policy,
        evaluate_deep_hedging_exit,
    )

    final = ExitDecision()
    evaluators = [
        ("cvd_divergence",       evaluate_cvd_divergence),
        ("filtered_obi",         evaluate_filtered_obi),
        ("funding_premium",      evaluate_funding_premium),
        ("hawkes",               evaluate_hawkes),
        ("mm_hawkes_spoof",      evaluate_mm_hawkes_spoof),
        ("bocpd_killswitch",     evaluate_bocpd_killswitch),
        ("conformal_bands",      evaluate_conformal_bands),
        ("mamba_quantiles",      evaluate_mamba_quantiles),
        ("diffusion_band",       evaluate_diffusion_band),
        ("gnn_contagion_kill",   evaluate_gnn_contagion_kill),
        ("liq_cascade_kill",     evaluate_liq_cascade_kill),
        ("llm_exit_council",     evaluate_llm_exit_council),
        ("ppo_exit_policy",      evaluate_ppo_exit_policy),
        ("deep_hedging_exit",    evaluate_deep_hedging_exit),
    ]
    for name, fn in evaluators:
        try:
            d = fn(trade, mark, r, sl_level, direction)
            if d is None:
                continue
            if d.force_close:
                d.features_fired = [name]
                return d  # hard exit short-circuits the rest
            if d.tighten_sl_mult < 1.0 or d.loosen_sl_mult > 1.0 \
               or d.sl_floor or d.sl_ceiling:
                d.features_fired = [name]
                final = fold(final, d)
        except Exception as exc:
            log.debug("frontier_feature_failed",
                      feature=name,
                      trade_id=str(trade.get("id")),
                      error=str(exc)[:120])
    return final
