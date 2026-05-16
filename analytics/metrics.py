"""
Section AF: Performance Analytics — AF-01 to AF-09.
"""
import json
import statistics
from datetime import datetime, timezone
import structlog
import redis_client
import redis_keys
from db import db_conn

log = structlog.get_logger()


def _fetch_closed(is_paper: bool | None = None, limit: int | None = None) -> list[dict]:
    filters = "WHERE status='closed'"
    params = []
    if is_paper is not None:
        filters += " AND is_paper = %s"
        params.append(is_paper)
    order = " ORDER BY exit_time DESC"
    lim = f" LIMIT {limit}" if limit else ""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM trades {filters}{order}{lim}", params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _daily_returns(trades: list[dict]) -> list[float]:
    daily: dict[str, float] = {}
    for t in trades:
        d = str(t.get("exit_time") or "")[:10]
        if d:
            daily[d] = daily.get(d, 0) + float(t.get("net_pnl_usdt") or 0)
    return list(daily.values())


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    std = statistics.stdev(returns)
    return round((mean / std * (252 ** 0.5)) if std else 0.0, 4)


def _sortino(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    neg = [r for r in returns if r < 0]
    std_neg = statistics.stdev(neg) if len(neg) > 1 else 0
    return round((mean / std_neg * (252 ** 0.5)) if std_neg else 0.0, 4)


def compute_rolling_metrics(window_n: int, is_paper: bool | None = None) -> dict:
    """AF-03: Compute all 9 rolling metrics over the last window_n closed trades."""
    trades = _fetch_closed(is_paper=is_paper, limit=window_n)
    if not trades:
        return {}

    net_pnls = [float(t.get("net_pnl_usdt") or 0) for t in trades]
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    returns = _daily_returns(trades)
    hold_times = [int(t.get("hold_time_seconds") or 0) for t in trades]

    drawdown = 0.0
    peak = 0.0
    cumulative = 0.0
    for r in returns:
        cumulative += r
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)

    return {
        "window_n": window_n,
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 2),
        "net_pnl_usdt": round(sum(net_pnls), 4),
        "sharpe": _sharpe(returns),
        "sortino": _sortino(returns),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else 0.0,
        "max_drawdown_usdt": round(drawdown, 4),
        "avg_hold_hours": round(statistics.mean(hold_times) / 3600, 2) if hold_times else 0,
        "avg_win_loss_ratio": round(
            (sum(wins) / len(wins)) / (abs(sum(losses)) / len(losses)), 4
        ) if wins and losses else 0.0,
        "directional_accuracy": round(
            sum(1 for t in trades if t.get("failure_type") != "direction") / len(trades) * 100, 2
        ),
    }


def update_equity_curve() -> None:
    """AF-01/AF-02: Write equity curve and drawdown point to Redis after each trade."""
    r = redis_client.get()
    balance = float(r.get(redis_keys.VIRTUAL_BALANCE) or r.get(redis_keys.ACCOUNT_BALANCE) or 0)

    peak_key = "brain:equity_peak"
    peak = float(r.get(peak_key) or balance)
    if balance > peak:
        r.set(peak_key, balance)
        peak = balance

    drawdown = round((peak - balance) / peak * 100, 4) if peak > 0 else 0

    ts = datetime.now(timezone.utc).isoformat()
    r.lpush("analytics:equity_curve", json.dumps({"ts": ts, "balance": balance, "drawdown": drawdown}))
    r.ltrim("analytics:equity_curve", 0, 9999)


def update_all_metrics() -> None:
    """AF-03/AF-04: Recompute all metrics and write to Redis."""
    r = redis_client.get()
    for window in [50, 100, 500]:
        for is_paper in [True, False, None]:
            label = "paper" if is_paper is True else "live" if is_paper is False else "all"
            metrics = compute_rolling_metrics(window, is_paper=is_paper)
            r.set(f"analytics:metrics:{label}:{window}", json.dumps(metrics))

    update_equity_curve()
    log.debug("analytics_updated")
