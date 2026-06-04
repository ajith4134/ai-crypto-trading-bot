"""cont. 60 — Multi-agent LLM Exit Council.

TradingAgents (arXiv:2412.20138) framework adapted for EXIT decisions.
Three agents debate whether to exit at key moments:

  * Bull Researcher  — argues for holding/extending the position
  * Bear Researcher  — argues for closing or tightening
  * Risk Manager     — gates by drawdown / vol / regime

Council is invoked SPARINGLY (not on every tick — would burn LLM budget):
  * On TP1 hit (decide between fixed TP2 vs chandelier-tail behaviour)
  * On peak-profit-pct >= 5% (decide whether to ratchet aggressively)
  * On SL within 0.5% of mark (last-chance review before getting stopped)

The orchestrator caches verdicts for 60s per trade so we don't re-query on
every tick. Budget-bounded.

Source: arXiv:2412.20138 TradingAgents (Dec 2024), arXiv:2510.15949 ATLAS.
"""
from __future__ import annotations
from typing import Optional
import json
import asyncio
import structlog

from .decision import ExitDecision

log = structlog.get_logger()


_COUNCIL_CACHE_TTL = 60   # seconds — verdict valid for this long
_PEAK_TRIGGER_PCT  = 0.05  # 5% peak profit triggers council review
_SL_PROX_TRIGGER   = 0.005 # SL within 0.5% of mark triggers council


def _should_invoke(trade, mark, sl_level, r) -> tuple[bool, str]:
    """Decide if council should be invoked. Returns (True, reason) or (False, "")."""
    trade_id = str(trade.get("id"))
    # Per-trade cooldown
    if r.get(f"llm_council:cooldown:{trade_id}"):
        return False, "cooldown"

    # Trigger 1: TP1 just fired (one-shot)
    # Phase A write-through (cont. 55): also accept tp_fired as the same trigger.
    tp1_fired = r.get(f"trade:{trade_id}:tp1_fired") == "1"
    tp_fired  = r.get(f"trade:{trade_id}:tp_fired")  == "1"
    council_seen_tp1 = r.get(f"llm_council:tp1_seen:{trade_id}") == "1"
    if (tp1_fired or tp_fired) and not council_seen_tp1:
        return True, "tp1_just_fired"

    # Trigger 2: Peak profit > 5%
    peak_pnl = float(trade.get("peak_pnl_usdt") or 0)
    capital  = float(trade.get("capital_usdt") or 0)
    leverage = int(trade.get("leverage") or 1)
    notional = capital * leverage
    if notional > 0:
        peak_pct = peak_pnl / notional
        last_pct_raw = r.get(f"llm_council:last_peak:{trade_id}")
        try:
            last_pct = float(last_pct_raw) if last_pct_raw is not None else 0.0
        except (TypeError, ValueError):
            last_pct = 0.0
        if peak_pct >= _PEAK_TRIGGER_PCT and (peak_pct - last_pct) >= 0.02:
            return True, "peak_5pct_plus"

    # Trigger 3: SL very close to mark (last-chance review)
    if sl_level > 0 and mark > 0:
        prox = abs(mark - sl_level) / mark
        if prox <= _SL_PROX_TRIGGER:
            return True, "sl_near_mark"

    return False, ""


def _build_council_prompts(trade, mark, sl_level, regime_now) -> tuple[str, str, str]:
    """Build bull/bear/risk prompts. Compact — under 600 tokens each."""
    pair = trade["pair"]
    direction = trade["direction"]
    entry = float(trade.get("average_entry") or trade.get("entry_price") or 0)
    capital = float(trade.get("capital_usdt") or 0)
    leverage = int(trade.get("leverage") or 1)
    peak_pnl = float(trade.get("peak_pnl_usdt") or 0)
    peak_loss = float(trade.get("peak_loss_usdt") or 0)
    pct_change = (mark - entry) / entry * 100 if entry > 0 else 0.0
    dir_sign = 1 if direction == "long" else -1
    realised_dir_pct = pct_change * dir_sign  # positive when in favour
    sl_pct = (sl_level - entry) / entry * 100 if (sl_level > 0 and entry > 0) else 0.0
    base_ctx = (
        f"Trade {pair} {direction}, entry {entry:.6f}, mark {mark:.6f} "
        f"({realised_dir_pct:+.2f}% favourable), SL {sl_level:.6f} ({sl_pct:+.2f}%), "
        f"peak profit ${peak_pnl:.2f}, peak loss ${peak_loss:.2f}, "
        f"capital ${capital} × {leverage}x, regime {regime_now}."
    )
    bull = (
        f"You are the Bull Researcher. {base_ctx} Argue why holding/extending "
        f"this {direction} position is correct now. Cite microstructure, regime, "
        "or trend continuation reasons. JSON: {action:'hold'|'tighten'|'exit', "
        "conviction:0-100, reason:str (<=140 chars)}."
    )
    bear = (
        f"You are the Bear Researcher. {base_ctx} Argue why closing or tightening "
        f"this {direction} position is correct now. Cite exhaustion, divergence, or "
        "regime risk. JSON: {action:'hold'|'tighten'|'exit', conviction:0-100, "
        "reason:str (<=140 chars)}."
    )
    risk = (
        f"You are the Risk Manager. {base_ctx} Decide the safest action: hold, "
        "tighten SL, or exit. Cite drawdown / vol / leverage. JSON: "
        "{action:'hold'|'tighten'|'exit', conviction:0-100, reason:str (<=140 chars)}."
    )
    return bull, bear, risk


