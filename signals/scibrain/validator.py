"""SciBrain Phase 7b — deterministic target-grounding + semantic-consistency validator (design §8).

The council's LLM can propose a change that is grammatically valid and even grounded (targets a
module that was measured) yet SEMANTICALLY WRONG — e.g. proposing to BOOST a module that voted WITH
the losing direction, which would only reinforce the mistake. This validator rejects such proposals
deterministically, from the frozen evidence, BEFORE anything is registered or evaluated. It never
calls an LLM and never touches a live parameter.

Core rule (from the attribution + path-aware twin fault):
  • A decision was BAD  (twin fault_class ∈ {direction, selection}, or the trade simply lost): the
    modules that were ALIGNED with that decision pushed us toward the error → the consistent change
    is to CUT them (or BOOST the ones that OPPOSED). Boosting an aligned driver is inconsistent.
  • A decision was GOOD (fault_class == none, or the trade won): the reverse — boosting an aligned
    driver reinforces what worked; cutting it (or boosting an opposer) is inconsistent.
So: an INCREASE is consistent iff (good_decision AND aligned) OR (bad_decision AND NOT aligned);
a DECREASE is consistent iff the opposite. When the outcome/fault is indeterminate, the validator
abstains (returns no error) — it never blocks on missing evidence, it just can't confirm.
"""
from __future__ import annotations

import structlog

from . import changespec as cs

log = structlog.get_logger()


def _decision_quality(evidence: dict):
    """True = the decision was GOOD, False = BAD, None = indeterminate (skip the semantic check)."""
    fault = evidence.get("twin_fault_class")
    won = evidence.get("won")
    if fault == "none" or won is True:
        return True
    if fault in ("direction", "selection") or won is False:
        return False
    return None


def _target_alignment(evidence: dict, module: str):
    """Whether the target module's vote was ALIGNED with this decision (from the frozen attribution),
    or None if the module isn't in the attribution (grounding handles that case separately)."""
    for a in evidence.get("attribution") or []:
        if isinstance(a, dict) and a.get("module") == module:
            return bool(a.get("aligned"))
    return None


def semantic_consistency(proposal: dict, evidence: dict) -> list[str]:
    """Return errors[] if the proposed gain change contradicts the deterministic evidence; empty list
    = consistent (or indeterminate). Assumes the proposal already passed grounding (valid DSL target
    that is a module measured in the evidence)."""
    errors: list[str] = []
    target = (proposal or {}).get("target", "")
    res = cs.resolve_target(target)
    if not res.get("valid") or res.get("kind") != "gain_multiplier":
        return errors                                  # non-gain targets: nothing to check here
    parts = target.split(".")
    module = parts[2] if len(parts) >= 4 else None

    quality = _decision_quality(evidence)
    if quality is None:
        return errors                                  # can't determine good/bad → abstain
    aligned = _target_alignment(evidence, module)
    if aligned is None:
        return errors                                  # not in attribution → grounding's job

    try:
        candidate = float(proposal.get("candidate"))
        current = float(res.get("current"))
    except (TypeError, ValueError):
        return ["candidate/current not numeric"]
    if abs(candidate - current) < 1e-9:
        return errors                                  # no-op → grounding's job
    increasing = candidate > current

    # an INCREASE is consistent iff (good_decision AND aligned) OR (bad_decision AND NOT aligned)
    increase_consistent = (quality and aligned) or ((not quality) and (not aligned))
    if increasing and not increase_consistent:
        verb = "won; boosting an opposed module would fight what worked" if quality else \
               "faulted; boosting a module ALIGNED with the bad decision reinforces the error"
        errors.append(f"semantic: increase of '{module}' is inconsistent — the decision {verb}")
    elif (not increasing) and increase_consistent:
        verb = "won; cutting a module that helped the win degrades it" if quality else \
               "faulted; cutting a module that OPPOSED the bad decision removes the corrective vote"
        errors.append(f"semantic: decrease of '{module}' is inconsistent — the decision {verb}")
    return errors
