"""
Blueprint F9 Miss Decoder + F12 Mismatch Decoder.

Both decoders run as background Celery tasks (see celery_app.py
decode_pending_misses / decode_pending_mismatches). They consume the
already-accumulated counterfactual + trade history and write LLM-decoded
postmortems back to the database:

  F9 → counterfactuals.miss_decode_reason / miss_decoded
  F12 → mismatches table (introduced by migration 014)

The LLM provider chain is the same 5-provider Llama 3.3 70B stack used by
self_improve / research_engine via llm.researcher.research(). Never called
from the live trading loop.

Rule 4 honesty:
  - The decoders explain trade outcomes IN HINDSIGHT. They produce
    written postmortems consumed by humans (and stored for future RAG/
    training). They do NOT yet wire back into is_rejection_filter — that
    "Filter Improvement Loop" is the next layer and is documented as a
    future enhancement.
  - Both decoders are strictly read-side analytics: they never mutate
    trade/signal state. The only writes are decode_reason text + a
    timestamp/flag indicating "decoded".
"""
import json
import structlog

log = structlog.get_logger()


_MISS_SYSTEM = (
    "You are a quant postmortem analyst. The bot rejected a trade signal "
    "that, in hindsight, would have won. Identify the specific feature, "
    "rule, or pattern that caused the rejection, explain why that rule was "
    "wrong on THIS trade, and choose ONE bounded action from the action "
    "vocabulary below to nudge the rejection filter. Pick `no_change` if "
    "this one missed-win does not justify a system-wide adjustment.\n\n"
    "Additionally, you MUST tag the root cause with ONE entry from the tag "
    "vocabulary below. Tags are how the system aggregates causes across "
    "thousands of rejections — never invent a tag outside this list.\n\n"
    "Tag vocabulary (pick exactly one):\n"
    "  signal_too_weak     — strength below threshold was the gating cause\n"
    "  regime_mismatch     — regime detector disagreed with the trade\n"
    "  memrl_rejected      — MemRL win-rate floor rejected\n"
    "  cooldown            — recent loss/exit cooldown blocked\n"
    "  sizer_zero          — position sizer returned 0 (budget/risk)\n"
    "  governance_blocked  — F30/F46 or related governance veto\n"
    "  liquidity_thin      — orderbook depth was insufficient\n"
    "  noise               — single-sample miss, no systemic cause\n\n"
    "Action vocabulary (pick exactly one):\n"
    "  tighten_min_signal_strength  — raise the strength threshold\n"
    "  loosen_min_signal_strength   — lower the strength threshold\n"
    "  tighten_memrl_threshold      — raise MemRL's reject win-rate floor\n"
    "  loosen_memrl_threshold       — lower MemRL's reject win-rate floor\n"
    "  no_change                    — this miss is noise; don't adjust\n"
    "Magnitude: 'small' for a typical miss, 'medium' only for a clearly "
    "systematic rule failure.\n\n"
    "MANDATORY: You MUST include a complete `filter_change` object in your "
    "response. If you are uncertain or this miss looks like noise, use "
    "`{\"action\": \"no_change\", \"magnitude\": \"small\", \"rationale\": "
    "\"single-sample noise\"}`. Never omit the filter_change key. Never "
    "emit prose-only output."
)

_MISMATCH_SYSTEM = (
    "You are a quant postmortem analyst. The bot rated trade A as HIGH "
    "potential but it lost; it rated trade B as LOW potential but it won. "
    "Identify what the Trade Potential Scorer OVERWEIGHTED on A and what it "
    "UNDERWEIGHTED on B. Choose ONE bounded action from the vocabulary "
    "below to re-weight a single scorer component. Pick `no_change` if the "
    "mismatch is noise or unrelated to weighting.\n\n"
    "Action vocabulary (pick exactly one):\n"
    "  increase_regime_weight  — regime confluence matters more\n"
    "  decrease_regime_weight  — regime confluence matters less\n"
    "  increase_ofi_weight     — microstructure flow matters more\n"
    "  decrease_ofi_weight     — microstructure flow matters less\n"
    "  increase_tft_weight     — short-horizon ML matters more\n"
    "  decrease_tft_weight     — short-horizon ML matters less\n"
    "  no_change               — this mismatch is noise\n"
    "Magnitude: 'small' for a typical mismatch, 'medium' only for a clearly "
    "systematic weighting error.\n\n"
    "MANDATORY: You MUST include a complete `scorer_change` object. If you "
    "are uncertain or this looks like noise, use "
    "`{\"action\": \"no_change\", \"magnitude\": \"small\", \"rationale\": "
    "\"single-pair noise\"}`. Never omit the scorer_change key."
)


