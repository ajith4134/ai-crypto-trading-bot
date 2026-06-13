"""
Debate realized-outcome learning loop — cont. 72 (2026-06-07).

Closes the feedback loop the cont.69 design left as a TODO. The deterministic
scorer (debate.fallback) reads `debate:prior:regime:{regime}:{dir}` as its PRIMARY
term but nothing ever WROTE it (the planned async LLM debate was never implemented,
and phi3 ~4 tok/s made it impractical). This module writes that prior directly from
REALIZED trade outcomes — no LLM, no hot-path cost. It is called once per closed
trade from memory.write.write_trade_close.

What it learns (ground-truth motivation, cont.72 audit):
  realized edge by regime:dir over closed trades — bear:short was 54% WR but -$0.84
  avg (small wins, huge losses); bull:long +0.59; turbulent:short +1.08; unknown:long
  -3.11. A win/loss-SIGN prior would rate bear:short positively (54% WR) — WRONG. So
  the prior tracks avg-PnL MAGNITUDE via return-on-capital, not win/loss.

Keys written:
  debate:prior:regime:{regime}:{dir}  -> EWMA(α) of clip(net_pnl/capital, -1, 1) ∈[-1,1]
  debate:recent_outcomes              -> list, most-recent-first '1'(win)/'0'(loss),
                                          capped 20 (loss-streak factor in fallback)
  debate:prior_updates_count          -> monotonic counter (Rule 12 evidence)
  debate:prior_last_ts                -> unix ts of last update
"""
import time

import structlog

import redis_client

log = structlog.get_logger()

_ALPHA = 0.05               # EWMA learning rate (slow — ~last 20 trades dominate)
_RECENT_CAP = 20            # loss-streak window
_PRIOR_KEY = "debate:prior:regime:{regime}:{direction}"

# Per-trade return-on-capital is small (typical ±0.5-1%, regime:dir means span
# ~[-0.02, +0.012]). Scale it up before clipping so the realized edge maps to a
# meaningful prior in [-1,1] (×_W_PRIOR=15 in fallback). At scale 50 a +1% trade →
# +0.5, a -2% trade → -1.0; the regime:dir EWMA means then span roughly ±0.98.
_RETURN_SCALE = 50.0


def _norm_return(net_pnl_usdt: float, capital_usdt: float) -> float:
    """Per-trade scaled return on capital, bounded to [-1, 1]. Capital floored at
    1 to avoid blow-ups on missing/zero capital."""
    cap = abs(capital_usdt) if capital_usdt else 0.0
    if cap < 1.0:
        cap = 1.0
    return max(-1.0, min(1.0, (net_pnl_usdt / cap) * _RETURN_SCALE))


def update_outcome_prior(regime: str, direction: str,
                         net_pnl_usdt: float, capital_usdt: float) -> dict:
    """EWMA-update the realized regime:direction prior + push to the loss-streak
    window. Best-effort: never raises (caller is in the trade-close path).
    Returns a summary dict for logging."""
    regime = (regime or "unknown").lower()
    direction = (direction or "long").lower()
    try:
        r = redis_client.get()
        key = _PRIOR_KEY.format(regime=regime, direction=direction)
        try:
            prev = float(r.get(key)) if r.get(key) is not None else 0.0
        except (TypeError, ValueError):
            prev = 0.0
        target = _norm_return(float(net_pnl_usdt or 0.0), float(capital_usdt or 0.0))
        new = (1.0 - _ALPHA) * prev + _ALPHA * target
        new = max(-1.0, min(1.0, new))
        r.set(key, round(new, 5))

        # Loss-streak window (global, most-recent-first).
        r.lpush("debate:recent_outcomes", "1" if net_pnl_usdt > 0 else "0")
        r.ltrim("debate:recent_outcomes", 0, _RECENT_CAP - 1)

        # Evidence (Rule 12).
        r.incr("debate:prior_updates_count")
        r.set("debate:prior_last_ts", int(time.time()))

        summary = {
            "status": "updated", "regime": regime, "direction": direction,
            "prev": round(prev, 4), "target": round(target, 4), "new": round(new, 4),
        }
        log.info("debate_prior_updated", **summary)
        return summary
    except Exception as exc:
        log.warning("debate_prior_update_failed", error=str(exc)[:160])
        return {"status": "error", "error": str(exc)[:160]}
