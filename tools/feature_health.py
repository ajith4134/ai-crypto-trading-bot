"""Per-feature firing-evidence audit (Path A).

Goal: for every blueprint feature (F2-F44), check ONE concrete observable that
proves the feature actually ran against real production data in the last N
hours. Output a status table + JSON file.

Statuses:
  firing   — evidence found and recent
  stale    — evidence found but older than threshold
  dead     — evidence absent (Redis key missing, DB query returns 0 rows)
  no_check — criterion not yet defined / feature not implemented
  error    — check itself threw

Run inside the brain container:
  docker exec trading-bot-brain-1 python -m tools.feature_health
"""
from __future__ import annotations
import json
import time
import sys
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Callable

import redis_client
from db import db_conn


HOUR = 3600
DAY = 24 * HOUR
WEEK = 7 * DAY


@dataclass
class Result:
    feature_id: str
    name: str
    status: str
    evidence: str
    last_seen_age_seconds: float | None = None


def _now() -> float:
    return time.time()


# ---------- low-level helpers ----------

def r_get(key: str) -> str | None:
    try:
        v = redis_client.get().get(key)
        return v
    except Exception:
        return None


def r_exists(key: str) -> bool:
    try:
        return bool(redis_client.get().exists(key))
    except Exception:
        return False


def r_ttl(key: str) -> int:
    try:
        return int(redis_client.get().ttl(key))
    except Exception:
        return -2


def r_llen(key: str) -> int:
    try:
        return int(redis_client.get().llen(key))
    except Exception:
        return 0


def r_keys(pattern: str, limit: int = 50) -> list[str]:
    try:
        keys = list(redis_client.get().scan_iter(pattern, count=200))
        return keys[:limit]
    except Exception:
        return []


def db_one(query: str, params: tuple = ()) -> tuple | None:
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()
    except Exception:
        return None


def db_scalar(query: str, params: tuple = ()) -> int | None:
    row = db_one(query, params)
    if not row:
        return None
    return row[0]


# Many features publish to a Redis key with an implicit "updated_at" via
# pub/sub. Without timestamps we infer freshness from existence + a sentinel
# "*:ts" sibling key when present, falling back to "exists = firing,
# missing = dead".

def redis_key_recent(key: str, max_age: int, ts_key: str | None = None) -> Result:
    """Helper: check if key exists. If ts_key given, compare its value to now."""
    if not r_exists(key):
        return Result("", "", "dead", f"key '{key}' missing")
    if ts_key:
        ts_raw = r_get(ts_key)
        if ts_raw:
            try:
                age = _now() - float(ts_raw)
                status = "firing" if age <= max_age else "stale"
                return Result("", "", status,
                              f"{key} present, {ts_key}={ts_raw} ({age:.0f}s ago)",
                              age)
            except Exception:
                pass
    return Result("", "", "firing", f"{key} present (no ts sibling)")


# ---------- per-feature checks ----------

def check_F2_pattern_mining() -> Result:
    """F2: pattern_miner writes to brain:patterns_mined after every run."""
    val = r_get("brain:patterns_mined")
    if val:
        return Result("", "", "firing", f"brain:patterns_mined present, {len(val)} chars")
    return Result("", "", "dead", "brain:patterns_mined missing — beat task hasn't fired")


def check_F4_trailing_sl() -> Result:
    """F4: Trailing SL — every recent closed trade should have non-null
    trailing_sl_level if the feature is firing."""
    row = db_one(
        "SELECT COUNT(*), COUNT(trailing_sl_level) FROM trades "
        "WHERE entry_time > NOW() - INTERVAL '24 hours' AND status='closed'"
    )
    if not row or row[0] == 0:
        return Result("", "", "no_check", "no closed trades in last 24h")
    pct = 100 * row[1] / max(row[0], 1)
    status = "firing" if pct >= 50 else "stale"
    return Result("", "", status, f"{row[1]}/{row[0]} closed-24h have trailing_sl ({pct:.0f}%)")


def check_F5_dca() -> Result:
    """F5: DCA — at least one trade in last 7d had dca round_1 or round_2 fire."""
    n = db_scalar(
        "SELECT COUNT(*) FROM trades WHERE entry_time > NOW() - INTERVAL '7 days' "
        "AND (dca_status->>'round_1_triggered')::boolean = true"
    )
    if n is None:
        return Result("", "", "error", "DCA query failed")
    if n > 0:
        return Result("", "", "firing", f"{n} trades had DCA round-1 in last 7d")
    return Result("", "", "stale", "no DCA-1 fires in last 7d (could be normal — no drawdowns)")


def check_F8_strategy_promote() -> Result:
    """F8 Strategy Lifecycle + Selector. Two sides per Rule 4 (post 2026-05-21
    cont. 7):
      (a) LIFECYCLE — strategies promoted to active in last 7d (or sustained
          active pool size when no recent promotions).
      (b) SELECTOR — UCB1 selector firing: selector:picks_count > 0 AND
          the set of distinct strategies picked has ≥ 2 members (proving the
          bandit actually explores across the pool, not just one winner).

    Firing requires the SELECTOR side. Lifecycle alone is fine if no
    experimentals are ready yet — but selector inactive means the
    pre-cont.-7 fixed-strategy-per-stage regression is back."""
    total_active = db_scalar("SELECT COUNT(*) FROM strategies WHERE status='active'")
    if total_active is None:
        return Result("", "", "error", "strategies query failed")
    recent_new = db_scalar(
        "SELECT COUNT(*) FROM strategies "
        "WHERE status='active' AND created_at > NOW() - INTERVAL '7 days'"
    ) or 0

    picks_count = int(r_get("selector:picks_count") or 0)
    last_pick_ts = r_get("selector:last_picked_ts")
    last_pick_name = r_get("selector:last_picked_name") or "?"
    try:
        distinct_picks = redis_client.get().scard("selector:distinct_strategies_picked") or 0
    except Exception:
        distinct_picks = 0

    pick_fresh = False
    pick_age_h = None
    if last_pick_ts:
        try:
            pick_age_h = (_now() - float(last_pick_ts)) / 3600.0
            pick_fresh = pick_age_h <= DAY / 3600.0
        except Exception:
            pass

    lifecycle_summary = (f"active={total_active} promoted_7d={recent_new}"
                         if total_active is not None else "lifecycle_unknown")
    selector_summary = (f"picks={picks_count} distinct={distinct_picks} "
                        f"last={last_pick_name}")
    if pick_age_h is not None:
        selector_summary += f" age={pick_age_h:.1f}h"

    # F8 Router activity (post 2026-05-21 cont. 16). Cont. 8/9/13 routers each
    # bump distinct Redis counters; surface them as a 4th evidence line so the
    # dashboard shows how much per-strategy routing the bot is actually doing.
    rt_entry_acc  = r_get("strategy_router:entry_accepted_count") or "0"
    rt_entry_rej  = r_get("strategy_router:entry_rejected_count") or "0"
    rt_dca        = r_get("strategy_router:dca_routed_count") or "0"
    rt_cap        = r_get("strategy_router:capital_routed_count") or "0"
    rt_sl_init    = r_get("strategy_router:sl_initial_count") or "0"
    rt_sl_trail   = r_get("strategy_router:sl_trailing_count") or "0"
    router_summary = (f"router: entry={rt_entry_acc}/{rt_entry_rej} "
                      f"dca={rt_dca} cap={rt_cap} "
                      f"sl_init={rt_sl_init} sl_trail={rt_sl_trail}")

    if pick_fresh and distinct_picks >= 1:
        return Result("", "", "firing",
                      f"{lifecycle_summary} | {selector_summary} | {router_summary}")
    if not pick_fresh and total_active > 0:
        return Result("", "", "stale",
                      f"selector silent — {lifecycle_summary} | {selector_summary} | {router_summary}")
    return Result("", "", "dead",
                  f"no active strategies AND selector inactive | {lifecycle_summary} | {selector_summary}")


