"""
Section AJ: Entry point & orchestrator — AJ-01 to AJ-03.
"""
import asyncio
import signal
import sys
from pathlib import Path
import structlog

log = structlog.get_logger()


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

    # llama.cpp GGUF model — warning only, not fatal (only needed for background tasks)
    llamacpp_path = Path(config.llm.llamacpp_model_path)
    if not llamacpp_path.exists():
        log.warning("llamacpp_model_not_found", path=str(llamacpp_path),
                    note="Background research tasks disabled but trading continues")
    else:
        log.info("llamacpp_model_ok")

    # Virtual balance is set by the user via dashboard before pressing Start Trading.
    # It is NOT hardcoded here. POST /bot/start initialises it from bot:starting_capital_usdt.
    log.info("all_startup_checks_passed")


async def _main() -> None:
    """AJ-02: Launch all services."""
    _startup_checks()

    from brain.soar import MasterBrain
    brain = MasterBrain()

    # AJ-03: Graceful shutdown
    loop = asyncio.get_running_loop()

    def _shutdown(sig_name: str) -> None:
        log.info("shutdown_requested", signal=sig_name)
        brain.stop()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig.name: _shutdown(s))

    # scanner_loop runs in the separate scanner container
    # brain runs here; data_feed runs in data_feed container
    await asyncio.gather(
        brain.run(),
        return_exceptions=True,
    )

    log.info("shutdown_complete")


if __name__ == "__main__":
    asyncio.run(_main())
