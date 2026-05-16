"""S-04 to S-10: Strategy lifecycle management."""
import shutil
from datetime import datetime, timezone
from pathlib import Path
import structlog
from strategy.save import save_strategy, _STRATEGY_DIRS
from db import db_conn

log = structlog.get_logger()


def promote_to_active(strategy_id: str) -> None:
    """S-04: Move .py from experimental/ to active/; update DB status."""
    _move_strategy(strategy_id, "active")


def retire_strategy(strategy_id: str, reason: str) -> None:
    """S-05: Move .py to retired/; update DB; never delete the file."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE strategies SET retirement_reason = %s, retired_at = %s WHERE id = %s",
                (reason, datetime.now(timezone.utc), strategy_id),
            )
    _move_strategy(strategy_id, "retired")
    log.info("strategy_retired", id=strategy_id, reason=reason)


def create_experimental(strategy: dict) -> str:
    """S-06: Save new strategy with status 'experimental'; paper trial begins."""
    strategy["status"] = "experimental"
    sid = save_strategy(strategy)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE strategies SET created_at = NOW() WHERE id = %s", (sid,)
            )
    log.info("experimental_strategy_created", id=sid)
    return sid


def check_trial_eligible(strategy_id: str) -> tuple[bool, str]:
    """S-07: Verify ≥30 paper trades AND ≥7 days since trial start."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT paper_trade_count, created_at FROM strategies WHERE id = %s",
                (strategy_id,),
            )
            row = cur.fetchone()
    if not row:
        return False, "strategy not found"

    trade_count, created_at = row
    if trade_count < 30:
        return False, f"only {trade_count} paper trades (need 30)"

    days_elapsed = (datetime.now(timezone.utc) - created_at).days
    if days_elapsed < 7:
        return False, f"only {days_elapsed} days elapsed (need 7)"

    return True, "eligible"


def auto_retire_if_underperforming(strategy_id: str, threshold_win_rate: float) -> None:
    """S-10: After every 50 closed trades on any strategy, evaluate performance."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT win_rate, trade_count FROM strategies WHERE id = %s", (strategy_id,)
            )
            row = cur.fetchone()
    if not row:
        return
    win_rate, trade_count = float(row[0] or 0), int(row[1] or 0)
    if trade_count >= 50 and win_rate < threshold_win_rate:
        retire_strategy(strategy_id, reason=f"win_rate_{win_rate:.1f}_below_threshold_{threshold_win_rate}")


def _move_strategy(strategy_id: str, new_status: str) -> None:
    """Move .py file between status directories; update DB in same transaction."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_path FROM strategies WHERE id = %s", (strategy_id,)
            )
            row = cur.fetchone()
            if not row or not row[0]:
                raise ValueError(f"No file_path found for strategy {strategy_id}")
            old_path = Path(row[0])
            new_dir = _STRATEGY_DIRS[new_status]
            new_path = new_dir / old_path.name

            new_dir.mkdir(parents=True, exist_ok=True)
            if old_path.exists():
                shutil.move(str(old_path), str(new_path))

            cur.execute(
                "UPDATE strategies SET status = %s, file_path = %s, updated_at = NOW() WHERE id = %s",
                (new_status, str(new_path), strategy_id),
            )
    log.info("strategy_moved", id=strategy_id, status=new_status)
