"""
Section Y: Multi-Agent Debate Council — Y-01 to Y-07.

Blueprint Feature 37 (NeurIPS 2024 FinCon / arXiv:2412.20138 TradingAgents /
BlackRock AlphaAgents arXiv:2508.11152):
  Round 1 — each agent states its position independently.
  Round 2 — each agent responds to the other agents' arguments.
  Round 3 — (conditional) moderator asks one clarifying question on the key
            remaining disagreement.
  Verbal Reinforcement — after every closed trade, was_correct per agent is
            computed and the per-agent weight prior (`debate:agent_weight:*`)
            is updated via constant-α running average. Future debates
            consult those weights when synthesising verdicts.

Activation: stage >= 3 AND paper_closed >= 300 (gated upstream in signals/engine.py).

Cost guard: rounds 2/3 only fire when round 1 had all three agents return
successfully AND there is meaningful disagreement. If the ollama/cloud LLM
circuit is open or any agent failed in round 1, the council falls back to the
existing 'debate_no_llm_full_allocation' path — preserving the previous
behaviour exactly. Worst case = 9 LLM calls per signal (3 agents × 3 rounds);
typical case = 3 (round 1) or 6 (rounds 1+2 when there's disagreement).
"""
import json
import structlog
from llm.guard import assert_no_reflection
import redis_keys
import redis_client
from db import db_conn

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────────
# Agent weights — read from Redis, default to 1.0 each. Updated by
# update_beliefs_on_close after every trade close.
# ─────────────────────────────────────────────────────────────────────────────

_AGENT_WEIGHT_ALPHA = 0.1  # constant-α running average of was_correct ∈ {0,1}
_DEFAULT_WEIGHT = 1.0

def _weight_key(role: str) -> str:
    return f"debate:agent_weight:{role}"


def get_agent_weights() -> dict[str, float]:
    """Return current per-agent weights {bull, bear, risk}. Default 1.0 each."""
    try:
        r = redis_client.get()
        out = {}
        for role in ("bull", "bear", "risk"):
            raw = r.get(_weight_key(role))
            try:
                out[role] = float(raw) if raw is not None else _DEFAULT_WEIGHT
            except (TypeError, ValueError):
                out[role] = _DEFAULT_WEIGHT
        return out
    except Exception:
        return {"bull": _DEFAULT_WEIGHT, "bear": _DEFAULT_WEIGHT,
                "risk": _DEFAULT_WEIGHT}


