"""F49 §Component 7 — Continuous Online Learner.

Event-driven per-trade incremental updates for Direction Model and World
Model, triggered by every `CH_TRADE_CLOSED` Redis pub/sub message. Uses
F17 EWC (Elastic Weight Consolidation) + a 32-sample replay buffer batch
to prevent catastrophic forgetting.

For CandleNet / TFT / PatchTST, per-trade gradient steps are too expensive
on CPU. Those models stay on the orchestrator's drift/perf-triggered batch
retrain schedule.

Runs as a background asyncio task launched at brain startup:
  await start_online_learner()
"""
from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

REPLAY_BATCH = 32
EWC_LAMBDA   = 1000.0


async def start_online_learner() -> None:
    """Subscribe to CH_TRADE_CLOSED and update each per-trade model on every
    close. Designed to run forever; cancellation closes the pubsub cleanly."""
    log.info("online_learner_starting")
    r = redis_client.get()
    pubsub = r.pubsub()
    pubsub.subscribe(redis_keys.CH_TRADE_CLOSED)

    loop = asyncio.get_event_loop()
    try:
        while True:
            # Blocking get_message on a thread so we don't block the event loop
            msg = await loop.run_in_executor(
                None, lambda: pubsub.get_message(timeout=5.0, ignore_subscribe_messages=True))
            if msg is None:
                await asyncio.sleep(0.1)
                continue
            try:
                payload = json.loads(msg.get("data") or "{}")
                trade_id = payload.get("trade_id")
                if not trade_id:
                    continue
                await _process_close(trade_id)
            except Exception as exc:
                log.warning("online_learner_event_failed",
                            error=str(exc)[:200])
                await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        log.info("online_learner_cancelled")
        try:
            pubsub.unsubscribe()
            pubsub.close()
        except Exception:
            pass
        raise


async def _process_close(trade_id: str) -> None:
    """Fetch the closed trade and dispatch per-model updates."""
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM trades WHERE id = %s", (trade_id,))
            row = cur.fetchone()
            if not row:
                return
            cols = [d[0] for d in cur.description]
            trade = dict(zip(cols, row))

    if trade.get("status") != "closed":
        return

    # Add to the EWC replay buffer (F17 infrastructure)
    try:
        from ml.continual_learning import add_to_replay_buffer
        add_to_replay_buffer(trade)
    except Exception as exc:
        log.debug("online_replay_buffer_skipped", error=str(exc)[:120])

    # Direction Model — incremental fit using new trade + replay batch
    try:
        await _update_direction_model(trade)
    except Exception as exc:
        log.warning("online_direction_update_failed",
                    trade_id=trade_id, error=str(exc)[:200])

    # World Model — already updated by brain.world_model.update_on_trade_close
    # in the existing close handler. We do not duplicate; the EWC consolidation
    # step (ml.continual_learning.consolidate_world_model_if_ready) runs from
    # the brain consolidator and consumes the replay buffer this loop fills.


async def _update_direction_model(trade: dict) -> None:
    """One incremental gradient step on the Direction Model with the new trade
    + a replay batch. Uses the existing direction_model retrain pipeline but
    with a small EWC-penalised batch instead of a full retrain.

    Falls back silently if the direction model module doesn't expose an
    incremental-update API; in that case the batched 50-trade retrain in
    brain remains the only training path.
    """
    try:
        from ml import direction_model as _dm
    except Exception:
        return

    incr_fn = getattr(_dm, "online_update", None)
    if incr_fn is None:
        # Direction model in this build does not expose online_update; nothing
        # to do incrementally. Batched retrain (every 50 closed trades, brain
        # side) still runs.
        return

    try:
        from ml.continual_learning import get_replay_batch
        batch = get_replay_batch(REPLAY_BATCH)
    except Exception:
        batch = []

    payload = {
        "new_trade":      trade,
        "replay_batch":   batch,
        "ewc_lambda":     EWC_LAMBDA,
        "triggered_at":   int(datetime.now(timezone.utc).timestamp()),
    }
    try:
        # F49 §Component 7 — online_update returns a status dict; the
        # counter increment now lives inside direction_model.online_update
        # (it knows whether the update was actually applied vs skipped).
        result = incr_fn(payload)
        if isinstance(result, dict) and result.get("status") == "updated":
            log.debug("direction_model_online_updated",
                      n=result.get("n"), label=result.get("label"))
        elif isinstance(result, dict) and result.get("status") == "skipped":
            log.debug("direction_model_online_skipped",
                      reason=result.get("reason"))
    except Exception as exc:
        log.warning("direction_model_online_update_error",
                    error=str(exc)[:200])
