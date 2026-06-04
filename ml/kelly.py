"""
L-09: Fractional Kelly position sizing.
- Before 50 trades: flat 5% allocation
- 50–99 trades: rough Kelly from early history
- 100+ trades: full Kelly, recalibrated every 50 trades
"""
import structlog
import config

log = structlog.get_logger()

_FRACTION = 0.25   # fractional Kelly multiplier


def compute_kelly(win_rate_pct: float, avg_win: float, avg_loss: float) -> float:
    """f* = W - (1-W)/R, then apply fractional Kelly."""
    if avg_loss == 0 or avg_win == 0:
        return config.capital.per_trade_min_pct
    W = win_rate_pct / 100
    R = avg_win / abs(avg_loss)
    f_star = W - (1 - W) / R
    fractional = f_star * _FRACTION * 100   # as % of capital
    return _clamp(fractional)


def _clamp(pct: float) -> float:
    return max(config.capital.per_trade_min_pct, min(config.capital.per_trade_max_pct, pct))


def _mark_called(result_pct: float) -> None:
    """Durable evidence for feature_health: each Kelly call bumps a counter
    and writes the latest sizing decision. Without this, the only evidence
    F16 fires was the brain-process log line `kelly_sizing` — unreadable from
    a container-internal checker."""
    try:
        import time as _t
        import redis_client as _rc
        r = _rc.get()
        r.incr("kelly:call_count")
        r.set("kelly:last_call_ts", str(int(_t.time())))
        r.set("kelly:last_pct", str(result_pct))
    except Exception:
        pass


def get_position_size_pct(paper_closed_count: int) -> float:
    """Return capital % based on trade count phase."""
    if paper_closed_count < 50:
        out = float(config.capital.per_trade_min_pct)
        _mark_called(out)
        return out

    from memory.query import get_recent_trades
    trades = get_recent_trades(min(paper_closed_count, 200))
    if not trades:
        out = float(config.capital.per_trade_min_pct)
        _mark_called(out)
        return out

    wins = [t for t in trades if (t.get("net_pnl_usdt") or 0) > 0]
    losses = [t for t in trades if (t.get("net_pnl_usdt") or 0) <= 0]
    if not wins or not losses:
        out = float(config.capital.per_trade_min_pct)
        _mark_called(out)
        return out

    win_rate = len(wins) / len(trades) * 100
    avg_win = sum(float(t["net_pnl_usdt"]) for t in wins) / len(wins)
    avg_loss = sum(abs(float(t["net_pnl_usdt"])) for t in losses) / len(losses)

    out = compute_kelly(win_rate, avg_win, avg_loss)
    _mark_called(out)
    return out