def _aggregate_verdict(bull_v: dict, bear_v: dict, risk_v: dict) -> dict:
    """Aggregate 3 verdicts into final action.

    Rule:
      * If risk_manager says exit with conviction >= 70 → exit
      * If 2 of 3 agree on an action with avg conviction >= 60 → that action
      * Else → hold
    """
    actions = []
    for v in (bull_v, bear_v, risk_v):
        if isinstance(v, dict):
            a = v.get("action", "hold")
            c = float(v.get("conviction", 0))
            actions.append((a, c))
        else:
            actions.append(("hold", 0.0))

    # Risk manager override
    risk_action, risk_conv = actions[2]
    if risk_action == "exit" and risk_conv >= 70:
        return {"final": "exit", "reason": "risk_manager_override"}

    # Majority rule
    from collections import Counter
    action_counts = Counter(a for a, c in actions)
    most_common, n = action_counts.most_common(1)[0]
    if n >= 2:
        # Average conviction for the majority action
        majority_conv = sum(c for a, c in actions if a == most_common) / n
        if majority_conv >= 60:
            return {"final": most_common, "reason": f"majority_{n}of3_conv{majority_conv:.0f}"}

    return {"final": "hold", "reason": "no_consensus"}


def evaluate_llm_exit_council(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Run multi-agent LLM debate IF trigger conditions met.

    Sync wrapper around the async council (uses asyncio.run since sl_monitor
    is itself async — we schedule the coroutine on the existing loop).
    """
    invoke, trigger = _should_invoke(trade, mark, sl_level, r)
    if not invoke:
        return None

    import redis_keys
    regime_now = str(r.get(redis_keys.CURRENT_REGIME) or "unknown").lower()
    bull_p, bear_p, risk_p = _build_council_prompts(trade, mark, sl_level, regime_now)

    # Run all 3 prompts in parallel. We need an event loop — and we're already
    # in one (sl_monitor is async). Use asyncio.get_running_loop + ensure_future.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return None
    if not loop.is_running():
        return None

    # We can't await here (this fn is sync, called from a sync context inside
    # an async function). Schedule the council, return None this tick, and
    # cache the verdict for the next tick to read. This is the budget-bound
    # pattern that keeps sl_monitor non-blocking.
    trade_id = str(trade.get("id"))
    if r.get(f"llm_council:in_flight:{trade_id}"):
        # Verdict already requested — check if it landed.
        verdict_raw = r.get(f"llm_council:verdict:{trade_id}")
        if verdict_raw is None:
            return None
        try:
            verdict = json.loads(verdict_raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        r.delete(f"llm_council:in_flight:{trade_id}")
        r.setex(f"llm_council:cooldown:{trade_id}", _COUNCIL_CACHE_TTL, "1")
        final = verdict.get("final", "hold")
        if final == "exit":
            try:
                r.incr("trail:llm_council_force_exit_count")
            except Exception:
                pass
            return ExitDecision(force_close=True,
                                reason="llm_council_exit",
                                notes={"verdict": verdict, "trigger": trigger})
        if final == "tighten":
            try:
                r.incr("trail:llm_council_tighten_count")
            except Exception:
                pass
            return ExitDecision(tighten_sl_mult=0.6,
                                notes={"verdict": verdict, "trigger": trigger})
        return None

    # Schedule the council in background. Mark in-flight.
    r.setex(f"llm_council:in_flight:{trade_id}", 60, "1")
    if trigger == "tp1_just_fired":
        r.set(f"llm_council:tp1_seen:{trade_id}", "1")
    if trigger == "peak_5pct_plus":
        peak_pnl = float(trade.get("peak_pnl_usdt") or 0)
        capital = float(trade.get("capital_usdt") or 0)
        leverage = int(trade.get("leverage") or 1)
        notional = max(capital * leverage, 1)
        r.set(f"llm_council:last_peak:{trade_id}", peak_pnl / notional)

    async def _run_council():
        from llm.decision import decide
        try:
            results = await asyncio.gather(
                decide(bull_p, timeout=15),
                decide(bear_p, timeout=15),
                decide(risk_p, timeout=15),
                return_exceptions=True,
            )
            bull_v = results[0] if isinstance(results[0], dict) else {}
            bear_v = results[1] if isinstance(results[1], dict) else {}
            risk_v = results[2] if isinstance(results[2], dict) else {}
            verdict = _aggregate_verdict(bull_v, bear_v, risk_v)
            r.setex(f"llm_council:verdict:{trade_id}", 120, json.dumps(verdict))
            try:
                r.incr("trail:llm_council_invoked_count")
            except Exception:
                pass
            log.info("llm_council_verdict",
                     trade_id=trade_id, trigger=trigger,
                     final=verdict.get("final"),
                     bull=bull_v.get("action"),
                     bear=bear_v.get("action"),
                     risk=risk_v.get("action"))
        except Exception as exc:
            log.warning("llm_council_failed",
                        trade_id=trade_id, error=str(exc)[:150])
            r.delete(f"llm_council:in_flight:{trade_id}")

    try:
        asyncio.ensure_future(_run_council())
    except RuntimeError:
        # Loop not running — give up cleanly.
        r.delete(f"llm_council:in_flight:{trade_id}")
        return None
    return None  # verdict will be picked up on next tick