def check_F9_rejected_signals() -> Result:
    """F9 Rejected Signal Scanner + Miss Decoder. Two sides per Rule 4:
      (a) producer  — rejected signals exist in last 24h (counterfactuals fed)
      (b) consumer — miss decoder ran on shadow wins (counterfactuals.miss_decoded)

    Firing requires either no shadow wins yet (decoder idle correctly) OR
    miss_decoded > 0 in last 7 days. Asymmetric — shadow wins accumulating
    without decodes is stale (the post-2026-05-21 Miss Decoder didn't run)."""
    row = db_one(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE accepted=false) FROM signals "
        "WHERE generated_at > NOW() - INTERVAL '24 hours'"
    )
    if not row:
        return Result("", "", "error", "signals query failed")
    total, rejected = row
    if total == 0:
        return Result("", "", "no_check", "no signals in last 24h")
    if rejected == 0:
        return Result("", "", "dead",
                      f"0/{total} signals rejected — counterfactual tracking inert")

    cf_row = db_one(
        "SELECT "
        "  COUNT(*) FILTER (WHERE would_have_won) AS shadow_wins, "
        "  COUNT(*) FILTER (WHERE miss_decoded)   AS decoded, "
        "  COUNT(*) FILTER (WHERE would_have_won AND NOT miss_decoded) AS pending "
        "FROM counterfactuals"
    )
    if not cf_row:
        return Result("", "", "stale",
                      f"{rejected}/{total} rejected (24h) but counterfactuals query failed")
    shadow_wins, decoded, pending = cf_row

    if shadow_wins == 0:
        return Result("", "", "firing",
                      f"{rejected}/{total} rejected (24h) / no shadow wins yet")
    if decoded == 0:
        return Result("", "", "stale",
                      f"{rejected}/{total} rejected (24h) / {shadow_wins} shadow wins / "
                      f"{pending} pending decode — Miss Decoder hasn't fired")
    return Result("", "", "firing",
                  f"{rejected}/{total} rejected (24h) / decoded={decoded} pending={pending}")


def check_F12_mismatch_decoder() -> Result:
    """F12 Mismatch Decoder. Producer-side: contemporaneous (high-pot loser,
    low-pot winner) pairs exist. Consumer-side: mismatches table has rows.

    Special-case 'no producer data' as no_check rather than dead — F12 only
    fires when the bot actually produces mispriced trades; a bot rating
    every trade correctly is a feature, not a regression. Becomes stale
    only when mispriced pairs exist but the decoder hasn't run on them."""
    if not _table_exists("mismatches"):
        return Result("", "", "no_check", "mismatches table missing (migration 014?)")

    pair_count = db_scalar("""
        SELECT COUNT(*)
        FROM trades loser
        JOIN trades winner
          ON winner.status = 'closed'
         AND winner.net_pnl_usdt > 0
         AND winner.trade_potential_score < 40
         AND ABS(EXTRACT(EPOCH FROM (winner.exit_time - loser.exit_time))) < 86400
         AND winner.id != loser.id
        WHERE loser.status = 'closed'
          AND loser.net_pnl_usdt < 0
          AND loser.trade_potential_score >= 60
    """)
    decoded = db_scalar("SELECT COUNT(*) FROM mismatches WHERE decode_reason IS NOT NULL")
    if pair_count is None or decoded is None:
        return Result("", "", "error", "mismatch query failed")

    summary = f"pairs={pair_count} decoded={decoded}"
    if pair_count == 0:
        return Result("", "", "no_check",
                      f"no mispriced (high-pot loser, low-pot winner) pairs yet — {summary}")
    if decoded == 0:
        return Result("", "", "stale",
                      f"{pair_count} mispriced pairs detected but Mismatch Decoder hasn't fired — {summary}")
    return Result("", "", "firing", summary)


def check_F10_pair_scanner() -> Result:
    """F10 Pair Scanner — TWO sides per Rule 4 (post 2026-05-21 cont. 18):
      (a) ACTIVE_PAIRS Redis set populated (scanner produces).
      (b) Brain-learned criteria weights — `criteria_weights:update_count` > 0
          proves the cont. 18 learner is running. NULL learner = scanner
          still uses config-static weights (pre-cont-18 behaviour); flag as
          partial firing rather than full."""
    try:
        from redis_keys import ACTIVE_PAIRS
        n = redis_client.get().scard(ACTIVE_PAIRS)
    except Exception as exc:
        return Result("", "", "error", f"{type(exc).__name__}: {exc}")
    if n == 0:
        return Result("", "", "dead", "ACTIVE_PAIRS empty")

    lw_count = r_get("criteria_weights:update_count") or "0"
    lw_ts    = r_get("criteria_weights:last_update_ts")
    lw_age_h = None
    if lw_ts:
        try:
            lw_age_h = (_now() - float(lw_ts)) / 3600.0
        except Exception:
            pass
    lw_summary = f"learned_weight_updates={lw_count}"
    if lw_age_h is not None:
        lw_summary += f" (last {lw_age_h:.1f}h ago)"

    if int(lw_count) > 0:
        return Result("", "", "firing",
                      f"{n} active pairs / {lw_summary}")
    return Result("", "", "firing",
                  f"{n} active pairs / {lw_summary} (config-static fallback)")


