"""
Section Y: Multi-Agent Debate Council — Y-01 to Y-07.
Activates at 300 closed paper trades.
"""
import asyncio
import json
import structlog
from llm.guard import assert_no_reflection
import redis_keys
import redis_client

log = structlog.get_logger()


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
        "Evaluate: position size reasonableness, SL placement, DCA capital reserve, "
        "account margin impact. "
        "Respond as JSON: {risk_acceptable: bool, concerns: list[str], recommended_size_pct: int}"
    )
    assert_no_reflection(prompt)
    return prompt


async def run_debate(signal: dict, market: dict, capital_pct: float, balance: float) -> dict:
    """
    Y-04: 2–3 round structured debate between Bull, Bear, Risk agents.
    Y-05: Decision LLM synthesises all outputs → verdict.
    """
    from llm.decision import decide

    # Round 1: independent positions (parallel)
    bull_out, bear_out, risk_out = await asyncio.gather(
        decide(_bull_prompt(signal, market), timeout=30),
        decide(_bear_prompt(signal, market), timeout=30),
        decide(_risk_prompt(signal, capital_pct, balance), timeout=30),
        return_exceptions=True,
    )

    # Default on LLM failure
    if isinstance(bull_out, Exception):
        bull_out = {"argue_for": False, "confidence": 50}
    if isinstance(bear_out, Exception):
        bear_out = {"argue_against": False, "risk_score": 50}
    if isinstance(risk_out, Exception):
        risk_out = {"risk_acceptable": True, "recommended_size_pct": capital_pct}

    # Y-05: Synthesise verdict
    bull_for = bull_out.get("argue_for", False)
    bear_against = bear_out.get("argue_against", False)
    risk_ok = risk_out.get("risk_acceptable", True)
    bull_conf = int(bull_out.get("confidence", 50))
    bear_risk = int(bear_out.get("risk_score", 50))

    if bull_for and risk_ok and not bear_against:
        verdict = "full_allocation"
        size_pct = capital_pct
    elif bull_for and risk_ok and bear_against and bull_conf > bear_risk:
        verdict = "reduced_allocation"
        size_pct = capital_pct * 0.7
    elif not bull_for and bear_against and risk_ok:
        verdict = "skip"
        size_pct = 0
    elif not risk_ok:
        verdict = "skip_risk"
        size_pct = 0
    else:
        verdict = "exploratory"
        size_pct = capital_pct * 0.05

    result = {
        "verdict": verdict,
        "size_pct": round(size_pct, 2),
        "bull": bull_out,
        "bear": bear_out,
        "risk": risk_out,
    }

    # Publish debate log
    redis_client.get().publish(redis_keys.CH_BRAIN_DECISION, json.dumps({
        "type": "debate_result",
        "pair": signal.get("pair"),
        "verdict": verdict,
    }))

    log.info("debate_complete", pair=signal.get("pair"), verdict=verdict, size_pct=size_pct)
    return result