def _truncate(value: object, limit: int = 240) -> str:
    """Defensive truncation for LLM context to avoid bloat from JSONB blobs."""
    s = str(value) if value is not None else ""
    return s if len(s) <= limit else s[:limit] + "…"


def _miss_prompt(cf: dict, sig: dict) -> str:
    return (
        f"{_MISS_SYSTEM}\n\n"
        f"Rejected signal context:\n"
        f"  Pair: {sig.get('pair')}\n"
        f"  Direction: {sig.get('direction')}\n"
        f"  Timeframe: {sig.get('timeframe') or 'n/a'}\n"
        f"  Signal strength: {sig.get('signal_strength')}\n"
        f"  Market regime: {sig.get('market_regime') or 'n/a'}\n"
        f"  Rejection reason: {_truncate(sig.get('rejection_reason'))}\n"
        f"  Brain stage at rejection: {sig.get('brain_stage')}\n\n"
        f"Counterfactual outcome (72h tracking window):\n"
        f"  Would-have-won: {cf.get('would_have_won')}\n"
        f"  Peak profit %: {cf.get('peak_profit_pct')}\n"
        f"  Peak loss %: {cf.get('peak_loss_pct')}\n"
        f"  Trailing-SL exit %: {cf.get('trailing_sl_exit_pct')}\n\n"
        "Output STRICT JSON, no surrounding prose. Keys:\n"
        '  "decode_reason": one-paragraph postmortem (<= 80 words)\n'
        '  "miss_tag": one of the tag vocabulary above (string)\n'
        '  "miss_tag_confidence": float in [0.0, 1.0] for confidence in the tag\n'
        '  "miss_tag_evidence": {"feature": "value"} dict of 2-4 features driving the tag\n'
        '  "predicted_peak_profit_pct": numeric — your best estimate of the\n'
        '     peak profit % a SIMILAR signal would yield if accepted again.\n'
        '     Anchor on the realised peak_profit_pct above; ADJUST up or down\n'
        '     if this miss looks systematic vs. noise. Clamped to [-200, 200].\n'
        '  "filter_change": {\n'
        '     "action": one of the action vocabulary above (string),\n'
        '     "magnitude": "small" or "medium",\n'
        '     "rationale": one sentence explaining the choice\n'
        '  }\n'
    )


def _mismatch_prompt(loser: dict, winner: dict) -> str:
    return (
        f"{_MISMATCH_SYSTEM}\n\n"
        "Trade A (HIGH potential, LOST):\n"
        f"  Pair: {loser.get('pair')}\n"
        f"  Direction: {loser.get('direction')}\n"
        f"  Trade potential score: {loser.get('trade_potential_score')}\n"
        f"  Direction confidence: {loser.get('direction_confidence')}\n"
        f"  Market regime at entry: {loser.get('market_regime') or 'n/a'}\n"
        f"  Exit reason: {loser.get('exit_reason') or 'n/a'}\n"
        f"  Net PnL (USDT): {loser.get('net_pnl_usdt')}\n"
        f"  Hold seconds: {loser.get('hold_time_seconds')}\n\n"
        "Trade B (LOW potential, WON):\n"
        f"  Pair: {winner.get('pair')}\n"
        f"  Direction: {winner.get('direction')}\n"
        f"  Trade potential score: {winner.get('trade_potential_score')}\n"
        f"  Direction confidence: {winner.get('direction_confidence')}\n"
        f"  Market regime at entry: {winner.get('market_regime') or 'n/a'}\n"
        f"  Exit reason: {winner.get('exit_reason') or 'n/a'}\n"
        f"  Net PnL (USDT): {winner.get('net_pnl_usdt')}\n"
        f"  Hold seconds: {winner.get('hold_time_seconds')}\n\n"
        "Output STRICT JSON, no surrounding prose. Keys:\n"
        '  "decode_reason": one-paragraph postmortem (<= 80 words)\n'
        '  "scorer_change": {\n'
        '     "action": one of the action vocabulary above (string),\n'
        '     "magnitude": "small" or "medium",\n'
        '     "rationale": one sentence explaining the choice\n'
        '  }\n'
    )


def _strict_json_parse(raw: str) -> dict | None:
    """Same pattern as celery_app.research_strategy — pull the first {...} block
    and strict-load. Returns None on any failure."""
    if not raw:
        return None
    start = raw.find("{")
    end   = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except Exception:
        return None