def check_F13_direction() -> Result:
    """F13: Direction Model — recent trades should have varied direction_confidence."""
    row = db_one(
        "SELECT COUNT(DISTINCT direction_confidence), COUNT(*) FROM trades "
        "WHERE entry_time > NOW() - INTERVAL '24 hours'"
    )
    if not row or row[1] == 0:
        return Result("", "", "no_check", "no trades in 24h")
    distinct, total = row
    if distinct >= 5:
        return Result("", "", "firing", f"{distinct} distinct confidences across {total} trades")
    if distinct > 0:
        return Result("", "", "stale", f"only {distinct} distinct values — model likely returning OFI strength only")
    return Result("", "", "dead", "all direction_confidence NULL or identical")


def check_F14_hmm() -> Result:
    """F14: HMM regime — CURRENT_REGIME Redis key updated."""
    from redis_keys import CURRENT_REGIME
    val = r_get(CURRENT_REGIME)
    if not val:
        return Result("", "", "dead", "CURRENT_REGIME key missing")
    return Result("", "", "firing", f"regime={val}")


def check_F15_microstructure() -> Result:
    """F15: VPIN/OFI/Kyle λ — Redis keys are <PAIR>:vpin (suffix not prefix)."""
    vpin = r_keys("*:vpin")
    ofi = r_keys("*:ofi")
    kyle = r_keys("*:kyles_lambda")
    if vpin or ofi:
        return Result("", "", "firing",
                      f"vpin={len(vpin)} ofi={len(ofi)} kyles_lambda={len(kyle)} per-pair keys")
    return Result("", "", "dead", "no *:vpin or *:ofi per-pair keys")


def check_F16_kelly() -> Result:
    """F16 Kelly — durable counter from ml.kelly._mark_called."""
    last_ts = r_get("kelly:last_call_ts")
    count = r_get("kelly:call_count") or "0"
    pct = r_get("kelly:last_pct")
    if last_ts:
        age = _now() - float(last_ts)
        status = "firing" if age <= DAY else "stale"
        return Result("", "", status,
                      f"call_count={count} last_pct={pct} ({age/60:.1f}m ago)",
                      age)
    return Result("", "", "dead", "no kelly:* keys — never invoked")


def check_F17_ewc() -> Result:
    """F17: Continual Learning EWC — replay buffer + Fisher state."""
    buf_len = r_llen("ewc:replay_buffer")
    fisher_present = r_exists("ewc:state")
    if buf_len > 0 and fisher_present:
        return Result("", "", "firing",
                      f"replay_buffer len={buf_len}, ewc:state present")
    if buf_len > 0:
        return Result("", "", "stale", f"replay_buffer len={buf_len}, but no ewc:state Fisher")
    return Result("", "", "dead", "ewc:replay_buffer empty")


def check_F18_sentiment() -> Result:
    """F18: Sentiment — distinguishes the real CryptoBERT+FinBERT path
    (PROGRESS.md cont. 9) from the Fear & Greed proxy fallback.

    Reads:
      SENTIMENT_GLOBAL              — 0..1 scalar consumed by signals/brain
      sentiment:source              — 'cryptobert+finbert' (real) | 'fear_greed_proxy'
      sentiment:real_last_update_ts — set by ml.sentiment.update_global_sentiment
      sentiment:n_samples           — texts in the last real-update batch
    """
    from redis_keys import SENTIMENT_GLOBAL
    val = r_get(SENTIMENT_GLOBAL)
    if not val:
        return Result("", "", "dead", "SENTIMENT_GLOBAL missing")

    source = r_get("sentiment:source") or "unknown"
    real_ts = r_get("sentiment:real_last_update_ts")
    n_samples = r_get("sentiment:n_samples") or "0"

    real_fresh = False
    real_age = None
    if real_ts:
        try:
            real_age = _now() - float(real_ts)
            real_fresh = real_age <= 30 * 60   # matches ml.sentiment FRESHNESS_SECONDS
        except Exception:
            pass

    if source == "cryptobert+finbert" and real_fresh:
        return Result("", "", "firing",
                      f"sentiment={val} (real CryptoBERT+FinBERT, n={n_samples}, "
                      f"updated {real_age/60:.1f}m ago)",
                      real_age)
    if real_ts and not real_fresh:
        return Result("", "", "stale",
                      f"sentiment={val} (real stale: last update "
                      f"{real_age/3600:.1f}h ago, proxy holding the value)",
                      real_age)
    return Result("", "", "firing",
                  f"sentiment={val} (F&G proxy, blueprint wants real CryptoBERT — "
                  "celery task score_web_intel_sentiment hasn't fired yet)")


def check_F19_tft() -> Result:
    """F19 TFT: 1h forecasts per pair, written by ml.tft.forecast.
    Cache TTL is short so misses indicate the producer isn't running."""
    forecasts = r_keys("*:1h:forecast")
    if forecasts:
        return Result("", "", "firing", f"{len(forecasts)} pair forecast keys")
    # F19's producer may be intermittent (TTL ~5min). Also check we have candles.
    if r_keys("*:1h:candles"):
        return Result("", "", "stale", "no live forecasts but candles present (forecast TTL expired)")
    return Result("", "", "dead", "no *:1h:forecast and no *:1h:candles")


def check_F20_patchtst() -> Result:
    """F20 PatchTST: per-pair longseq_forecast keys written by data_feed poller."""
    keys = r_keys("*:longseq_forecast")
    if keys:
        return Result("", "", "firing", f"{len(keys)} *:longseq_forecast keys")
    return Result("", "", "dead", "no *:longseq_forecast per-pair keys")


def check_F21_marl() -> Result:
    """F21: MARL agent call counters from ml.marl._mark_called.
    Reports 'firing (default passthrough)' when calls are made but no
    checkpoint loaded — distinguishes from truly-dead."""
    day_ts = r_get("marl:day:last_call_ts")
    minute_ts = r_get("marl:minute:last_call_ts")
    last_ts = max((float(t) for t in (day_ts, minute_ts) if t), default=None)
    if last_ts is None:
        return Result("", "", "dead", "no marl:*:last_call_ts keys")
    age = _now() - last_ts
    day_active = (r_get("marl:day:last_active") == "1")
    minute_active = (r_get("marl:minute:last_active") == "1")
    day_n = r_get("marl:day:call_count") or "0"
    minute_n = r_get("marl:minute:call_count") or "0"
    state = "checkpoints_loaded" if (day_active or minute_active) else "default_passthrough"
    status = "firing" if age <= DAY else "stale"
    return Result("", "", status,
                  f"day_calls={day_n} minute_calls={minute_n} state={state} ({age/60:.1f}m ago)",
                  age)


