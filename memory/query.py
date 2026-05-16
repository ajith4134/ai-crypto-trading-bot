"""M-07: All query methods used by other modules."""
import json
from typing import Optional
import structlog
from db import db_conn

log = structlog.get_logger()


def _fetchall(sql: str, params: tuple = ()) -> list[dict]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetchone(sql: str, params: tuple = ()) -> Optional[dict]:
    rows = _fetchall(sql, params)
    return rows[0] if rows else None


def get_trades_by_regime(regime: str) -> list[dict]:
    return _fetchall("SELECT * FROM trades WHERE market_regime = %s AND status = 'closed'", (regime,))


def get_trades_by_pair(pair: str) -> list[dict]:
    return _fetchall("SELECT * FROM trades WHERE pair = %s AND status = 'closed'", (pair,))


def get_trades_by_strategy(strategy_id: str) -> list[dict]:
    return _fetchall("SELECT * FROM trades WHERE strategy_id = %s AND status = 'closed'", (strategy_id,))


def get_trades_by_failure_type(failure_type: str) -> list[dict]:
    return _fetchall("SELECT * FROM trades WHERE failure_type = %s", (failure_type,))


def get_recent_trades(n: int) -> list[dict]:
    return _fetchall("SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT %s", (n,))


def get_similar_trades(embedding_list: list[float], top_k: int = 10) -> list[dict]:
    """pgvector cosine similarity search."""
    vec = json.dumps(embedding_list)
    return _fetchall(
        f"SELECT *, 1 - (embedding <=> %s::vector) AS similarity "
        f"FROM trades WHERE embedding IS NOT NULL "
        f"ORDER BY embedding <=> %s::vector LIMIT %s",
        (vec, vec, top_k),
    )


def get_win_rate(filters: dict = None) -> float:
    where = "WHERE status = 'closed'"
    params = []
    if filters:
        for k, v in filters.items():
            where += f" AND {k} = %s"
            params.append(v)
    row = _fetchone(
        f"SELECT COUNT(*) FILTER (WHERE net_pnl_usdt > 0) AS wins, COUNT(*) AS total FROM trades {where}",
        tuple(params),
    )
    if not row or not row["total"]:
        return 0.0
    return round(row["wins"] / row["total"] * 100, 2)


def get_sharpe(filters: dict = None) -> float:
    where = "WHERE status = 'closed' AND exit_time IS NOT NULL"
    params = []
    if filters:
        for k, v in filters.items():
            where += f" AND {k} = %s"
            params.append(v)
    rows = _fetchall(
        f"SELECT net_pnl_usdt, DATE(exit_time) AS exit_date FROM trades {where}",
        tuple(params),
    )
    if len(rows) < 2:
        return 0.0
    import statistics
    daily: dict = {}
    for r in rows:
        d = str(r["exit_date"])
        daily[d] = daily.get(d, 0) + float(r["net_pnl_usdt"] or 0)
    returns = list(daily.values())
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    std = statistics.stdev(returns)
    return round((mean / std * (252 ** 0.5)) if std else 0.0, 4)


def get_rolling_win_rate(window_n: int) -> float:
    rows = _fetchall(
        "SELECT net_pnl_usdt FROM trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT %s",
        (window_n,),
    )
    if not rows:
        return 0.0
    wins = sum(1 for r in rows if (r["net_pnl_usdt"] or 0) > 0)
    return round(wins / len(rows) * 100, 2)


def get_rolling_sharpe(window_n: int) -> float:
    rows = _fetchall(
        "SELECT net_pnl_usdt, DATE(exit_time) AS exit_date FROM trades "
        "WHERE status = 'closed' ORDER BY exit_time DESC LIMIT %s",
        (window_n,),
    )
    if len(rows) < 2:
        return 0.0
    import statistics
    daily: dict = {}
    for r in rows:
        d = str(r["exit_date"])
        daily[d] = daily.get(d, 0) + float(r["net_pnl_usdt"] or 0)
    returns = list(daily.values())
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    std = statistics.stdev(returns)
    return round((mean / std * (252 ** 0.5)) if std else 0.0, 4)


def get_open_trades() -> list[dict]:
    return _fetchall("SELECT * FROM trades WHERE status = 'open'")


def get_paper_closed_count() -> int:
    row = _fetchone("SELECT COUNT(*) AS n FROM trades WHERE status = 'closed' AND is_paper = TRUE")
    return row["n"] if row else 0


def get_live_closed_count() -> int:
    row = _fetchone("SELECT COUNT(*) AS n FROM trades WHERE status = 'closed' AND is_paper = FALSE")
    return row["n"] if row else 0