def decode_miss(cf: dict, sig: dict) -> dict | None:
    """F9: LLM-decode a shadow-win counterfactual. Returns parsed JSON or None."""
    try:
        from llm.guard import assert_no_reflection
        from llm.researcher import research
    except Exception as exc:
        log.warning("decode_miss_import_failed", error=str(exc)[:120])
        return None

    prompt = _miss_prompt(cf, sig)
    assert_no_reflection(prompt)
    try:
        raw = research(prompt, max_new_tokens=256)
    except Exception as exc:
        log.warning("decode_miss_llm_failed",
                    pair=sig.get("pair"), error=str(exc)[:200])
        return None
    parsed = _strict_json_parse(raw)
    if not parsed or not parsed.get("decode_reason"):
        log.info("decode_miss_no_payload",
                 pair=sig.get("pair"), raw_preview=raw[:120] if raw else "")
        return None
    # Cont. 41: enforce mandatory filter_change. If the LLM omits it despite
    # the prompt insisting, synthesise a `no_change` so the actuator at least
    # records the decode happened. Counts visible via decoders:f9_synthetic_*
    # so we can monitor how often the LLM ignores the instruction.
    if not isinstance(parsed.get("filter_change"), dict):
        try:
            import redis_client as _rc
            _rc.get().incr("decoders:f9_synthetic_no_change_count")
        except Exception:
            pass
        parsed["filter_change"] = {
            "action": "no_change",
            "magnitude": "small",
            "rationale": "llm_omitted_filter_change_synthesised",
        }
    _VALID_MISS_TAGS = {
        "signal_too_weak", "regime_mismatch", "memrl_rejected", "cooldown",
        "sizer_zero", "governance_blocked", "liquidity_thin", "noise",
    }
    tag = parsed.get("miss_tag")
    if tag not in _VALID_MISS_TAGS:
        try:
            import redis_client as _rc
            _rc.get().incr("decoders:f9_miss_tag_invalid_count")
        except Exception:
            pass
        parsed["miss_tag"] = None
        parsed["miss_tag_confidence"] = None
        parsed["miss_tag_evidence"] = None
    else:
        try:
            conf = float(parsed.get("miss_tag_confidence"))
            conf = max(0.0, min(1.0, conf))
        except Exception:
            conf = 0.5
        parsed["miss_tag_confidence"] = conf
        ev = parsed.get("miss_tag_evidence")
        if not isinstance(ev, dict):
            parsed["miss_tag_evidence"] = {}
    # cont. 63 (2026-05-29) — predicted_peak_profit_pct validation. Clamp to
    # the same range as the DB CHECK constraint. If LLM omits or returns a
    # non-numeric, fall back to the absolute value of the realised
    # peak_profit_pct from the counterfactual itself — this guarantees the
    # actuator always has a non-NULL number to scale magnitude with, even
    # when the LLM ignores the new prompt instruction.
    _pp_raw = parsed.get("predicted_peak_profit_pct")
    try:
        _pp = float(_pp_raw)
        if _pp != _pp:  # nan
            raise ValueError("nan")
        _pp = max(-200.0, min(200.0, _pp))
        parsed["predicted_peak_profit_pct"] = round(_pp, 4)
    except (TypeError, ValueError):
        try:
            _fallback = float(cf.get("peak_profit_pct") or 0.0)
            _fallback = max(-200.0, min(200.0, abs(_fallback)))
            parsed["predicted_peak_profit_pct"] = round(_fallback, 4)
            try:
                import redis_client as _rc
                _rc.get().incr("decoders:f9_predicted_peak_fallback_count")
            except Exception:
                pass
        except (TypeError, ValueError):
            parsed["predicted_peak_profit_pct"] = None
    return parsed


def decode_mismatch(loser: dict, winner: dict) -> dict | None:
    """F12: LLM-decode a high-pot-loser / low-pot-winner pair. Returns parsed JSON or None."""
    try:
        from llm.guard import assert_no_reflection
        from llm.researcher import research
    except Exception as exc:
        log.warning("decode_mismatch_import_failed", error=str(exc)[:120])
        return None

    prompt = _mismatch_prompt(loser, winner)
    assert_no_reflection(prompt)
    try:
        raw = research(prompt, max_new_tokens=256)
    except Exception as exc:
        log.warning("decode_mismatch_llm_failed",
                    loser=loser.get("pair"), winner=winner.get("pair"),
                    error=str(exc)[:200])
        return None
    parsed = _strict_json_parse(raw)
    if not parsed or not parsed.get("decode_reason"):
        log.info("decode_mismatch_no_payload",
                 loser=loser.get("pair"), winner=winner.get("pair"),
                 raw_preview=raw[:120] if raw else "")
        return None
    # Cont. 41: mirror the F9 mandatory-scorer_change fix.
    if not isinstance(parsed.get("scorer_change"), dict):
        try:
            import redis_client as _rc
            _rc.get().incr("decoders:f12_synthetic_no_change_count")
        except Exception:
            pass
        parsed["scorer_change"] = {
            "action": "no_change",
            "magnitude": "small",
            "rationale": "llm_omitted_scorer_change_synthesised",
        }
    return parsed