def check_F22_maml() -> Result:
    """F22: MAML — durable evidence from ml:maml:last_adapt_ts / adapt_count.
    Also report 'gated_off' when feature_governance has F22 inactive (design,
    not a bug — Phase 4 activation requires Stage 4)."""
    last_ts = r_get("ml:maml:last_adapt_ts")
    count = r_get("ml:maml:adapt_count")
    loss = r_get("ml:maml:last_mean_loss")
    if last_ts:
        age = _now() - float(last_ts)
        status = "firing" if age <= WEEK else "stale"
        return Result("", "", status,
                      f"adapt_count={count} last_loss={loss} ({age/3600:.1f}h ago)",
                      age)
    if _feature_inactive("F22"):
        return Result("", "", "no_check",
                      "F22 inactive in governance (Phase 4 — Stage 4 required)")
    if r_exists("maml:adapt_lock"):
        return Result("", "", "firing", "maml:adapt_lock present (BOCPD triggered, await first success)")
    return Result("", "", "dead", "no MAML adapt evidence")


def check_F23_mi() -> Result:
    """F23: Mutual Information — analytics:feature_relevance key populated."""
    val = r_get("analytics:feature_relevance")
    if val:
        return Result("", "", "firing", f"feature_relevance present, len={len(val)} chars")
    return Result("", "", "dead", "analytics:feature_relevance missing")


def check_F24_gnn() -> Result:
    """F24 GNN: writes `interasset_signals` Redis key (1h TTL)."""
    val = r_get("interasset_signals")
    if val:
        return Result("", "", "firing", f"interasset_signals present, {len(val)} chars")
    return Result("", "", "dead", "interasset_signals key missing")


def check_F25_ga() -> Result:
    """F25: GA — ga:history list."""
    n = r_llen("ga:history")
    if n > 0:
        return Result("", "", "firing", f"ga:history has {n} entries")
    if r_exists("ga:best_params"):
        return Result("", "", "stale", "ga:best_params present but history empty")
    return Result("", "", "dead", "no ga:history or ga:best_params")


def check_F26_bocpd() -> Result:
    """F26: durable fire counter + last timestamp written by ml.bocpd."""
    last_ts = r_get("bocpd:last_fired_ts")
    count = r_get("bocpd:fires_count")
    last_pair = r_get("bocpd:last_fired_pair")
    if last_ts:
        age = _now() - float(last_ts)
        status = "firing" if age <= HOUR else "stale"
        return Result("", "", status,
                      f"fires_count={count} last_pair={last_pair} ({age:.0f}s ago)",
                      age)
    if r_exists("maml:adapt_lock"):
        return Result("", "", "firing", "maml:adapt_lock present (BOCPD fired in last hour)")
    return Result("", "", "dead", "no BOCPD fire evidence")


def check_F27_transfer_entropy() -> Result:
    """F27 Transfer Entropy: ml/transfer_entropy.get_lead_lag_matrix writes
    analytics:lead_lag_matrix (1h TTL)."""
    val = r_get("analytics:lead_lag_matrix")
    if val:
        return Result("", "", "firing", f"analytics:lead_lag_matrix present, {len(val)} chars")
    return Result("", "", "dead", "analytics:lead_lag_matrix missing")


def check_F28_turbulence() -> Result:
    from redis_keys import TURBULENCE_INDEX
    val = r_get(TURBULENCE_INDEX)
    if val is None:
        return Result("", "", "dead", "TURBULENCE_INDEX missing")
    return Result("", "", "firing", f"turbulence={val}")


def check_F29_web_intel() -> Result:
    if not _table_exists("web_intelligence"):
        return Result("", "", "no_check", "web_intelligence table missing")
    n = db_scalar(
        "SELECT COUNT(*) FROM web_intelligence WHERE fetched_at > NOW() - INTERVAL '24 hours'"
    )
    if n is None:
        return Result("", "", "error", "query failed")
    if n > 0:
        return Result("", "", "firing", f"{n} web_intel rows last 24h")
    return Result("", "", "dead", "0 web_intel rows last 24h")


def check_F30_governance() -> Result:
    """F30: Feature Governance — contribution scores written."""
    keys = r_keys("feature:*:contribution") + r_keys("governance:*")
    if keys:
        return Result("", "", "firing", f"{len(keys)} governance contribution keys")
    return Result("", "", "dead", "no feature:*:contribution keys")


def check_F31_analytics() -> Result:
    keys = r_keys("analytics:metrics:*")
    if keys:
        return Result("", "", "firing", f"{len(keys)} analytics:metrics keys")
    return Result("", "", "dead", "no analytics:metrics keys")


def check_F32_account_risk() -> Result:
    keys = r_keys("account:*") + r_keys("risk:*")
    if keys:
        return Result("", "", "firing", f"{len(keys)} account/risk keys")
    return Result("", "", "dead", "no account/risk keys")


def check_F33_watchdog() -> Result:
    val = r_get("watchdog:alive")
    if val:
        return Result("", "", "firing", f"watchdog:alive ttl={r_ttl('watchdog:alive')}s")
    return Result("", "", "dead", "watchdog:alive missing")


