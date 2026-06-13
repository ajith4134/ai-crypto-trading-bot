"""
Section AJ: Entry point & orchestrator — AJ-01 to AJ-03.
"""
import asyncio
import signal
import sys
from pathlib import Path
import structlog

log = structlog.get_logger()


_STAGE_STRATEGY_NAMES = {
    1: "stage1_ofi_momentum",
    2: "stage2_sentiment_ofi",
}


def _seed_strategy_uuid_keys() -> None:
    """Mirror each stage's active strategy UUID into Redis at
    `bot:strategy_uuid:{stage}`. Runs every startup, idempotent — overwrites
    with whatever postgres says is current. If a stage's strategy row is
    missing, log and skip that stage rather than crash the bot.
    """
    from db import db_conn
    import redis_client

    r = redis_client.get()
    names = list(_STAGE_STRATEGY_NAMES.values())
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, id FROM strategies "
                    "WHERE name = ANY(%s) AND status = 'active'",
                    (names,),
                )
                found = {name: str(sid) for name, sid in cur.fetchall()}
    except Exception as exc:
        log.error("strategy_uuid_seed_db_failed", error=str(exc))
        return

    for stage, name in _STAGE_STRATEGY_NAMES.items():
        uuid_str = found.get(name)
        key = f"bot:strategy_uuid:{stage}"
        if uuid_str:
            r.set(key, uuid_str)
            log.info("strategy_uuid_seeded", stage=stage, name=name, uuid=uuid_str)
        else:
            log.warning(
                "strategy_uuid_missing_active_row",
                stage=stage,
                name=name,
                note="trades for this stage will be inserted with strategy_id=NULL",
            )


def _startup_checks() -> None:
    """AJ-01: Validate all dependencies before starting any service."""
    import db
    import redis_client
    import config

    # DB
    db.init_pool()
    log.info("postgres_ok")

    # Redis
    redis_client.init()
    log.info("redis_ok")

    # Ollama connectivity
    import httpx
    try:
        r = httpx.get(f"{config.llm.ollama_url}/api/tags", timeout=5)
        r.raise_for_status()
        log.info("ollama_ok")
    except Exception as exc:
        log.error("ollama_unreachable", error=str(exc))
        sys.exit(1)

    # ML model checkpoints
    required_models = [
        Path("models/hmm_regime.pkl"),
        Path("models/tft.pth"),
        Path("models/patchtst.pth"),
        Path("models/gnn.pth"),
        Path("models/world_model.pth"),
    ]
    missing = [str(p) for p in required_models if not p.exists()]
    if missing:
        log.error("missing_ml_models", missing=missing)
        sys.exit(1)

    # Cloud Llama 3.3 70B provider chain — warning if zero keys configured.
    # See llm/researcher.py for the Groq → Cerebras → SambaNova chain.
    _providers = [
        ("GROQ_API_KEY", config.GROQ_API_KEY),
        ("CEREBRAS_API_KEY", config.CEREBRAS_API_KEY),
        ("SAMBANOVA_API_KEY", config.SAMBANOVA_API_KEY),
    ]
    _configured = [name for name, val in _providers if val]
    if not _configured:
        log.warning("no_cloud_llm_provider_configured",
                    note="Background research tasks disabled but trading continues")
    else:
        log.info("cloud_llm_providers_ok", providers=_configured)

    # Blueprint F30: Feature Governance — register every feature at startup
    try:
        from feature_governance.bootstrap import bootstrap_all_features
        bootstrap_all_features()
    except Exception as exc:
        log.warning("feature_governance_bootstrap_skipped", error=str(exc))

    # Seed bot:strategy_uuid:{1,2} from the strategies table so trade INSERTs
    # always carry a valid FK. Without this, soar.py reads None from Redis and
    # every trade lands with strategy_id=NULL (see PROGRESS.md Bug 22 — the
    # original seed was a one-off and did not survive Redis being cleared).
    _seed_strategy_uuid_keys()

    # Virtual balance is set by the user via dashboard before pressing Start Trading.
    # It is NOT hardcoded here. POST /bot/start initialises it from bot:starting_capital_usdt.
    log.info("all_startup_checks_passed")


async def _main() -> None:
    """AJ-02: Launch all services."""
    _startup_checks()

    from brain.soar import MasterBrain
    from risk.manager import monitor_trailing_sl
    brain = MasterBrain()
    engine = brain.engine

    # AJ-03: Graceful shutdown
    loop = asyncio.get_running_loop()

    def _shutdown(sig_name: str) -> None:
        log.info("shutdown_requested", signal=sig_name)
        brain.stop()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig.name: _shutdown(s))

    # brain + trailing SL monitor run together
    # scanner runs in scanner container, data_feed in data_feed container
    # F49 §Component 7 — Online learner subscribes to CH_TRADE_CLOSED and
    # incrementally updates Direction Model + World Model on every close.
    # Falls back silently if F49 is deactivated.
    from ml.online_learner import start_online_learner
    # cont. 69: pending limit-entry filler (execution.limit_entry). Default OFF
    # via Redis; the loop is cheap when there are no pending entries.
    from execution.limit_entry import process_pending_entries
    await asyncio.gather(
        brain.run(),
        monitor_trailing_sl(engine),
        start_online_learner(),
        process_pending_entries(engine),
        return_exceptions=True,
    )

    log.info("shutdown_complete")


if __name__ == "__main__":
    asyncio.run(_main())
