"""
Blueprint F8 Strategy Router — per-strategy execution overrides.

Companion to `strategy/selector.py` (UCB1 selector). The selector picks which
strategy_id to attribute a trade to; this router translates that choice into
behavior by reading the strategy's typed rule columns (currently `dca_rules`)
and returning per-trade overrides.

Before 2026-05-21 (cont. 8), the strategy_id was a pure attribution tag — the
DCA thresholds came from `config.capital.dca_trigger_1_pct` regardless of
which strategy was picked. Wiring the router closes Rule-4 note #1 from
cont. 7: different strategies now actually trade differently.

Lookup is cached per strategy_id for 5 min to avoid hammering postgres on
every DCA-trigger check. The router fail-soft: any error returns None and
the caller falls back to the global config defaults.
"""
from __future__ import annotations

import json
import time
import structlog

import redis_client
from db import db_conn

log = structlog.get_logger()

_CACHE_TTL_SEC = 300   # 5 min — strategy rules don't churn fast


def get_dca_rules(strategy_id: str | None) -> dict | None:
    """Return `{"round_1_pct": <neg int>, "round_2_pct": <neg int>}` for the
    given strategy_id, or None when no strategy_id is set / no override exists.

    Pct values are NEGATIVE percentages (e.g. -20 for "trigger DCA round 1 at
    -20% from entry"). Caller divides by 100 before comparing to pct_move.
    """
    if not strategy_id:
        return None
    r = redis_client.get()
    cache_key = f"strategy:router:dca:{strategy_id}"
    cached = r.get(cache_key)
    if cached:
        if cached == "__NULL__":
            return None
        try:
            return json.loads(cached)
        except Exception:
            pass

    rules = None
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dca_rules FROM strategies WHERE id = %s",
                    (strategy_id,),
                )
                row = cur.fetchone()
        if row and row[0]:
            raw = row[0]
            data = raw if isinstance(raw, dict) else json.loads(raw)
            r1 = data.get("round_1_pct")
            r2 = data.get("round_2_pct")
            if r1 is not None and r2 is not None:
                rules = {"round_1_pct": float(r1), "round_2_pct": float(r2)}
    except Exception as exc:
        log.warning("strategy_router_dca_lookup_failed",
                    strategy_id=strategy_id, error=str(exc)[:120])

    try:
        if rules is None:
            r.setex(cache_key, _CACHE_TTL_SEC, "__NULL__")
        else:
            r.setex(cache_key, _CACHE_TTL_SEC, json.dumps(rules))
    except Exception:
        pass

    return rules


def get_entry_overrides(strategy_id: str | None) -> dict | None:
    """Return typed entry overrides for the given strategy_id, or None.

    Supported keys (all optional, any subset valid):
      min_signal_strength : float
      turbulence_cap      : float
      regime_whitelist    : list[str]   (signal rejected if regime not in list)

    Cached per-strategy in Redis for 5 min. NULL responses cached as
    "__NULL__" so we don't re-query strategies without overrides.
    """
    if not strategy_id:
        return None
    r = redis_client.get()
    cache_key = f"strategy:router:entry:{strategy_id}"
    cached = r.get(cache_key)
    if cached:
        if cached == "__NULL__":
            return None
        try:
            return json.loads(cached)
        except Exception:
            pass

    overrides = None
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT entry_overrides FROM strategies WHERE id = %s",
                    (strategy_id,),
                )
                row = cur.fetchone()
        if row and row[0]:
            raw = row[0]
            data = raw if isinstance(raw, dict) else json.loads(raw)
            if isinstance(data, dict) and data:
                overrides = data
    except Exception as exc:
        log.warning("strategy_router_entry_lookup_failed",
                    strategy_id=strategy_id, error=str(exc)[:120])

    try:
        if overrides is None:
            r.setex(cache_key, _CACHE_TTL_SEC, "__NULL__")
        else:
            r.setex(cache_key, _CACHE_TTL_SEC, json.dumps(overrides))
    except Exception:
        pass

    return overrides


def record_routed_entry_decision(strategy_id: str, accepted: bool, reason: str) -> None:
    """Observability counter: bump when accept_or_reject applies a
    strategy-specific override (vs config/GA defaults). Lets feature_health
    distinguish router-driven decisions from default-driven decisions."""
    try:
        r = redis_client.get()
        if accepted:
            r.incr("strategy_router:entry_accepted_count")
        else:
            r.incr("strategy_router:entry_rejected_count")
            r.set("strategy_router:entry_last_reject_reason", reason)
        r.set("strategy_router:entry_last_strategy_id", strategy_id)
        r.set("strategy_router:entry_last_ts", int(time.time()))
    except Exception:
        pass