def check_F34_world_model() -> Result:
    """F34: World Model — THREE independent firing sides per blueprint Feature 34:

    1. Per-trade predictions stored at open time (`world_model:prediction:<trade_id>`).
       Proves the reward head is queried and post-hoc training has substrate.
    2. Model-Predictive Planning (MPP) calls at signal-decision time
       (`world_model:mpp_calls_count` + `world_model:mpp_last_ts`).
       Proves the world model actually informs entry decisions.
    3. RSSM core online training — `world_model:rssm_updates_count`
       (post-2026-05-21 cont. 5). Proves encoder+GRU+prior+posterior are
       training, not just the reward head.

    Firing requires (1) AND (2). RSSM training is surfaced as an extra
    evidence line but does not gate firing — the RSSM path only runs on
    trades opened AFTER the cont. 5 fix lands, so older closed trades
    won't drive it; firing should still report fully while the new path
    accumulates samples."""
    pred_keys = [k for k in r_keys("world_model:prediction:*", limit=500)]
    has_pred = bool(pred_keys)

    mpp_calls = r_get("world_model:mpp_calls_count") or "0"
    mpp_ts_raw = r_get("world_model:mpp_last_ts")
    mpp_action = r_get("world_model:mpp_last_best_action") or "?"
    mpp_fresh = False
    mpp_age = None
    if mpp_ts_raw:
        try:
            mpp_age = _now() - float(mpp_ts_raw)
            mpp_fresh = mpp_age <= DAY
        except Exception:
            pass

    rssm_updates = r_get("world_model:rssm_updates_count") or "0"
    rssm_last_loss = r_get("world_model:rssm_last_loss") or "n/a"
    rssm_summary = f"rssm_updates={rssm_updates} last_loss={rssm_last_loss}"
    # L2 inline gate evidence (post 2026-05-21 cont. 10). Soft scales and hard
    # rejects driven by brain:world_model_uncertainty.
    l2_scales  = r_get("inline_decide:l2_scale_count") or "0"
    l2_rejects = r_get("inline_decide:l2_reject_count") or "0"
    rssm_summary += f" / L2 gate scales={l2_scales} rejects={l2_rejects}"

    if has_pred and mpp_fresh:
        return Result("", "", "firing",
                      f"predictions={len(pred_keys)} | MPP calls={mpp_calls} "
                      f"last_action={mpp_action} age={mpp_age/3600:.1f}h | "
                      f"{rssm_summary}",
                      mpp_age)
    if has_pred and not mpp_fresh:
        return Result("", "", "stale",
                      f"predictions={len(pred_keys)} but MPP never fired "
                      f"(calls={mpp_calls}, last_ts={mpp_ts_raw or 'none'}) | "
                      f"{rssm_summary}")
    if mpp_fresh and not has_pred:
        return Result("", "", "stale",
                      f"MPP firing (calls={mpp_calls}) but no world_model:prediction:* keys",
                      mpp_age)
    return Result("", "", "dead",
                  f"no world_model:prediction:* keys and no MPP calls | {rssm_summary}")


def check_F35_memrl() -> Result:
    """F35: TWO independent firing sides per blueprint Feature 35:

    1. RETRIEVAL — memrl:retrieve_phase1/2 / base_rate_sample counters prove
       the consumer side (signals/engine.py) is reading memories.
    2. Q-LEARNING — q_learning:updates_count + distinct_buckets prove the
       producer side (memory/write.py:write_trade_close) is actually doing
       constant-α MC updates per closed trade. This is the blueprint's
       "Q-value learning" (arXiv:2601.03192), previously absent — Phase 2 was
       raw-PnL sort.

    Firing requires BOTH sides active in the last 24h. If Q-learning is
    populated but retrieval hasn't fired (or vice-versa), status is `stale`
    — surfaces asymmetric-pipeline regressions per Rule 4 producer-side check.
    """
    p1 = r_get("memrl:retrieve_phase1_count") or "0"
    p2 = r_get("memrl:retrieve_phase2_count") or "0"
    br = r_get("memrl:base_rate_sample_count") or "0"
    retrieve_ts = (r_get("memrl:retrieve_phase2_last_ts")
                   or r_get("memrl:base_rate_sample_last_ts")
                   or r_get("memrl:retrieve_phase1_last_ts"))
    retrieve_fresh = False
    retrieve_age = None
    if retrieve_ts:
        try:
            retrieve_age = _now() - float(retrieve_ts)
            retrieve_fresh = retrieve_age <= DAY
        except Exception:
            pass

    q_updates = r_get("q_learning:updates_count") or "0"
    q_last_ts = r_get("q_learning:last_update_ts")
    try:
        q_buckets = int(redis_client.get().scard("q_learning:distinct_buckets") or 0)
    except Exception:
        q_buckets = 0
    q_fresh = False
    q_age = None
    if q_last_ts:
        try:
            q_age = _now() - float(q_last_ts)
            q_fresh = q_age <= 7 * DAY  # Q-update only fires on trade close (slower than retrieval)
        except Exception:
            pass

    retrieve_summary = f"p1={p1} p2={p2} base_rate={br}"
    if retrieve_age is not None:
        retrieve_summary += f" (last {retrieve_age/3600:.1f}h ago)"
    q_summary = f"q_updates={q_updates} buckets={q_buckets}"
    if q_age is not None:
        q_summary += f" (last {q_age/3600:.1f}h ago)"

    # Fast→Slow consolidation evidence (post 2026-05-21 cont. 6). Surfaced as
    # a third line; does NOT gate firing — consolidation runs on the sleep
    # beat (daily-ish), so it can be hours stale and still healthy.
    cons_count = r_get("memrl:consolidation_count") or "0"
    cons_ts    = r_get("memrl:consolidation_last_ts")
    cons_upd   = r_get("memrl:consolidation_last_clusters_updated") or "0"
    cons_new   = r_get("memrl:consolidation_last_clusters_created") or "0"
    cons_age_h = None
    if cons_ts:
        try:
            cons_age_h = (_now() - float(cons_ts)) / 3600.0
        except Exception:
            pass
    cons_summary = f"consolidations={cons_count} upd={cons_upd} new={cons_new}"
    if cons_age_h is not None:
        cons_summary += f" (last {cons_age_h:.1f}h ago)"
    # Cluster CONSUMER evidence (post 2026-05-21 cont. 14). Closes the
    # asymmetric pipeline I flagged in cont. 6 — until cont. 14 nothing
    # READ memory_clusters even though sleep consolidation produced them.
    cluster_reads   = r_get("memrl:cluster_context_count")    or "0"
    cluster_scaled  = r_get("memrl:cluster_scaled_down_count") or "0"
    cluster_boosted = r_get("memrl:cluster_boosted_count")    or "0"
    cons_summary += (f" / cluster_reads={cluster_reads} "
                     f"scaled={cluster_scaled} boosted={cluster_boosted}")

    if retrieve_fresh and q_fresh and q_buckets >= 1:
        return Result("", "", "firing",
                      f"{retrieve_summary} | {q_summary} | {cons_summary}",
                      retrieve_age)
    if retrieve_fresh and not q_fresh:
        return Result("", "", "stale",
                      f"retrieval firing ({retrieve_summary}) but Q-learning never fired ({q_summary})",
                      retrieve_age)
    if q_fresh and not retrieve_fresh:
        return Result("", "", "stale",
                      f"Q-learning firing ({q_summary}) but retrieval cold ({retrieve_summary})",
                      q_age)
    return Result("", "", "dead",
                  f"neither side fired ({retrieve_summary} | {q_summary})")


