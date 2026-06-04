"""Reset world_model.pth and replay post-broken-era closed trades.

Cont. 39 (2026-05-22): the world model was online-trained on ~391 closed
trades, many of which came from the broken-trail era (cont. 23 and earlier).
Empirical probe in this session showed mean predicted prob_profit ≈ 0.10 vs
actual same-day win rate ≈ 0.58 — the model is inverted and rejects 100% of
LLM-generated strategies at F36 prescreen.

This script:
  1. Reinitialises models/world_model.pth with a fresh WorldModelBundle.
  2. Iterates closed trades from `CUTOFF_ISO` onwards in postgres.
  3. Reconstructs entry_obs / exit_obs from postgres columns (the original
     entry_obs in Redis was deleted on close).
  4. Calls train_rssm_step per trade.
  5. Reports loss progression so we can confirm the model actually moves.

Run inside the brain container:
  docker compose exec brain python -m tools.retrain_world_model

Idempotent — re-running just performs a fresh replay against the same data.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import structlog
import torch

from db import db_conn
from ml.architectures import WorldModelBundle
from world_model.model import train_rssm_step

log = structlog.get_logger()

MODELS_DIR  = Path("/app/models") if Path("/app/models").exists() else Path("models")
CKPT_PATH   = MODELS_DIR / "world_model.pth"
CUTOFF_ISO  = "2026-05-20 00:00:00+00"   # post broken-era boundary
BATCH_LOG_EVERY = 50


def _reset_checkpoint() -> None:
    """Re-init world_model.pth with a fresh WorldModelBundle state_dict.
    Same shape as pretrainer/main.py::train_world_model — no historical bias."""
    MODELS_DIR.mkdir(exist_ok=True)
    model = WorldModelBundle()
    torch.save(model.state_dict(), CKPT_PATH)
    log.info("world_model_checkpoint_reset", path=str(CKPT_PATH),
             params=sum(p.numel() for p in model.parameters()))


def _fetch_trades(cutoff: str) -> list[dict]:
    """Pull closed trades after cutoff with all fields needed for replay.
    Excludes rows missing entry/exit/leverage/capital so train_rssm_step
    never sees malformed obs."""
    sql = (
        "SELECT id, direction, entry_price, exit_price, "
        "       capital_usdt, leverage, net_pnl_usdt, exit_time "
        "FROM trades "
        "WHERE status = 'closed' "
        "  AND exit_time >= %s "
        "  AND entry_price IS NOT NULL AND entry_price > 0 "
        "  AND exit_price  IS NOT NULL AND exit_price  > 0 "
        "  AND capital_usdt IS NOT NULL AND capital_usdt > 0 "
        "  AND leverage IS NOT NULL AND leverage > 0 "
        "ORDER BY exit_time ASC"
    )
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cutoff,))
            rows = cur.fetchall()
    trades = []
    for r in rows:
        trades.append({
            "id":          str(r[0]),
            "direction":   r[1],
            "entry_price": float(r[2]),
            "exit_price":  float(r[3]),
            "capital":     float(r[4]),
            "leverage":    float(r[5]),
            "pnl":         float(r[6] or 0.0),
            "exit_time":   r[7],
        })
    return trades


def _replay(trades: list[dict]) -> dict:
    """Replay trades through train_rssm_step. Returns summary metrics."""
    losses_first = []
    losses_last  = []
    skipped      = 0
    errors       = 0
    started      = time.time()
    n = len(trades)
    for i, t in enumerate(trades):
        entry_obs = {"price": t["entry_price"], "volume": t["capital"], "lev": t["leverage"]}
        exit_obs  = {"price": t["exit_price"],  "volume": t["capital"], "lev": t["leverage"]}
        action    = "open_long" if t["direction"] == "long" else "open_short"
        try:
            res = train_rssm_step(entry_obs=entry_obs, action=action,
                                  exit_obs=exit_obs, actual_pnl=t["pnl"])
        except Exception as exc:
            errors += 1
            log.warning("retrain_step_error", trade_id=t["id"], error=str(exc)[:160])
            continue
        if res.get("status") != "updated":
            skipped += 1
            continue
        loss = float(res.get("loss") or 0.0)
        if i < 20:
            losses_first.append(loss)
        if i >= max(0, n - 20):
            losses_last.append(loss)
        if (i + 1) % BATCH_LOG_EVERY == 0:
            elapsed = time.time() - started
            log.info("retrain_progress", i=i + 1, n=n,
                     elapsed_s=round(elapsed, 1), loss=round(loss, 6),
                     skipped=skipped, errors=errors)

    return {
        "trades_seen":  n,
        "skipped":      skipped,
        "errors":       errors,
        "loss_first20": round(sum(losses_first) / len(losses_first), 6) if losses_first else None,
        "loss_last20":  round(sum(losses_last)  / len(losses_last),  6) if losses_last  else None,
        "elapsed_s":    round(time.time() - started, 1),
    }


def main() -> int:
    log.info("retrain_world_model_starting", cutoff=CUTOFF_ISO, ckpt=str(CKPT_PATH))

    _reset_checkpoint()

    trades = _fetch_trades(CUTOFF_ISO)
    if not trades:
        log.error("retrain_no_trades_found", cutoff=CUTOFF_ISO)
        return 1
    log.info("retrain_trades_fetched", count=len(trades),
             first_exit=str(trades[0]["exit_time"]),
             last_exit=str(trades[-1]["exit_time"]))

    summary = _replay(trades)
    log.info("retrain_complete", **summary)

    # Bust the baseline-probe cache so research/engine picks up the new model.
    try:
        import redis_client
        redis_client.get().delete("world_model:baseline_probe")
    except Exception:
        pass

    print("\nRetrain summary:")
    for k, v in summary.items():
        print(f"  {k:14s} = {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