def _set_agent_weight(role: str, value: float) -> None:
    """Persist weight; clamp to a sane range so a single noisy streak can't
    drive the weight to extremes that would unbalance future verdicts."""
    try:
        r = redis_client.get()
        v = max(0.2, min(2.0, float(value)))
        r.set(_weight_key(role), str(round(v, 4)))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Round 1 prompts (existing, unchanged signature so callers don't break).
# ─────────────────────────────────────────────────────────────────────────────

def _bull_prompt(signal: dict, market: dict) -> str:
    prompt = (
        f"You are the Bull Agent in a trading debate council. "
        f"Pair: {signal.get('pair')} | Direction: LONG | "
        f"Regime: {market.get('regime')} | Signal strength: {signal.get('signal_strength')}. "
        f"Argue FOR entering this trade. List: signal confluence, momentum evidence, "
        f"historical win conditions that match now, profit potential. "
        "Respond as JSON: {argue_for: bool, arguments: list[str], confidence: int}"
    )
    assert_no_reflection(prompt)
    return prompt


def _bear_prompt(signal: dict, market: dict) -> str:
    prompt = (
        f"You are the Bear Agent in a trading debate council. "
        f"Pair: {signal.get('pair')} | Direction: LONG | "
        f"Regime: {market.get('regime')} | Turbulence: {market.get('turbulence')}. "
        "Argue AGAINST entering this trade. List: risk factors, conflicting signals, "
        "recent similar failures, market uncertainty. "
        "Respond as JSON: {argue_against: bool, arguments: list[str], risk_score: int}"
    )
    assert_no_reflection(prompt)
    return prompt


def _risk_prompt(signal: dict, capital_pct: float, balance: float) -> str:
    prompt = (
        f"You are the Risk Agent in a trading debate council. "
        f"Pair: {signal.get('pair')} | Capital: {capital_pct}% of ${balance:.0f} | "
        f"Leverage: {signal.get('leverage', 5)}x. "
        "Evaluate: position size reasonableness, SL placement (is it too tight or too wide?), "
        "account margin impact at given leverage. "
        "Respond as JSON: {risk_acceptable: bool, concerns: list[str], recommended_size_pct: int}"
    )
    assert_no_reflection(prompt)
    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# Round 2 prompts — each agent sees peers' round-1 arguments and addresses them.
# Per blueprint: "Each agent responds to the others' specific arguments".
# ─────────────────────────────────────────────────────────────────────────────

def _trim_args(agent_out: dict, limit: int = 5) -> list[str]:
    """Take up to N arguments from an agent's output, stringify defensively."""
    raw = agent_out.get("arguments") or agent_out.get("concerns") or []
    if not isinstance(raw, list):
        return []
    return [str(a)[:200] for a in raw[:limit]]


def _bull_prompt_r2(signal: dict, market: dict,
                    bear_r1: dict, risk_r1: dict) -> str:
    bear_args = _trim_args(bear_r1)
    risk_args = _trim_args(risk_r1)
    prompt = (
        f"You are the Bull Agent. Round 2 of the trading debate. "
        f"Pair: {signal.get('pair')} | Direction: LONG. "
        f"Bear Agent argued AGAINST with: {bear_args}. "
        f"Risk Agent's concerns: {risk_args}. "
        "Address their specific points. For each point you can rebut, explain why. "
        "For any point you concede, say so honestly — do not strawman. "
        "Update your overall stance and confidence accordingly. "
        "Respond as JSON: {argue_for: bool, rebuttals: list[str], conceded: list[str], confidence: int}"
    )
    assert_no_reflection(prompt)
    return prompt


def _bear_prompt_r2(signal: dict, market: dict,
                    bull_r1: dict, risk_r1: dict) -> str:
    bull_args = _trim_args(bull_r1)
    risk_args = _trim_args(risk_r1)
    prompt = (
        f"You are the Bear Agent. Round 2 of the trading debate. "
        f"Pair: {signal.get('pair')} | Direction: LONG. "
        f"Bull Agent argued FOR with: {bull_args}. "
        f"Risk Agent's concerns: {risk_args}. "
        "Address their specific points. Counter the bullish arguments where you can, "
        "concede any point where the Bull is correct. "
        "Update your overall stance and risk_score accordingly. "
        "Respond as JSON: {argue_against: bool, rebuttals: list[str], conceded: list[str], risk_score: int}"
    )
    assert_no_reflection(prompt)
    return prompt


def _risk_prompt_r2(signal: dict, capital_pct: float, balance: float,
                    bull_r1: dict, bear_r1: dict) -> str:
    bull_args = _trim_args(bull_r1)
    bear_args = _trim_args(bear_r1)
    prompt = (
        f"You are the Risk Agent. Round 2 of the trading debate. "
        f"Pair: {signal.get('pair')} | Capital: {capital_pct}% of ${balance:.0f}. "
        f"Bull argued: {bull_args}. Bear argued: {bear_args}. "
        "Given both arguments, re-evaluate the position-sizing recommendation. "
        "If the Bear's concerns are valid, lower recommended_size_pct. If the Bull's "
        "case is strong AND risk is acceptable, you can hold or raise it. "
        "Respond as JSON: {risk_acceptable: bool, concerns: list[str], recommended_size_pct: int}"
    )
    assert_no_reflection(prompt)
    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# Round 3 — conditional moderator clarification on the key disagreement.
# Per blueprint: "Moderator (Decision LLM) requests clarification on key
# disagreements". Only fires when rounds 1+2 still have a meaningful split.
# ─────────────────────────────────────────────────────────────────────────────

def _moderator_question(bull_r2: dict, bear_r2: dict) -> str:
    """Build a single concrete clarifying question from the round-2 outputs."""
    bull_remaining = _trim_args(bull_r2, limit=2)
    bear_remaining = _trim_args(bear_r2, limit=2)
    if not bull_remaining and not bear_remaining:
        return "What single piece of evidence would change your mind?"
    return (
        f"Bull's strongest remaining claim: {bull_remaining[0] if bull_remaining else '(none)'} "
        f"Bear's strongest remaining claim: {bear_remaining[0] if bear_remaining else '(none)'} "
        "Pick the ONE most important disagreement and either substantiate it with "
        "specific market evidence or concede."
    )


def _bull_prompt_r3(signal: dict, moderator_q: str) -> str:
    prompt = (
        f"You are the Bull Agent. Round 3 of the trading debate. "
        f"Pair: {signal.get('pair')} | Direction: LONG. "
        f"The moderator asks: {moderator_q} "
        "Give a single final, evidence-grounded position. "
        "Respond as JSON: {argue_for: bool, key_evidence: str, confidence: int}"
    )
    assert_no_reflection(prompt)
    return prompt


def _bear_prompt_r3(signal: dict, moderator_q: str) -> str:
    prompt = (
        f"You are the Bear Agent. Round 3 of the trading debate. "
        f"Pair: {signal.get('pair')} | Direction: LONG. "
        f"The moderator asks: {moderator_q} "
        "Give a single final, evidence-grounded position. "
        "Respond as JSON: {argue_against: bool, key_evidence: str, risk_score: int}"
    )
    assert_no_reflection(prompt)
    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# Disagreement detection — whether to fire rounds 2 and 3.
# ─────────────────────────────────────────────────────────────────────────────

_R2_AMBIGUITY_GAP = 15   # was 30 — tighter now that argue_for/against catches active disagreement directly
_R2_STRENGTH_GATE = 60   # minimum signal_strength to spend LLM budget on multi-round debate


def _has_meaningful_disagreement_r1(bull: dict, bear: dict, risk: dict) -> bool:
    """Round 2 fires when:
      - Bull says argue_for=True AND Bear says argue_against=True (active disagreement), OR
      - confidence vs risk_score gap is small (< _R2_AMBIGUITY_GAP) suggesting genuine ambiguity.
    Skip round 2 on strong consensus (saves LLM calls)."""
    bull_for = bool(bull.get("argue_for", False))
    bear_against = bool(bear.get("argue_against", False))
    if bull_for and bear_against:
        return True
    bull_conf = int(bull.get("confidence", 50))
    bear_risk = int(bear.get("risk_score", 50))
    return abs(bull_conf - bear_risk) < _R2_AMBIGUITY_GAP


def _still_disagrees_r2(bull_r2: dict, bear_r2: dict) -> bool:
    """Round 3 fires only when round 2 didn't resolve the disagreement.
    Both still hold their opposing positions AND neither conceded significantly."""
    bull_for = bool(bull_r2.get("argue_for", False))
    bear_against = bool(bear_r2.get("argue_against", False))
    bull_conceded = len(bull_r2.get("conceded") or [])
    bear_conceded = len(bear_r2.get("conceded") or [])
    if bull_for and bear_against and (bull_conceded + bear_conceded) < 2:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Verdict synthesis — uses ALL rounds + per-agent weights.
# ─────────────────────────────────────────────────────────────────────────────

def _final_position(r1: dict, r2: dict | None, r3: dict | None,
                    flag_key: str) -> bool:
    """Take the latest non-empty boolean position across rounds — round 3 wins
    if present, else round 2, else round 1. None / missing rounds are skipped."""
    for src in (r3, r2, r1):
        if isinstance(src, dict) and flag_key in src:
            return bool(src.get(flag_key, False))
    return bool(r1.get(flag_key, False))


def _final_score(r1: dict, r2: dict | None, r3: dict | None,
                 score_key: str, default: int = 50) -> int:
    """Latest non-empty integer score (confidence / risk_score) across rounds."""
    for src in (r3, r2, r1):
        if isinstance(src, dict) and score_key in src:
            try:
                return int(src.get(score_key, default))
            except (TypeError, ValueError):
                return default
    try:
        return int(r1.get(score_key, default))
    except (TypeError, ValueError):
        return default


_RISK_VETO_BULL_FLOOR = 40  # eff_bull below this → bull case too weak to override a risk veto


def _synthesise(bull_r1: dict, bear_r1: dict, risk_r1: dict,
                bull_r2: dict | None, bear_r2: dict | None, risk_r2: dict | None,
                bull_r3: dict | None, bear_r3: dict | None,
                capital_pct: float, weights: dict[str, float]) -> tuple[str, float]:
    """Synthesise verdict + size_pct from final positions across all rounds.

    Per-agent weights bias the synthesis: a Bull whose track record on closed
    trades shows it argues_for on losers more often than winners gets
    weight < 1.0 → its argue_for has reduced influence on the verdict. Same
    for Bear/Risk. This is the FinCon "systematic investment beliefs" update.

    Weighted consensus (TrustTrade / WBFT pattern): the Risk agent cannot
    unilaterally veto a trade. skip_risk requires corroboration — either Bear
    also argues against, or Bull's weight-adjusted confidence is below
    _RISK_VETO_BULL_FLOOR. When Risk says no but Bull is strong and Bear is
    neutral, the verdict downgrades to reduced_allocation at 50% capital instead
    of a full block. This prevents a single overly-conservative LLM call from
    rejecting 40-50% of signals with no supporting evidence from the other agents.
    """
    w_bull = weights.get("bull", 1.0)
    w_bear = weights.get("bear", 1.0)
    w_risk = weights.get("risk", 1.0)

    bull_for = _final_position(bull_r1, bull_r2, bull_r3, "argue_for")
    bear_against = _final_position(bear_r1, bear_r2, bear_r3, "argue_against")
    risk_ok = bool(risk_r2.get("risk_acceptable", risk_r1.get("risk_acceptable", True))
                   if risk_r2 else risk_r1.get("risk_acceptable", True))

    bull_conf = _final_score(bull_r1, bull_r2, bull_r3, "confidence", 50)
    bear_risk = _final_score(bear_r1, bear_r2, bear_r3, "risk_score", 50)

    # Weight-adjusted effective scores. A high-weight agent's claim carries
    # more force; a low-weight (historically-wrong) agent's claim is dampened.
    eff_bull = bull_conf * w_bull
    eff_bear = bear_risk * w_bear
    eff_risk_ok = risk_ok if w_risk >= 0.5 else True  # untrustworthy risk → don't let it veto

    if bull_for and eff_risk_ok and not bear_against:
        return "full_allocation", capital_pct
    if bull_for and eff_risk_ok and bear_against and eff_bull > eff_bear:
        return "reduced_allocation", capital_pct * 0.7
    if not bull_for and bear_against and eff_risk_ok:
        return "skip", 0
    if not eff_risk_ok:
        # 2-of-3 weighted consensus required for a full risk block.
        # skip_risk fires only when Bear also argues against OR Bull is too weak.
        if bear_against or eff_bull <= _RISK_VETO_BULL_FLOOR:
            return "skip_risk", 0
        # Risk says no but Bull is strong and Bear is neutral — cautious trade.
        log.info("risk_veto_overridden_by_consensus",
                 eff_bull=round(eff_bull, 1), eff_bear=round(eff_bear, 1),
                 w_risk=round(w_risk, 2), floor=_RISK_VETO_BULL_FLOOR)
        return "reduced_allocation", capital_pct * 0.5
    return "exploratory", capital_pct * 0.05


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point — preserves run_debate(...) signature so signals/engine.py
# doesn't need to change.
# ─────────────────────────────────────────────────────────────────────────────

async def run_debate(signal: dict, market: dict, capital_pct: float, balance: float) -> dict:
    """
    Y-04: 2–3 round structured debate between Bull, Bear, Risk agents.
    Y-05: Decision LLM synthesises all outputs → verdict.
    Y-06: All rounds' arguments returned in the debate_log dict for downstream
          persistence (signals/engine.py:save_debate_arguments after write_signal).
    """
    from llm.decision import decide

    # ── Round 1 ──────────────────────────────────────────────────────────────
    # Sequential: Ollama has one CPU-bound slot per inference; concurrent dispatch
    # means all 3 share cores and each takes 3× longer, reliably hitting the 60s
    # timeout. Sequential gives each call the full CPU and its own 60s window.
    try:
        bull_r1_raw = await decide(_bull_prompt(signal, market), timeout=60)
    except Exception as exc:
        bull_r1_raw = exc
    try:
        bear_r1_raw = await decide(_bear_prompt(signal, market), timeout=60)
    except Exception as exc:
        bear_r1_raw = exc
    try:
        risk_r1_raw = await decide(_risk_prompt(signal, capital_pct, balance), timeout=60)
    except Exception as exc:
        risk_r1_raw = exc
    bull_failed = isinstance(bull_r1_raw, Exception) or not isinstance(bull_r1_raw, dict)
    bear_failed = isinstance(bear_r1_raw, Exception) or not isinstance(bear_r1_raw, dict)
    risk_failed = isinstance(risk_r1_raw, Exception) or not isinstance(risk_r1_raw, dict)
    if bull_failed:
        log.warning("debate_bull_fallback", err=str(bull_r1_raw)[:120])
        bull_r1 = {"argue_for": False, "confidence": 50}
    else:
        bull_r1 = bull_r1_raw
    if bear_failed:
        log.warning("debate_bear_fallback", err=str(bear_r1_raw)[:120])
        bear_r1 = {"argue_against": False, "risk_score": 50}
    else:
        bear_r1 = bear_r1_raw
    if risk_failed:
        log.warning("debate_risk_fallback", err=str(risk_r1_raw)[:120])
        risk_r1 = {"risk_acceptable": True, "recommended_size_pct": capital_pct}
    else:
        risk_r1 = risk_r1_raw

    # ── Majority-failed fallback (LLM circuit open / degraded) ──────────────
    # cont. 68b: was `all three failed`. But under a DEGRADED LLM (e.g. Ollama
    # serializing 3 concurrent decide() calls past the 30s timeout) the typical
    # pattern is 1-2 agents time out while one succeeds. With only a partial
    # result, the failed Bull defaults to argue_for=False (line above) which
    # biases the synthesised verdict toward SKIP — rejecting trades for an
    # infrastructure reason, not signal quality. So if a MAJORITY (≥2/3) of
    # agents failed, treat the LLM as unavailable and fail OPEN to
    # full_allocation rather than synthesising a skip from one real opinion.
    if (int(bull_failed) + int(bear_failed) + int(risk_failed)) >= 2:
        log.info("debate_no_llm_full_allocation", pair=signal.get("pair"),
                 bull_failed=bull_failed, bear_failed=bear_failed,
                 risk_failed=risk_failed)
        result = {
            "verdict": "full_allocation",
            "size_pct": round(capital_pct, 2),
            "rounds_used": 1,
            "rounds": {1: {"bull": bull_r1, "bear": bear_r1, "risk": risk_r1}},
            "weights": get_agent_weights(),
            "bull": bull_r1, "bear": bear_r1, "risk": risk_r1,  # back-compat fields
            "llm_available": False,
        }
        redis_client.get().publish(redis_keys.CH_BRAIN_DECISION, json.dumps({
            "type": "debate_result", "pair": signal.get("pair"),
            "verdict": "full_allocation",
        }))
        log.info("debate_complete", pair=signal.get("pair"),
                 verdict="full_allocation", size_pct=round(capital_pct, 2),
                 rounds_used=1)
        return result

    # ── Round 2 — only when ALL r1 agents succeeded AND there's disagreement ─
    # Phase-C cost gate: skip multi-round debate on weak signals. Below
    # _R2_STRENGTH_GATE the marginal value of resolving bull/bear nuance
    # is dominated by other rejection paths in signals/engine.py, so
    # burning 3-5 more LLM calls per signal isn't worth it.
    bull_r2 = bear_r2 = risk_r2 = None
    rounds_used = 1
    _strength = float(signal.get("signal_strength") or 0)
    _strong_enough = _strength >= _R2_STRENGTH_GATE
    fire_r2 = (not (bull_failed or bear_failed or risk_failed)
               and _strong_enough
               and _has_meaningful_disagreement_r1(bull_r1, bear_r1, risk_r1))
    if not _strong_enough:
        log.info("debate_r2_skipped_weak_signal",
                 pair=signal.get("pair"), signal_strength=_strength,
                 threshold=_R2_STRENGTH_GATE)
    if fire_r2:
        try:
            try:
                bull_r2_raw = await decide(_bull_prompt_r2(signal, market, bear_r1, risk_r1), timeout=60)
            except Exception as exc:
                bull_r2_raw = exc
            try:
                bear_r2_raw = await decide(_bear_prompt_r2(signal, market, bull_r1, risk_r1), timeout=60)
            except Exception as exc:
                bear_r2_raw = exc
            try:
                risk_r2_raw = await decide(_risk_prompt_r2(signal, capital_pct, balance, bull_r1, bear_r1), timeout=60)
            except Exception as exc:
                risk_r2_raw = exc
            if isinstance(bull_r2_raw, dict):
                bull_r2 = bull_r2_raw
            if isinstance(bear_r2_raw, dict):
                bear_r2 = bear_r2_raw
            if isinstance(risk_r2_raw, dict):
                risk_r2 = risk_r2_raw
            # If ANY agent failed in r2, the round is partial but we still keep
            # whatever came back. _final_position falls back to r1 when r2 is None.
            if bull_r2 or bear_r2 or risk_r2:
                rounds_used = 2
        except Exception as exc:
            log.warning("debate_round2_failed", err=str(exc)[:120])

    # ── Round 3 — conditional moderator clarification ────────────────────────
    bull_r3 = bear_r3 = None
    fire_r3 = (rounds_used >= 2 and bull_r2 is not None and bear_r2 is not None
               and _still_disagrees_r2(bull_r2, bear_r2))
    if fire_r3:
        try:
            mod_q = _moderator_question(bull_r2, bear_r2)
            try:
                bull_r3_raw = await decide(_bull_prompt_r3(signal, mod_q), timeout=45)
            except Exception as exc:
                bull_r3_raw = exc
            try:
                bear_r3_raw = await decide(_bear_prompt_r3(signal, mod_q), timeout=45)
            except Exception as exc:
                bear_r3_raw = exc
            if isinstance(bull_r3_raw, dict):
                bull_r3 = bull_r3_raw
            if isinstance(bear_r3_raw, dict):
                bear_r3 = bear_r3_raw
            if bull_r3 or bear_r3:
                rounds_used = 3
        except Exception as exc:
            log.warning("debate_round3_failed", err=str(exc)[:120])

    # ── Verdict synthesis with per-agent weights ─────────────────────────────
    weights = get_agent_weights()
    verdict, size_pct = _synthesise(
        bull_r1, bear_r1, risk_r1,
        bull_r2, bear_r2, risk_r2,
        bull_r3, bear_r3,
        capital_pct, weights,
    )

    # Build full multi-round result for downstream persistence.
    rounds_payload: dict = {
        1: {"bull": bull_r1, "bear": bear_r1, "risk": risk_r1},
    }
    if rounds_used >= 2:
        rounds_payload[2] = {"bull": bull_r2, "bear": bear_r2, "risk": risk_r2}
    if rounds_used >= 3:
        rounds_payload[3] = {"bull": bull_r3, "bear": bear_r3}

    result = {
        "verdict": verdict,
        "size_pct": round(size_pct, 2),
        "rounds_used": rounds_used,
        "rounds": rounds_payload,
        "weights": weights,
        # Back-compat keys — older consumers (dashboards, signals/engine.py)
        # read bull/bear/risk directly. Mirror the final round.
        "bull": bull_r3 or bull_r2 or bull_r1,
        "bear": bear_r3 or bear_r2 or bear_r1,
        "risk": risk_r2 or risk_r1,
    }

    redis_client.get().publish(redis_keys.CH_BRAIN_DECISION, json.dumps({
        "type": "debate_result",
        "pair": signal.get("pair"),
        "verdict": verdict,
        "rounds_used": rounds_used,
    }))
    log.info("debate_complete", pair=signal.get("pair"),
             verdict=verdict, size_pct=round(size_pct, 2),
             rounds_used=rounds_used)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Persistence — called from signals/engine.py after write_signal returns
# signal_id (and optionally after open_trade returns trade_id).
# ─────────────────────────────────────────────────────────────────────────────

def _score_for_role(role: str, agent_out: dict) -> int | None:
    """Extract the numeric score for an agent's output (confidence / risk_score /
    recommended_size_pct). Returns None if unavailable."""
    if not isinstance(agent_out, dict):
        return None
    key = {"bull": "confidence", "bear": "risk_score",
           "risk": "recommended_size_pct"}.get(role)
    if not key:
        return None
    try:
        return int(agent_out.get(key)) if agent_out.get(key) is not None else None
    except (TypeError, ValueError):
        return None


def save_debate_arguments(signal_id: str, trade_id: str | None,
                          debate_log: dict) -> int:
    """Persist every (agent, round) tuple from a debate. Returns number of rows
    inserted. ON CONFLICT DO NOTHING is used to make this idempotent in case of
    retries — primary key (signal_id, agent_role, round_num) prevents dupes.

    Skips persistence when llm_available=False (the all-failed fallback path)
    since the args are just the default dicts and aren't worth a DB row.
    """
    if debate_log.get("llm_available") is False:
        return 0
    rounds = debate_log.get("rounds") or {}
    if not rounds:
        return 0

    inserted = 0
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                for round_num, agents in rounds.items():
                    if not isinstance(agents, dict):
                        continue
                    for role, agent_out in agents.items():
                        if not isinstance(agent_out, dict):
                            continue
                        score = _score_for_role(role, agent_out)
                        cur.execute(
                            "INSERT INTO debate_arguments "
                            "(signal_id, trade_id, agent_role, round_num, arguments, score) "
                            "VALUES (%s, %s, %s, %s, %s::jsonb, %s) "
                            "ON CONFLICT (signal_id, agent_role, round_num) DO NOTHING",
                            (signal_id, trade_id, role, int(round_num),
                             json.dumps(agent_out), score),
                        )
                        inserted += cur.rowcount
        # Evidence keys for feature_health.
        try:
            import time as _t
            r = redis_client.get()
            r.incrby("debate:args_persisted_count", inserted)
            r.set("debate:args_last_ts", str(int(_t.time())))
        except Exception:
            pass
    except Exception as exc:
        log.warning("save_debate_arguments_failed", error=str(exc)[:200])
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# Verbal reinforcement — called from memory/write.py:write_trade_close after
# every closed trade. Reads the persisted args, computes was_correct per agent
# from the actual outcome, writes back to debate_arguments.was_correct, and
# updates the Redis weight priors.
# ─────────────────────────────────────────────────────────────────────────────

_CATASTROPHIC_LOSS_FRAC = 0.10  # > 10% of capital → Risk was wrong to call it acceptable

def _agent_was_correct(role: str, agent_out: dict, won: bool,
                       net_pnl_usdt: float, capital_usdt: float) -> bool | None:
    """Per-agent correctness rule. Returns None when we can't decide
    (e.g., agent's round-1 dict didn't have its position field — shouldn't
    happen in practice but defends against malformed LLM output)."""
    if not isinstance(agent_out, dict):
        return None
    if role == "bull":
        if "argue_for" not in agent_out:
            return None
        return bool(agent_out.get("argue_for")) == bool(won)
    if role == "bear":
        if "argue_against" not in agent_out:
            return None
        # Bear is correct when it argued_against AND the trade lost,
        # OR when it didn't argue_against AND the trade won.
        return bool(agent_out.get("argue_against")) != bool(won)
    if role == "risk":
        if "risk_acceptable" not in agent_out:
            return None
        ok = bool(agent_out.get("risk_acceptable"))
        # Risk is right when it called acceptable AND no catastrophic loss,
        # or when it called unacceptable AND there was a catastrophic loss.
        catastrophic = (capital_usdt > 0 and net_pnl_usdt < 0
                        and abs(net_pnl_usdt) / capital_usdt > _CATASTROPHIC_LOSS_FRAC)
        return ok != catastrophic
    return None


def update_beliefs_on_close(trade_id: str, won: bool,
                            net_pnl_usdt: float, capital_usdt: float) -> dict:
    """Verbal reinforcement: read persisted debate args for this trade,
    compute was_correct per agent role (averaged across rounds), back-fill
    debate_arguments.was_correct, and update per-agent weights in Redis.

    Returns summary dict for logging and feature_health evidence.
    """
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT signal_id, agent_role, round_num, arguments "
                    "FROM debate_arguments WHERE trade_id = %s",
                    (trade_id,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        log.warning("update_beliefs_query_failed", error=str(exc)[:200])
        return {"status": "query_failed", "trade_id": trade_id}

    if not rows:
        return {"status": "no_debate_args", "trade_id": trade_id}

    # Group args by role (a role can appear in 1-3 rounds).
    role_args: dict[str, list[tuple[int, dict]]] = {}
    for _sid, role, round_num, args_json in rows:
        if isinstance(args_json, str):
            try:
                args_json = json.loads(args_json)
            except Exception:
                args_json = {}
        role_args.setdefault(role, []).append((int(round_num), args_json))

    # Correctness per (role, round) — write back. Then compute role-level
    # correctness = the LATEST round (most-evolved position).
    role_correct: dict[str, bool] = {}
    update_pairs: list[tuple[bool, str, str, int]] = []  # (was_correct, signal_id, role, round)
    signal_id_for_writeback = rows[0][0]

    for role, rounds in role_args.items():
        rounds.sort(key=lambda x: x[0])  # ascending round order
        for round_num, args in rounds:
            res = _agent_was_correct(role, args, won, net_pnl_usdt, capital_usdt)
            if res is not None:
                update_pairs.append((res, signal_id_for_writeback, role, round_num))
        # Latest-round position wins for the weight update.
        last_round_num, last_args = rounds[-1]
        latest = _agent_was_correct(role, last_args, won, net_pnl_usdt, capital_usdt)
        if latest is not None:
            role_correct[role] = latest

    # Batch write was_correct.
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                for correct, sid, role, rnd in update_pairs:
                    cur.execute(
                        "UPDATE debate_arguments SET was_correct = %s "
                        "WHERE signal_id = %s AND agent_role = %s AND round_num = %s",
                        (correct, sid, role, rnd),
                    )
    except Exception as exc:
        log.warning("update_beliefs_writeback_failed", error=str(exc)[:200])

    # Update per-agent weights — constant-α toward {0.0 if wrong, 1.0 if right}.
    # weight stays close to 1.0 by default and drifts based on track record.
    weights_before = get_agent_weights()
    weights_after: dict[str, float] = {}
    for role in ("bull", "bear", "risk"):
        if role in role_correct:
            target = 1.0 if role_correct[role] else 0.5  # wrong → drift toward 0.5
            new_w = weights_before[role] + _AGENT_WEIGHT_ALPHA * (target - weights_before[role])
            _set_agent_weight(role, new_w)
            weights_after[role] = new_w
        else:
            weights_after[role] = weights_before[role]

    # Evidence keys.
    try:
        import time as _t
        r = redis_client.get()
        r.incr("debate:beliefs_updates_count")
        r.set("debate:beliefs_last_ts", str(int(_t.time())))
    except Exception:
        pass

    summary = {
        "status": "updated",
        "trade_id": trade_id,
        "won": won,
        "role_correct": role_correct,
        "weights_before": weights_before,
        "weights_after": weights_after,
        "rounds_updated": len(update_pairs),
    }
    log.info("debate_beliefs_updated",
             trade_id=trade_id, won=won,
             role_correct=role_correct,
             weights_after={k: round(v, 3) for k, v in weights_after.items()})
    return summary