def check_F36_strategy_research() -> Result:
    """F36: needs BOTH a recent research strategy AND prescreen-side evidence
    (Step 4 world-model prescreen ran). Rule 4 asymmetric-pipeline guard —
    after 2026-05-21 cont. 2 the prescreen is no longer a stub, so failing
    to see prescreen evidence means the new gate isn't being exercised."""
    if not _table_exists("strategies"):
        return Result("", "", "no_check", "strategies table missing")
    n = db_scalar(
        "SELECT COUNT(*) FROM strategies WHERE source IN ('research','self_play') "
        "AND created_at > NOW() - INTERVAL '7 days'"
    )
    if n is None:
        return Result("", "", "error", "query failed")

    import redis_client, time
    try:
        r = redis_client.get()
        pre_count   = int(r.get("research:prescreen_count") or 0)
        pre_last_ts = int(r.get("research:prescreen_last_ts") or 0)
        iter_runs   = int(r.get("research:iteration_runs") or 0)
        last_iters  = int(r.get("research:iteration_last_iters") or 0)
    except Exception as exc:
        return Result("", "", "error", f"redis read failed: {str(exc)[:60]}")

    pre_age_h = (time.time() - pre_last_ts) / 3600.0 if pre_last_ts else None
    pre_summary = (f"prescreens={pre_count} iter_runs={iter_runs} "
                   f"last_iters={last_iters} pre_age="
                   f"{round(pre_age_h, 1) if pre_age_h is not None else 'n/a'}h")

    if n > 0 and pre_count > 0:
        return Result("", "", "firing",
                      f"{n} strategies last 7d / {pre_summary}")
    if n > 0 and pre_count == 0:
        return Result("", "", "stale",
                      f"{n} strategies last 7d but prescreen never fired ({pre_summary})")
    if n == 0 and pre_count > 0:
        return Result("", "", "stale",
                      f"prescreen firing ({pre_summary}) but 0 strategies created last 7d")
    return Result("", "", "dead", f"0 research strategies last 7d / {pre_summary}")


def check_F37_debate() -> Result:
    """F37: Debate Council — THREE independent firing sides per blueprint F37:

    1. VERDICT — signals.debate_verdict populated on stage>=3 signals (round-1
       output is here; existed pre-Phase-C).
    2. ROUNDS 2/3 + persistence — debate_arguments rows. round_num in {1,2,3}
       proves the multi-round protocol ran. Row count + recent insert ts.
    3. VERBAL REINFORCEMENT — debate:beliefs_updates_count + last_ts evidence
       that update_beliefs_on_close fired at trade close and updated the
       per-agent weight priors.

    Firing requires (1) AND at least one of (2,3) — round-1-only is now stale
    (the Phase-C wiring exists, so failing to use it is a regression signal).
    """
    if not _column_exists("signals", "debate_verdict"):
        return Result("", "", "no_check", "signals.debate_verdict column missing")
    row = db_one(
        "SELECT COUNT(*), COUNT(debate_verdict) FROM signals "
        "WHERE generated_at > NOW() - INTERVAL '24 hours' AND brain_stage >= 3"
    )
    if not row or row[0] == 0:
        return Result("", "", "no_check", "no stage>=3 signals last 24h")
    total, with_verdict = row

    # Multi-round persistence side.
    args_total = 0
    args_r2 = 0
    args_r3 = 0
    if _column_exists("debate_arguments", "round_num"):
        args_row = db_one(
            "SELECT COUNT(*) FILTER (WHERE round_num = 1), "
            "       COUNT(*) FILTER (WHERE round_num = 2), "
            "       COUNT(*) FILTER (WHERE round_num = 3) "
            "FROM debate_arguments WHERE created_at > NOW() - INTERVAL '24 hours'"
        )
        if args_row:
            args_total, args_r2, args_r3 = args_row[0], args_row[1], args_row[2]

    # Verbal reinforcement side.
    vr_count = r_get("debate:beliefs_updates_count") or "0"
    vr_last_ts = r_get("debate:beliefs_last_ts")
    vr_fresh = False
    vr_age = None
    if vr_last_ts:
        try:
            vr_age = _now() - float(vr_last_ts)
            vr_fresh = vr_age <= 7 * DAY
        except Exception:
            pass

    if with_verdict == 0:
        return Result("", "", "dead",
                      f"0/{total} stage>=3 signals have debate_verdict (F37 wiring inert)")

    parts = [f"verdicts={with_verdict}/{total}"]
    if args_total or args_r2 or args_r3:
        parts.append(f"args r1={args_total} r2={args_r2} r3={args_r3}")
    parts.append(f"belief_updates={vr_count}")
    if vr_age is not None:
        parts.append(f"vr_last={vr_age/3600:.1f}h")
    summary = " | ".join(parts)

    has_multi_round = args_r2 > 0 or args_r3 > 0
    has_vr = vr_fresh and int(vr_count) > 0

    if has_multi_round and has_vr:
        return Result("", "", "firing", summary)
    if has_multi_round or has_vr:
        # One of the Phase-C sides firing → blueprint-grade partial.
        return Result("", "", "firing", summary + " (partial Phase-C)")
    # Round-1 only — that's the pre-Phase-C bar, now reported as stale because
    # round-2/3 + verbal-reinforcement wiring exists but didn't fire (likely
    # LLM circuit open OR no disagreement → no rounds 2/3 needed).
    return Result("", "", "stale", summary + " (round-1 only — rounds 2/3 + VR never fired)")


def check_F38_curiosity() -> Result:
    val = r_get("brain:curiosity_score")
    if val is not None:
        return Result("", "", "firing", f"brain:curiosity_score={val}")
    return Result("", "", "dead", "brain:curiosity_score missing")


def check_F39A_opro() -> Result:
    val = r_get("brain:executor_prompt_addendum")
    if val:
        return Result("", "", "firing", f"addendum len={len(val)} chars")
    return Result("", "", "dead", "brain:executor_prompt_addendum missing")


def check_F39B_dgm() -> Result:
    """F39B DGM: durable counter from celery_app.dgm_rewrite_weakest.
    Beat schedule fires Mondays 05:00 UTC at 500+ trades — stale if older
    than a week."""
    last_ts = r_get("dgm:last_run_ts")
    count = r_get("dgm:run_count") or "0"
    submitted = r_get("dgm:last_submitted_count") or "0"
    if last_ts:
        age = _now() - float(last_ts)
        status = "firing" if age <= WEEK else "stale"
        return Result("", "", status,
                      f"run_count={count} last_submitted={submitted} ({age/3600:.1f}h ago)",
                      age)
    return Result("", "", "dead", "no dgm:last_run_ts — never invoked (next Monday 05:00 UTC)")


def check_F39C_ai_scientist() -> Result:
    """F39C AI Scientist: durable counter from celery_app.ai_scientist_run.
    Beat schedule fires every 4h at 300+ trades."""
    last_ts = r_get("ai_scientist:last_run_ts")
    count = r_get("ai_scientist:run_count") or "0"
    task_id = r_get("ai_scientist:last_task_id")
    if last_ts:
        age = _now() - float(last_ts)
        status = "firing" if age <= DAY else "stale"
        return Result("", "", status,
                      f"run_count={count} last_task={task_id} ({age/3600:.1f}h ago)",
                      age)
    return Result("", "", "dead", "no ai_scientist:last_run_ts — never invoked")


