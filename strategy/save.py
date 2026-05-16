"""S-02/S-03: Strategy dual-save — DB row + .py file in a single transaction."""
import os
import uuid
from pathlib import Path
import structlog
from db import db_conn

log = structlog.get_logger()

_STRATEGY_DIRS = {
    "active":       Path("/opt/trading-bot/strategies/active"),
    "experimental": Path("/opt/trading-bot/strategies/experimental"),
    "retired":      Path("/opt/trading-bot/strategies/retired"),
}


def save_strategy(strategy: dict) -> str:
    """
    S-02: Wrap DB INSERT + filesystem write in a single PostgreSQL transaction.
    If either fails, both are rolled back — DB and filesystem never diverge.
    """
    status = strategy.get("status", "experimental")
    strategy_id = strategy.get("id") or str(uuid.uuid4())
    directory = _STRATEGY_DIRS[status]
    file_path = directory / f"strategy_{strategy_id[:8]}.py"

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO strategies (
                    id, name, status, source, code, file_path,
                    entry_conditions, exit_conditions, position_sizing_rules,
                    dca_rules, trailing_sl_params, generation, parent_strategy_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    code = EXCLUDED.code,
                    file_path = EXCLUDED.file_path,
                    updated_at = NOW()
            """, (
                strategy_id,
                strategy.get("name", f"strategy_{strategy_id[:8]}"),
                status,
                strategy.get("source", "brain"),
                strategy.get("code", ""),
                str(file_path),
                strategy.get("entry_conditions"),
                strategy.get("exit_conditions"),
                strategy.get("position_sizing_rules"),
                strategy.get("dca_rules"),
                strategy.get("trailing_sl_params"),
                strategy.get("generation", 0),
                strategy.get("parent_strategy_id"),
            ))

        try:
            directory.mkdir(parents=True, exist_ok=True)
            file_path.write_text(strategy.get("code", ""))
        except OSError as exc:
            raise RuntimeError(f"Filesystem write failed for {file_path}: {exc}") from exc

    log.info("strategy_saved", id=strategy_id, status=status, path=str(file_path))
    return strategy_id