def get_sl_overrides(strategy_id: str | None) -> dict | None:
    """Return typed trailing-SL overrides for the given strategy_id, or None.

    Supported keys (all optional, any subset valid):
      initial_atr_mult   : float  — ATR multiplier for initial SL distance
                                    (default 2.5 in compute_initial_sl)
      initial_min_pct    : float  — minimum SL distance as fraction of mark
                                    (default 0.03 = 3%)
      trailing_dist_pct  : float  — trailing distance as fraction of mark
                                    (default 0.02 = 2%)

    Cached per-strategy in Redis for 5 min. NULL responses cached as
    "__NULL__" so we don't re-query strategies without overrides.
    """
    return _get_typed_overrides(strategy_id, "trailing_sl_params", "sl")


def get_capital_overrides(strategy_id: str | None) -> dict | None:
    """Return typed capital-sizing overrides for the given strategy_id, or None.

    Supported keys (all optional):
      capital_pct_mult : float — multiplier on brain_state.default_capital_usdt
                                 (1.0 = no change, 0.5 = half, 1.5 = bigger).
                                 Clipped to [0.25, 2.0] at the consumer to
                                 prevent a runaway override from blowing the
                                 user's position-size band.
    """
    return _get_typed_overrides(strategy_id, "position_sizing_rules", "cap")


def _get_typed_overrides(strategy_id: str | None,
                          column: str, cache_tag: str) -> dict | None:
    """Shared loader: read one JSONB column from strategies, cache with
    NULL sentinel, fail-soft."""
    if not strategy_id:
        return None
    r = redis_client.get()
    cache_key = f"strategy:router:{cache_tag}:{strategy_id}"
    cached = r.get(cache_key)
    if cached:
        if cached == "__NULL__":
            return None
        try:
            return json.loads(cached)
        except Exception:
            pass

    overrides = None
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {column} FROM strategies WHERE id = %s",
                    (strategy_id,),
                )
                row = cur.fetchone()
        if row and row[0]:
            raw = row[0]
            data = raw if isinstance(raw, dict) else json.loads(raw)
            if isinstance(data, dict) and data:
                overrides = data
    except Exception as exc:
        log.warning("strategy_router_typed_lookup_failed",
                    column=column, strategy_id=strategy_id,
                    error=str(exc)[:120])

    try:
        if overrides is None:
            r.setex(cache_key, _CACHE_TTL_SEC, "__NULL__")
        else:
            r.setex(cache_key, _CACHE_TTL_SEC, json.dumps(overrides))
    except Exception:
        pass

    return overrides


def record_routed_sl(strategy_id: str, kind: str, value: float) -> None:
    """Observability counter for routed SL decisions."""
    try:
        r = redis_client.get()
        r.incr(f"strategy_router:sl_{kind}_count")
        r.set(f"strategy_router:sl_last_strategy_id", strategy_id)
        r.set(f"strategy_router:sl_last_value", float(value))
        r.set(f"strategy_router:sl_last_ts", int(time.time()))
    except Exception:
        pass


def record_routed_capital(strategy_id: str, mult: float, final_usdt: float) -> None:
    """Observability counter for routed capital sizing."""
    try:
        r = redis_client.get()
        r.incr("strategy_router:capital_routed_count")
        r.set("strategy_router:capital_last_strategy_id", strategy_id)
        r.set("strategy_router:capital_last_mult", float(mult))
        r.set("strategy_router:capital_last_final_usdt", float(final_usdt))
        r.set("strategy_router:capital_last_ts", int(time.time()))
    except Exception:
        pass


def record_routed_dca(strategy_id: str, round_num: int, rules: dict) -> None:
    """Observability counter: bump when a per-strategy DCA threshold actually
    fires (instead of the global config default). Distinguishes router-driven
    DCAs from config-default DCAs in feature_health."""
    try:
        r = redis_client.get()
        r.incr("strategy_router:dca_routed_count")
        r.set("strategy_router:dca_last_strategy_id", strategy_id)
        r.set("strategy_router:dca_last_round", round_num)
        r.set("strategy_router:dca_last_thresholds",
              json.dumps(rules))
        r.set("strategy_router:dca_last_ts", int(time.time()))
    except Exception:
        pass


# ============================================================================
# Overlay piggy-back (seed gene pool D family: vol_target / hmm_regime_gate)
#
# Two seeds in the 27-archetype gene pool are overlays, not standalone
# strategies: vol_target_sizing_overlay and hmm_regime_gate_overlay. They have
# no entry mechanism of their own — when the bandit picks them, the runtime
# applies their parameters on top of whatever directional signal fired.
#
# Implementation note: the bandit attribution is unchanged. When an overlay
# strategy_id is chosen by select_strategy(), trades are still attributed to
# the overlay (so it accrues paper_trade_count and can complete trial
# eligibility). The helpers below let consumers detect overlay strategies
# and apply the right modifier.
# ============================================================================