def check_F40_llm_layer() -> Result:
    """F40: Local LLM Layer — ollama probe success OR cloud key configured."""
    import config as _cfg
    cloud = any([_cfg.GROQ_API_KEY, _cfg.CEREBRAS_API_KEY, _cfg.SAMBANOVA_API_KEY])
    return Result("", "", "firing" if cloud else "dead",
                  f"cloud keys configured: groq={bool(_cfg.GROQ_API_KEY)} "
                  f"cerebras={bool(_cfg.CEREBRAS_API_KEY)} samba={bool(_cfg.SAMBANOVA_API_KEY)}")


def check_F41_self_play() -> Result:
    """F41 Self-Play vs MarS. Two sides:
      (a) episodes are running        — brain:self_play_win_rate, games count
      (b) the LOB sim is producing real microstructure
         — self_play:last_mean_spread + last_mean_slippage_bps > 0 + recent ts

    Asymmetric guard: post-2026-05-21 (cont. 4) the GBM next_tick() was
    replaced with an agent-based LOB. If episodes run but spread/slippage
    are still zero, the LOB upgrade isn't actually being exercised."""
    val = r_get("brain:self_play_win_rate")
    games = r_get("brain:self_play_games")
    if val is None and not games:
        keys = r_keys("self_play:*") + r_keys("mars:*")
        if keys:
            return Result("", "", "firing", f"{len(keys)} self_play/mars keys")
        return Result("", "", "dead", "no self_play evidence")

    spread   = r_get("self_play:last_mean_spread")
    slippage = r_get("self_play:last_mean_slippage_bps")
    fills    = r_get("self_play:last_fills")
    last_ts  = r_get("self_play:last_episode_ts")

    if last_ts is None:
        return Result("", "", "stale",
                      f"win_rate={val} but LOB stats absent (sim probably still GBM)")

    age_h = (_now() - int(last_ts)) / 3600.0
    if age_h > 24:
        return Result("", "", "stale",
                      f"win_rate={val} but last LOB episode {round(age_h, 1)}h ago")

    if spread is None or float(spread) == 0:
        return Result("", "", "stale",
                      f"win_rate={val} but mean_spread=0 (book degenerate?)")

    # spread > 0 proves the LOB is producing real microstructure. Fills + slippage
    # require the strategy to actually engage with the book — that's policy-dependent
    # and not a LOB health signal. Surface them as evidence, don't gate on them.
    return Result("", "", "firing",
                  f"win_rate={val} games={games or 0} / "
                  f"spread={spread} slip_bps={slippage or 0} fills={fills or 0} "
                  f"age={round(age_h, 1)}h")


def check_F42_soar() -> Result:
    val = r_get("brain:paper_closed")
    if val is not None:
        return Result("", "", "firing", f"brain heartbeat, paper_closed={val}")
    return Result("", "", "dead", "brain:paper_closed missing")


def check_F43_metacog() -> Result:
    """F43 Metacognitive Monitor + L9 inline gate. Two sides per Rule 4:
      (a) competence_map populated (the model has data to compute confidence)
      (b) inline_decide:l9_* counters (the L9 gate in signals/engine.py is
          actually firing on signal evaluation)

    Counters being zero with confidence in the normal 50–100 band is OK
    (gate inactive by design); they only bump when brain:metacog_confidence
    drops into the caution band (<50) or rejection band (<20)."""
    cmap = r_get("brain:competence_map")
    gap = r_get("brain:priority_learning_gap")
    confidence = r_get("brain:metacog_confidence") or "n/a"
    l9_scales  = r_get("inline_decide:l9_scale_count") or "0"
    l9_rejects = r_get("inline_decide:l9_reject_count") or "0"
    l9_summary = (f"confidence={confidence} L9 gate scales={l9_scales} "
                  f"rejects={l9_rejects}")
    if cmap:
        return Result("", "", "firing",
                      f"competence_map len={len(cmap)} chars, "
                      f"gap={'set' if gap else 'unset'} | {l9_summary}")
    keys = r_keys("metacog:*") + r_keys("brain:metacog:*")
    if keys:
        return Result("", "", "firing",
                      f"{len(keys)} metacog keys | {l9_summary}")
    return Result("", "", "dead", f"no metacog evidence | {l9_summary}")


def check_F44_hedge() -> Result:
    """F44 Directional Hedge — evaluated on every losing-trade tick.
    Evidence keys are written by risk.hedge._mark_evaluated / _mark_opened."""
    eval_count = r_get("hedge:eval_count") or "0"
    open_count = r_get("hedge:open_count") or "0"
    last_eval_ts = r_get("hedge:last_eval_ts")
    last_open_ts = r_get("hedge:last_open_ts")
    last_reason = r_get("hedge:last_eval_reason")

    # Strongest evidence: actually opened a hedge (rare event, real fire).
    if last_open_ts:
        age = _now() - float(last_open_ts)
        status = "firing" if age <= WEEK else "stale"
        return Result("", "", status,
                      f"open_count={open_count} eval_count={eval_count} (last hedge {age/3600:.1f}h ago)",
                      age)
    # Next-best: evaluations are running. open_count=0 just means trigger conds
    # haven't all been met yet — that's the normal state most of the time.
    if last_eval_ts:
        age = _now() - float(last_eval_ts)
        status = "firing" if age <= DAY else "stale"
        return Result("", "", status,
                      f"eval_count={eval_count} last_reason={last_reason} (last eval {age/60:.1f}m ago, no hedge yet)",
                      age)
    # Module never invoked — either F44 inactive or no losing trades reached
    # the DCA-fired + extra-5% state.
    if _feature_inactive("F44"):
        return Result("", "", "no_check", "F44 inactive in governance")
    return Result("", "", "dead", "no hedge:* keys — no losing trade has reached the trigger threshold yet")


# ---------- log scanners ----------

def _check_brain_log(marker_regex: str, max_age: int) -> Result:
    return _check_container_log("trading-bot-brain-1", marker_regex, max_age)


def _check_celery_log(marker_regex: str, max_age: int) -> Result:
    return _check_container_log("trading-bot-celery_worker-1", marker_regex, max_age)


