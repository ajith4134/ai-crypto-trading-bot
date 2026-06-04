"""S-02/S-03: Strategy dual-save — DB row + .py file in a single transaction."""
import os
import uuid
from pathlib import Path
import structlog
from db import db_conn

log = structlog.get_logger()

# Relative to container WORKDIR (/app); host /opt/trading-bot/strategies is
# bind-mounted in via docker-compose so writes show up on the host too.
_STRATEGY_DIRS = {
    "active":       Path("strategies/active"),
    "experimental": Path("strategies/experimental"),
    "retired":      Path("strategies/retired"),
}


_REQUIRED_ROUTER_FIELDS = (
    "dca_rules",
    "entry_overrides",
    "position_sizing_rules",
    "trailing_sl_params",
)


def _ensure_router_fields(strategy: dict) -> dict:
    """Fill any missing F8 router field with config-derived defaults so the
    saved strategy is always complete. cont. 23: prevents the silent-NULL
    failure mode where new strategies got attribution but no router behaviour
    (bot fell back to global config; per-strategy bandit was effectively
    dead for those rows)."""
    import config as _cfg
    out = dict(strategy)
    if not out.get("dca_rules"):
        out["dca_rules"] = {
            "round_1_pct": float(_cfg.capital.dca_trigger_1_pct),
            "round_2_pct": float(_cfg.capital.dca_trigger_2_pct),
        }
    if not out.get("entry_overrides"):
        out["entry_overrides"] = {
            "min_signal_strength": 25,
            "regime_whitelist": ["bull", "bear"],
        }
    if not out.get("position_sizing_rules"):
        out["position_sizing_rules"] = {"capital_pct_mult": 1.0}
    if not out.get("trailing_sl_params"):
        out["trailing_sl_params"] = {
            "initial_atr_mult": 2.5,
            "initial_min_pct": 0.03,
            "trailing_dist_pct": 0.02,
        }
    return out


def save_strategy(strategy: dict) -> str:
    """
    S-02: Wrap DB INSERT + filesystem write in a single PostgreSQL transaction.
    If either fails, both are rolled back — DB and filesystem never diverge.
    """
    status = strategy.get("status", "experimental")
    strategy_id = strategy.get("id") or str(uuid.uuid4())
    directory = _STRATEGY_DIRS[status]
    file_path = directory / f"strategy_{strategy_id[:8]}.py"
    strategy = _ensure_router_fields(strategy)

    import json as _json
    def _as_jsonb(v):
        return _json.dumps(v) if isinstance(v, (dict, list)) else v

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO strategies (
                    id, name, status, source, code, file_path,
                    entry_conditions, exit_conditions, position_sizing_rules,
                    dca_rules, trailing_sl_params, generation, parent_strategy_id,
                    entry_overrides
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    code = EXCLUDED.code,
                    file_path = EXCLUDED.file_path,
                    position_sizing_rules = COALESCE(strategies.position_sizing_rules, EXCLUDED.position_sizing_rules),
                    dca_rules             = COALESCE(strategies.dca_rules,             EXCLUDED.dca_rules),
                    trailing_sl_params    = COALESCE(strategies.trailing_sl_params,    EXCLUDED.trailing_sl_params),
                    entry_overrides       = COALESCE(strategies.entry_overrides,       EXCLUDED.entry_overrides),
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
                _as_jsonb(strategy.get("position_sizing_rules")),
                _as_jsonb(strategy.get("dca_rules")),
                _as_jsonb(strategy.get("trailing_sl_params")),
                strategy.get("generation", 0),
                strategy.get("parent_strategy_id"),
                _as_jsonb(strategy.get("entry_overrides")),
            ))

        try:
            directory.mkdir(parents=True, exist_ok=True)
            file_path.write_text(strategy.get("code", ""))
        except OSError as exc:
            raise RuntimeError(f"Filesystem write failed for {file_path}: {exc}") from exc

    log.info("strategy_saved", id=strategy_id, status=status, path=str(file_path))
    return strategy_id
