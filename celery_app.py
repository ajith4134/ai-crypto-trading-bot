"""Celery application — background task queue for AirLLM and scheduled tasks."""
import os
from celery import Celery
from celery.schedules import crontab

app = Celery(
    "trading_bot",
    broker=f"redis://{os.getenv('REDIS_HOST','localhost')}:{os.getenv('REDIS_PORT','6379')}/0",
    backend=f"redis://{os.getenv('REDIS_HOST','localhost')}:{os.getenv('REDIS_PORT','6379')}/1",
)

app.conf.task_routes = {
    "celery_app.research_*": {"queue": "airllm"},
    "celery_app.opro_*": {"queue": "airllm"},
    "celery_app.sleep_*": {"queue": "airllm"},
    "celery_app.*": {"queue": "default"},
}

app.conf.beat_schedule = {
    "sleep-consolidation": {
        "task": "celery_app.sleep_consolidation",
        "schedule": crontab(hour=3, minute=0),
    },
    "daily-summary": {
        "task": "celery_app.send_daily_summary_task",
        "schedule": crontab(hour=8, minute=0),
    },
    "weekly-report": {
        "task": "celery_app.send_weekly_report_task",
        "schedule": crontab(hour=8, minute=0, day_of_week=1),
    },
}


@app.task(bind=True, max_retries=3, default_retry_delay=300, queue="airllm")
def research_strategy(self, hypothesis: str) -> str:
    """AirLLM background task: generate strategy hypothesis."""
    try:
        from llm.researcher import research
        return research(hypothesis, max_new_tokens=1024)
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=300, queue="airllm")
def opro_optimize(self, prompt: str, window_scores: list) -> str:
    """AirLLM background task: OPRO prompt optimization."""
    try:
        from llm.researcher import research
        full_prompt = (
            f"Previous window ROI scores: {window_scores}. "
            f"Current Decision LLM prompt: {prompt}. "
            "Identify the weakest reasoning step and propose a specific improvement. "
            "Output as JSON with keys: weak_step, improvement, new_prompt_section."
        )
        return research(full_prompt, max_new_tokens=512)
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=300, queue="airllm")
def sleep_consolidation(self) -> str:
    """AirLLM background task: nightly memory consolidation."""
    try:
        from llm.researcher import research
        from analytics.metrics import update_all_metrics
        update_all_metrics()
        summary_prompt = (
            "Summarise the key trading patterns and lessons from today's closed trades. "
            "Focus on what conditions led to wins vs losses. "
            "Output as JSON with keys: key_patterns, regime_insights, suggested_improvements."
        )
        return research(summary_prompt, max_new_tokens=1024)
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task(queue="default")
def track_counterfactual(signal_id: str, pair: str) -> None:
    """T-04: Track a rejected signal 72h after rejection."""
    import redis_client
    import redis_keys
    from memory.write import write_counterfactual
    from datetime import datetime, timezone, timedelta

    r = redis_client.get()
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)

    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT generated_at FROM signals WHERE id = %s", (signal_id,))
            row = cur.fetchone()
    if not row:
        return

    signal_time = row[0]
    now = datetime.now(timezone.utc)
    would_have_won = mark > 0

    write_counterfactual(signal_id, {
        "tracking_window_start": signal_time,
        "tracking_window_end": now,
        "would_have_won": would_have_won,
    })

    r_data = r.get(redis_keys.SHADOW_WIN_RATE)
    import json
    stats = json.loads(r_data) if r_data else {"total": 0, "won": 0}
    stats["total"] += 1
    if would_have_won:
        stats["won"] += 1
    stats["rate"] = round(stats["won"] / stats["total"] * 100, 2)
    r.set(redis_keys.SHADOW_WIN_RATE, json.dumps(stats))


@app.task(queue="default")
def send_daily_summary_task() -> None:
    from analytics.metrics import compute_rolling_metrics
    from notifications.telegram import send_daily_summary
    import redis_client, redis_keys
    r = redis_client.get()
    metrics = compute_rolling_metrics(100)
    send_daily_summary({
        "trades_opened": 0,
        "trades_closed": metrics.get("trade_count", 0),
        "daily_pnl": metrics.get("net_pnl_usdt", 0),
        "win_rate": metrics.get("win_rate", 0),
        "brain_stage": int(r.get(redis_keys.BRAIN_STAGE) or 1),
        "top_pair": "N/A",
    })


@app.task(queue="default")
def send_weekly_report_task() -> None:
    from analytics.metrics import compute_rolling_metrics
    from notifications.telegram import send_weekly_report
    import redis_client, redis_keys
    r = redis_client.get()
    metrics = compute_rolling_metrics(500)
    send_weekly_report({
        "sharpe": metrics.get("sharpe", 0),
        "brain_stage": int(r.get(redis_keys.BRAIN_STAGE) or 1),
    })