def _check_container_log(container: str, marker_regex: str, max_age: int) -> Result:
    import subprocess
    try:
        since_seconds = min(max_age, 7 * DAY)
        out = subprocess.check_output(
            ["docker", "logs", "--since", f"{since_seconds}s", container],
            stderr=subprocess.STDOUT, timeout=10,
        ).decode("utf-8", errors="replace")
        import re
        matches = re.findall(marker_regex, out)
        if matches:
            return Result("", "", "firing",
                          f"{len(matches)} matches of /{marker_regex}/ in {container} last "
                          f"{since_seconds//3600}h")
        return Result("", "", "dead",
                      f"no /{marker_regex}/ matches in {container} last {since_seconds//3600}h")
    except FileNotFoundError:
        return Result("", "", "no_check", "docker binary unavailable inside container")
    except Exception as exc:
        return Result("", "", "error", f"{type(exc).__name__}: {exc}")


# ---------- schema helpers ----------

def _feature_inactive(feature_id: str) -> bool:
    """True if feature_governance reports the feature as inactive (gated off)."""
    try:
        from feature_governance.registry import is_active
        return not is_active(feature_id)
    except Exception:
        return False


def _table_exists(name: str) -> bool:
    row = db_one(
        "SELECT 1 FROM information_schema.tables WHERE table_name=%s LIMIT 1",
        (name,),
    )
    return row is not None


def _column_exists(table: str, col: str) -> bool:
    row = db_one(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=%s AND column_name=%s LIMIT 1",
        (table, col),
    )
    return row is not None


# ---------- registry ----------

CHECKS: list[tuple[str, str, Callable[[], Result]]] = [
    ("F2",   "Trade Memory Pattern Mining",     check_F2_pattern_mining),
    ("F4",   "Dynamic Trailing SL",             check_F4_trailing_sl),
    ("F5",   "DCA Loss Recovery",               check_F5_dca),
    ("F8",   "Strategy Promotion",              check_F8_strategy_promote),
    ("F9",   "Rejected Signal Scanner",         check_F9_rejected_signals),
    ("F10",  "Pair Scanner",                    check_F10_pair_scanner),
    ("F12",  "Mismatch Decoder",                check_F12_mismatch_decoder),
    ("F13",  "Direction Prediction Model",      check_F13_direction),
    ("F14",  "HMM Regime Detection",            check_F14_hmm),
    ("F15",  "Market Microstructure VPIN/OFI",  check_F15_microstructure),
    ("F16",  "Fractional Kelly",                check_F16_kelly),
    ("F17",  "Continual Learning EWC",          check_F17_ewc),
    ("F18",  "Sentiment (CryptoBERT+FinBERT)",  check_F18_sentiment),
    ("F19",  "TFT Forecaster",                  check_F19_tft),
    ("F20",  "PatchTST Forecaster",             check_F20_patchtst),
    ("F21",  "Hierarchical MARL",               check_F21_marl),
    ("F22",  "Meta-RL MAML",                    check_F22_maml),
    ("F23",  "Mutual Information",              check_F23_mi),
    ("F24",  "GNN Correlation",                 check_F24_gnn),
    ("F25",  "Genetic Algorithm",               check_F25_ga),
    ("F26",  "BOCPD Changepoint",               check_F26_bocpd),
    ("F27",  "Transfer Entropy",                check_F27_transfer_entropy),
    ("F28",  "Turbulence Index",                check_F28_turbulence),
    ("F29",  "Web Intelligence",                check_F29_web_intel),
    ("F30",  "Feature Governance",              check_F30_governance),
    ("F31",  "Performance Analytics",           check_F31_analytics),
    ("F32",  "Account Risk Monitor",            check_F32_account_risk),
    ("F33",  "Watchdog",                        check_F33_watchdog),
    ("F34",  "World Model",                     check_F34_world_model),
    ("F35",  "MemRL Quality-Weighted Memory",   check_F35_memrl),
    ("F36",  "Strategy Research Engine",        check_F36_strategy_research),
    ("F37",  "Multi-Agent Debate Council",      check_F37_debate),
    ("F38",  "Curiosity Engine",                check_F38_curiosity),
    ("F39A", "OPRO Prompt Optimization",        check_F39A_opro),
    ("F39B", "DGM Code Rewriting",              check_F39B_dgm),
    ("F39C", "AI Scientist",                    check_F39C_ai_scientist),
    ("F40",  "LLM Layer",                       check_F40_llm_layer),
    ("F41",  "Self-Play vs MarS",               check_F41_self_play),
    ("F42",  "SOAR Cognitive Loop",             check_F42_soar),
    ("F43",  "Metacognitive Monitor",           check_F43_metacog),
    ("F44",  "Directional Hedge",               check_F44_hedge),
]


def run_all() -> list[Result]:
    redis_client.init()
    from db import init_pool
    init_pool()

    results: list[Result] = []
    for fid, name, fn in CHECKS:
        try:
            r = fn()
        except Exception as exc:
            r = Result("", "", "error", f"check raised: {type(exc).__name__}: {exc}")
        r.feature_id = fid
        r.name = name
        # cont. 68b: a governance-disabled feature must NOT show as firing/stale
        # just because its historical evidence keys/trades still exist. Overlay
        # a 'disabled' status so the panel reflects the real gate state — e.g.
        # F13/F35 were re-disabled to stop the mono-short regression but their
        # old direction_confidence variety / counters made them read "firing".
        if _feature_inactive(fid):
            r.status = "disabled"
            r.evidence = f"GATED OFF (governance is_active=False) — {r.evidence}"
        results.append(r)
    return results


def render_table(results: list[Result]) -> str:
    out = []
    out.append("| Feature | Name | Status | Evidence |")
    out.append("|---|---|---|---|")
    for r in results:
        evidence = r.evidence.replace("|", "/")
        out.append(f"| {r.feature_id} | {r.name} | {r.status} | {evidence} |")
    return "\n".join(out)


def render_summary(results: list[Result]) -> str:
    from collections import Counter
    c = Counter(r.status for r in results)
    total = len(results)
    out = [f"Total: {total} features"]
    for k in ("firing", "stale", "dead", "no_check", "error"):
        out.append(f"  {k:<10}: {c.get(k, 0)}")
    return "\n".join(out)


if __name__ == "__main__":
    results = run_all()
    table = render_table(results)
    summary = render_summary(results)
    print(summary)
    print()
    print(table)
    # Also dump JSON for downstream parsing
    out_path = "/tmp/feature_health.json"
    with open(out_path, "w") as fh:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": dict((s, sum(1 for r in results if r.status == s))
                            for s in ("firing", "stale", "dead", "no_check", "error")),
            "results": [asdict(r) for r in results],
        }, fh, indent=2)
    print(f"\nJSON written to {out_path}")
