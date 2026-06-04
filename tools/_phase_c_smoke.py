"""Phase C smoke test — verify save_debate_arguments + update_beliefs_on_close
work end-to-end against the real DB without needing an LLM call.

Run inside the brain container:
    docker exec trading-bot-brain-1 python -m tools._phase_c_smoke
"""
import uuid
import sys
from datetime import datetime, timezone

from db import db_conn
from debate.council import (
    save_debate_arguments, update_beliefs_on_close, get_agent_weights,
)


def main() -> int:
    # Pick a recent closed trade as the real trade_id + capital target.
    with db_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, capital_usdt, net_pnl_usdt, pair "
                "FROM trades WHERE status='closed' AND is_paper=true "
                "  AND net_pnl_usdt IS NOT NULL "
                "ORDER BY exit_time DESC LIMIT 1"
            )
            row = cur.fetchone()
    if not row:
        print("FAIL: no closed paper trade available for smoke test")
        return 1
    trade_id = str(row[0])
    capital = float(row[1] or 0)
    net_pnl = float(row[2])
    pair = row[3]
    won = net_pnl > 0
    print(f"Smoke test trade: {trade_id[:8]} pair={pair} pnl={net_pnl:.2f} "
          f"capital={capital:.2f} won={won}")

    # Synthetic signal row tied to that trade.
    sig_id = str(uuid.uuid4())
    with db_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO signals (id, generated_at, pair, direction, accepted, "
                "brain_stage, trade_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (sig_id, datetime.now(timezone.utc), pair, "long", True, 3, trade_id),
            )

    debate_log = {
        "verdict": "full_allocation",
        "size_pct": 5.0,
        "rounds_used": 3,
        "rounds": {
            1: {
                "bull": {"argue_for": True,
                         "arguments": ["momentum", "regime supports"],
                         "confidence": 75},
                "bear": {"argue_against": False,
                         "arguments": ["nothing major"],
                         "risk_score": 30},
                "risk": {"risk_acceptable": True, "concerns": [],
                         "recommended_size_pct": 5},
            },
            2: {
                "bull": {"argue_for": True, "rebuttals": ["addressed"],
                         "conceded": [], "confidence": 80},
                "bear": {"argue_against": True, "rebuttals": ["caveats"],
                         "conceded": [], "risk_score": 60},
                "risk": {"risk_acceptable": True, "concerns": [],
                         "recommended_size_pct": 5},
            },
            3: {
                "bull": {"argue_for": True, "key_evidence": "strong volume",
                         "confidence": 85},
                "bear": {"argue_against": True, "key_evidence": "weak fund.",
                         "risk_score": 70},
            },
        },
        "weights": {"bull": 1.0, "bear": 1.0, "risk": 1.0},
        "llm_available": True,
    }

    n = save_debate_arguments(sig_id, trade_id, debate_log)
    print(f"save_debate_arguments inserted: {n}")

    with db_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT agent_role, round_num, score FROM debate_arguments "
                "WHERE signal_id=%s ORDER BY round_num, agent_role", (sig_id,))
            persisted = cur.fetchall()
    for r in persisted:
        print(f"  persisted: agent={r[0]} round={r[1]} score={r[2]}")
    assert len(persisted) == 8, f"expected 8 rows (3+3+2), got {len(persisted)}"

    print(f"weights before: {get_agent_weights()}")
    res = update_beliefs_on_close(trade_id, won, net_pnl, capital)
    print(f"verbal reinforcement: status={res.get('status')} "
          f"role_correct={res.get('role_correct')} "
          f"rounds_updated={res.get('rounds_updated')}")
    weights_after = get_agent_weights()
    print(f"weights after:  {weights_after}")

    with db_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT agent_role, round_num, was_correct FROM debate_arguments "
                "WHERE signal_id=%s ORDER BY round_num, agent_role", (sig_id,))
            corrected = cur.fetchall()
    for r in corrected:
        print(f"  was_correct: agent={r[0]} round={r[1]} correct={r[2]}")

    # Cleanup synthetic data — must NOT pollute real history.
    with db_conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM debate_arguments WHERE signal_id=%s", (sig_id,))
            cur.execute("DELETE FROM signals WHERE id=%s", (sig_id,))
    # Reset weights to baseline so smoke test doesn't pollute live weight state.
    import redis_client
    rcli = redis_client.get()
    for role in ("bull", "bear", "risk"):
        rcli.delete(f"debate:agent_weight:{role}")
    print("smoke test cleaned up (signals, debate_arguments, weight keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