def get_overlay_metadata(strategy_id: str | None) -> dict | None:
    """Return overlay metadata for the strategy, or None if not an overlay.

    Returns dict with at minimum {"overlay_type": "sizing"|"regime_gate"}
    plus the type-specific params (target_realized_vol_pct, vol_lookback_bars,
    kill_in_turbulent, hmm_confidence_min, etc.).

    Reads `entry_overrides.overlay_type` to discriminate. Cached for 5 min via
    the same mechanism as get_entry_overrides.
    """
    if not strategy_id:
        return None
    overrides = get_entry_overrides(strategy_id)
    if not overrides:
        return None
    overlay_type = overrides.get("overlay_type")
    if not overlay_type:
        return None
    return {k: v for k, v in overrides.items()
            if k in ("overlay_type", "target_realized_vol_pct",
                     "vol_lookback_bars", "kill_in_turbulent",
                     "hmm_confidence_min")}


def apply_vol_target_sizing(capital_usdt: float, pair: str,
                            target_vol_pct: float = 0.20,
                            lookback_bars: int = 1440) -> tuple[float, str]:
    """Scale capital so realized-vol contribution matches target.

    Returns (new_capital_usdt, reason_tag). Falls back to input capital when
    realized vol can't be computed (silent-rejection rule: emit counter +
    reason instead of crashing). Per [[feedback_silent_rejection]] every
    skip path must emit a log + counter.
    """
    try:
        r = redis_client.get()
        # Look up rolling realized vol from existing dvol/atr cache.
        realized_vol_key = f"{pair}:realized_vol_pct"
        realized = r.get(realized_vol_key)
        if realized is None:
            r.incr("strategy_router:overlay_vol_skipped_count")
            r.set("strategy_router:overlay_vol_last_skip_reason", "no_realized_vol")
            return capital_usdt, "no_realized_vol"
        realized = float(realized)
        if realized <= 0:
            r.incr("strategy_router:overlay_vol_skipped_count")
            r.set("strategy_router:overlay_vol_last_skip_reason", "zero_vol")
            return capital_usdt, "zero_vol"
        # Scale: bigger when realized < target, smaller when realized > target.
        # Clipped to [0.25x, 2.0x] same band as F8 capital router (cont. 13).
        scale = max(0.25, min(2.0, target_vol_pct / realized))
        new_capital = max(5.0, round(capital_usdt * scale, 2))
        r.incr("strategy_router:overlay_vol_applied_count")
        r.set("strategy_router:overlay_vol_last_scale", float(scale))
        r.set("strategy_router:overlay_vol_last_pair", pair)
        r.set("strategy_router:overlay_vol_last_ts", int(time.time()))
        return new_capital, "applied"
    except Exception as exc:
        log.warning("overlay_vol_target_failed", error=str(exc)[:120])
        try:
            redis_client.get().incr("strategy_router:overlay_vol_error_count")
        except Exception:
            pass
        return capital_usdt, "error"


def check_hmm_regime_kill(hmm_confidence_min: float = 0.7,
                          kill_in_turbulent: bool = True) -> tuple[bool, str]:
    """Return (should_block, reason). When True, entry should be rejected
    or open position force-closed.

    Reads current HMM regime + confidence from Redis (`hmm:current_regime`,
    `hmm:current_confidence`). Falls back to allow-entry when HMM unavailable
    (silent-rejection counter logged).
    """
    try:
        r = redis_client.get()
        regime = r.get("hmm:current_regime")
        conf = r.get("hmm:current_confidence")
        if regime is None or conf is None:
            r.incr("strategy_router:overlay_hmm_skipped_count")
            r.set("strategy_router:overlay_hmm_last_skip_reason", "hmm_unavailable")
            return False, "hmm_unavailable"
        conf = float(conf)
        if kill_in_turbulent and regime == "turbulent" and conf >= hmm_confidence_min:
            r.incr("strategy_router:overlay_hmm_kill_count")
            r.set("strategy_router:overlay_hmm_last_kill_regime", regime)
            r.set("strategy_router:overlay_hmm_last_kill_conf", float(conf))
            r.set("strategy_router:overlay_hmm_last_kill_ts", int(time.time()))
            return True, f"hmm_turbulent_conf_{conf:.2f}"
        return False, "allowed"
    except Exception as exc:
        log.warning("overlay_hmm_gate_failed", error=str(exc)[:120])
        try:
            redis_client.get().incr("strategy_router:overlay_hmm_error_count")
        except Exception:
            pass
        return False, "error"


def record_overlay_piggyback(overlay_strategy_id: str,
                             overlay_type: str,
                             outcome: str) -> None:
    """Counter for overlay piggy-back events. outcome ∈ {"applied",
    "skipped", "blocked", "error"}. Per [[feedback_silent_rejection]]."""
    try:
        r = redis_client.get()
        r.incr(f"strategy_router:overlay_piggyback_{outcome}_count")
        r.set("strategy_router:overlay_piggyback_last_id", overlay_strategy_id)
        r.set("strategy_router:overlay_piggyback_last_type", overlay_type)
        r.set("strategy_router:overlay_piggyback_last_outcome", outcome)
        r.set("strategy_router:overlay_piggyback_last_ts", int(time.time()))
    except Exception:
        pass
