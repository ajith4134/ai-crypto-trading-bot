"""
Section AB: Self-Improvement Loop.
AB-01 to AB-04: OPRO Prompt Optimization (from 5 trades).
AB-05 to AB-09: DGM-Style Code Rewriting (from 500 trades, weekly).
AB-10: AI Scientist Hypothesis Generation (from 300 trades, continuous).
"""
import json
import structlog
import redis_keys
import redis_client
from db import db_conn

log = structlog.get_logger()


# --- AB-01 to AB-04: OPRO ---

def increment_opro_counter() -> int:
    """AB-01: Increment window counter after each closed trade."""
    r = redis_client.get()
    return int(r.incr(redis_keys.OPRO_WINDOW_COUNTER))


def compute_opro_score(net_pnl_list: list[float]) -> float:
    """AB-02: ROI score = max(0, min(100, 50 + 250 × avg_roi))."""
    if not net_pnl_list:
        return 50.0
    avg_roi = sum(net_pnl_list) / len(net_pnl_list) / 100
    return max(0.0, min(100.0, 50 + 250 * avg_roi))


def trigger_opro_if_due(trade_count: int, window_size: int = 5) -> bool:
    """Return True if we should run an OPRO round now."""
    return trade_count > 0 and trade_count % window_size == 0


def get_current_prompt() -> str:
    """Read current Decision LLM prompt from brain_state."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_executor_prompt FROM brain_state WHERE id=1")
            row = cur.fetchone()
            if row and row[0]:
                return json.dumps(row[0])
    return ""


def save_new_prompt(new_prompt_json: str) -> None:
    """AB-03: Write candidate prompt to brain_state AND publish addendum to Redis so brain consumes it."""
    import json as _json
    try:
        parsed = _json.loads(new_prompt_json)
    except Exception:
        parsed = {"raw": new_prompt_json}

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE brain_state SET current_executor_prompt = %s, updated_at = NOW() WHERE id=1",
                (_json.dumps(parsed),),
            )

    r = redis_client.get()
    # brain/soar.py _decide() reads this every cycle and splices it into the Ollama prompt.
    # Cap length so a runaway LLM output can't poison every decision.
    addendum = (parsed.get("new_prompt_section") or "").strip()[:500] if isinstance(parsed, dict) else ""
    if addendum:
        r.set("brain:executor_prompt_addendum", addendum)
    version = int(r.incr("brain:opro_prompt_version"))
    log.info("opro_prompt_updated", version=version, addendum_chars=len(addendum))


def revert_prompt(previous_prompt_json: str) -> None:
    """AB-04: Revert if new prompt performed worse."""
    save_new_prompt(previous_prompt_json)
    log.info("opro_prompt_reverted")


# --- AB-05 to AB-09: DGM Code Rewriting ---

def get_weakest_strategies(n: int = 3) -> list[dict]:
    """AB-05: Find n strategies with worst rolling Sharpe over last 100 trades."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.name, s.file_path, s.sharpe_ratio
                FROM strategies s
                WHERE s.status = 'active'
                ORDER BY s.sharpe_ratio ASC NULLS LAST
                LIMIT %s
            """, (n,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def submit_rewrite_task(strategy_id: str, file_path: str) -> str:
    """AB-06: Submit DGM code rewrite as AirLLM Celery task."""
    try:
        code = open(file_path).read()
    except Exception:
        code = "# file not found"

    from celery_app import app
    task = app.send_task(
        "celery_app.research_strategy",
        args=[f"Rewrite and improve this trading strategy:\n\n{code}\n\nMake it more profitable while keeping the same structure."],
        queue="airllm",
    )
    log.info("dgm_rewrite_submitted", strategy_id=strategy_id, task_id=task.id)
    return task.id


# --- AB-10: AI Scientist ---

def submit_ai_scientist_task(trade_summary: str, competence_gaps: list[str]) -> str:
    """AB-10: Continuous hypothesis generation via AirLLM."""
    from celery_app import app
    prompt = (
        f"Analyse these trade outcomes: {trade_summary}. "
        f"Known competence gaps: {competence_gaps}. "
        "Generate 3 new testable trading hypotheses targeting these gaps. "
        "Output as JSON list: [{hypothesis, testable_condition, expected_outcome}]"
    )
    task = app.send_task("celery_app.research_strategy", args=[prompt], queue="airllm")
    return task.id
