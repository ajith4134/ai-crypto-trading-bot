"""
Section V (continued) — F35 MemRL Quality-Weighted Memory: Q-learning component.

Blueprint Feature 35 (arXiv:2601.03192) specifies a two-phase memory retrieval:
  Phase 1 — Semantic filter (cosine similarity over pgvector)
  Phase 2 — Quality re-ranking by Q-value

This module provides the Q-value side. State buckets are computed from trade
fields available at both retrieval time (from any candidate trade row) and
close time (from the trade being learned from), so backfill from history is
deterministic.

Update rule: constant-α Monte Carlo.
    Q(s,a) ← Q(s,a) + α · (r - Q(s,a))      α = 0.1
where r = clip(net_pnl_usdt / 50, -1, +1).

This is "constant-step MC" — each trade is a complete episode (terminal reward
= net_pnl_usdt). No TD bootstrapping is needed because trades don't chain into
multi-step decision sequences from a single Q-table's perspective. The α=0.1
choice trades unbiased convergence (which 1/(n+1) would give) for regime
responsiveness — if the market shifts, Q adapts in ~10 trades instead of
needing the same number again.

State bucket dimensions (deliberately small to ensure each bucket sees
adequate samples within the bot's lifetime):
    regime          : bull | bear | turbulent | unknown        (4)
    strength_tier   : low (<40) | med (40-70) | high (≥70)     (3)
    pair_class      : major (BTC/ETH/SOL) | alt (everything)   (2)
                                                       total: 24 distinct states

Action space:
    long | short    — the direction the trade actually took.

Per-bucket Q-table capacity: 24 × 2 = 48 (state, action) entries — easily
fills within a few hundred paper trades. Phase 2 retrieval falls back to raw
net_pnl_usdt for any candidate whose bucket has n_samples < 20 so low-data
buckets don't dominate by accident.
"""
from __future__ import annotations
import structlog
import redis_client
from db import db_conn

log = structlog.get_logger()


_ALPHA = 0.1       # MC step size — α=0.1 → effective half-life ~7 trades per bucket
_PNL_SCALE = 50.0  # net_pnl_usdt ÷ 50 → reward ∼ [-1, +1] (clipped)
_MIN_SAMPLES = 20  # phase 2 trusts Q only when bucket has at least this many updates

# cont. 62c — pair → 2-class delegated to risk.pair_classes. The hardcoded
# {"BTCUSDT","ETHUSDT","SOLUSDT"} set used to live here; now the live
# scanner-built bucket assignment (Redis `scanner:pair_bucket:{pair}`) is
# the source of truth. Q-table keys stay "major" / "alt" so existing
# learned values remain interpretable across the migration.


# ─────────────────────────────────────────────────────────────────────────────
# Bucketing — pure functions, deterministic, no side effects.
# Used identically at update-time and retrieval-time so backfill matches live.
# ─────────────────────────────────────────────────────────────────────────────

def _strength_tier(strength: float | None) -> str:
    """Discretize signal/trade_potential strength into 3 tiers."""
    if strength is None:
        return "low"
    try:
        s = float(strength)
    except (TypeError, ValueError):
        return "low"
    if s < 40:
        return "low"
    if s < 70:
        return "med"
    return "high"


def _pair_class(pair: str | None) -> str:
    """major vs alt — covers ~80% of trade volume in 2 classes.

    cont. 62c: backed by risk.pair_classes.class_2 (which reads the
    scanner-built bucket assignment). Was hardcoded to BTC/ETH/SOL pre-62c.
    """
    if not pair:
        return "alt"
    from risk.pair_classes import class_2
    return class_2(pair)


def _regime_norm(regime: str | None) -> str:
    """Canonicalize regime label. Anything unrecognised → 'unknown'."""
    if regime in ("bull", "bear", "turbulent"):
        return regime
    return "unknown"


def state_bucket(regime: str | None, pair: str | None,
                 strength: float | None) -> str:
    """Canonical state bucket key — used as the PK component for q_values.

    Returns 'regime|strength_tier|pair_class' — e.g. 'bull|high|major'.
    Deterministic from trade columns that exist on every trade row, so
    backfill from history produces the same buckets as live writes.
    """
    return f"{_regime_norm(regime)}|{_strength_tier(strength)}|{_pair_class(pair)}"


def reward_from_pnl(net_pnl_usdt: float | None) -> float:
    """Normalize net PnL into MC reward in [-1, +1]."""
    if net_pnl_usdt is None:
        return 0.0
    try:
        r = float(net_pnl_usdt) / _PNL_SCALE
    except (TypeError, ValueError):
        return 0.0
    if r > 1.0:
        return 1.0
    if r < -1.0:
        return -1.0
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Q-table writes — constant-α Monte Carlo update from a single closed trade.
# ─────────────────────────────────────────────────────────────────────────────

def update_q(bucket: str, action: str, reward: float, alpha: float = _ALPHA) -> dict:
    """Apply one MC update for (bucket, action). Upserts the row.

    Returns the new Q-value and updated sample count, plus 'inserted' flag.
    Logs evidence keys for feature_health.
    """
    if action not in ("long", "short"):
        return {"status": "skip_action_invalid", "action": action}

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT q_value, n_samples FROM q_values "
                "WHERE state_bucket = %s AND action = %s",
                (bucket, action),
            )
            row = cur.fetchone()
            if row is None:
                # First time seeing this bucket+action — initialise at the observed reward.
                # Equivalent to MC update from Q=0 with α=1 on the very first sample, then
                # α=0.1 afterwards. Avoids the "everything starts at 0" bias for buckets
                # that genuinely see only positive (or only negative) returns.
                new_q = float(reward)
                new_n = 1
                cur.execute(
                    "INSERT INTO q_values (state_bucket, action, q_value, n_samples, last_updated) "
                    "VALUES (%s, %s, %s, %s, NOW())",
                    (bucket, action, new_q, new_n),
                )
                inserted = True
            else:
                old_q, old_n = float(row[0]), int(row[1])
                new_q = old_q + alpha * (float(reward) - old_q)
                new_n = old_n + 1
                cur.execute(
                    "UPDATE q_values "
                    "SET q_value = %s, n_samples = %s, last_updated = NOW() "
                    "WHERE state_bucket = %s AND action = %s",
                    (new_q, new_n, bucket, action),
                )
                inserted = False

    _mark_update(bucket)
    return {
        "status": "updated",
        "bucket": bucket,
        "action": action,
        "q_value": round(new_q, 6),
        "n_samples": new_n,
        "inserted": inserted,
    }


def update_q_from_trade(trade: dict) -> dict:
    """Convenience wrapper — derive (bucket, action, reward) from a closed trade row
    and apply update_q. Used by both live close hook in memory/write.py and the
    one-shot backfill in tools/backfill_q_values.py."""
    bucket = state_bucket(
        regime=trade.get("market_regime"),
        pair=trade.get("pair"),
        strength=trade.get("trade_potential_score") or trade.get("direction_confidence"),
    )
    action = trade.get("direction")
    reward = reward_from_pnl(trade.get("net_pnl_usdt"))
    return update_q(bucket, action, reward)


# ─────────────────────────────────────────────────────────────────────────────
# Q-table reads — used by memrl._phase2_quality_rerank.
# ─────────────────────────────────────────────────────────────────────────────

def get_q(bucket: str, action: str) -> tuple[float, int]:
    """Return (q_value, n_samples) for a single (bucket, action). (0.0, 0) if absent."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT q_value, n_samples FROM q_values "
                "WHERE state_bucket = %s AND action = %s",
                (bucket, action),
            )
            row = cur.fetchone()
            if row is None:
                return 0.0, 0
            return float(row[0]), int(row[1])


def get_q_batch(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[float, int]]:
    """Batch lookup — one SQL query for N (bucket, action) lookups.

    Returns dict keyed by (bucket, action) → (q_value, n_samples). Missing
    entries are absent from the dict (caller must default).
    """
    if not pairs:
        return {}
    # Build a single VALUES clause for a tuple-IN match. Postgres handles
    # composite IN via (a,b) IN ((v1a, v1b), ...).
    with db_conn() as conn:
        with conn.cursor() as cur:
            from psycopg2.extras import execute_values
            # Use a temporary VALUES list joined via ANY for portability.
            placeholder = ",".join(["(%s,%s)"] * len(pairs))
            flat: list = []
            for b, a in pairs:
                flat.append(b)
                flat.append(a)
            sql = (
                "SELECT state_bucket, action, q_value, n_samples FROM q_values "
                f"WHERE (state_bucket, action) IN ({placeholder})"
            )
            cur.execute(sql, flat)
            out: dict[tuple[str, str], tuple[float, int]] = {}
            for b, a, q, n in cur.fetchall():
                out[(b, a)] = (float(q), int(n))
            return out


def is_q_trustworthy(n_samples: int) -> bool:
    """Phase 2 trusts a bucket's Q only when n_samples ≥ _MIN_SAMPLES."""
    return n_samples >= _MIN_SAMPLES


# ─────────────────────────────────────────────────────────────────────────────
# Evidence keys for feature_health.
# ─────────────────────────────────────────────────────────────────────────────

def _mark_update(bucket: str) -> None:
    """Durable evidence: total updates count, last update ts, distinct buckets."""
    try:
        import time as _t
        r = redis_client.get()
        r.incr("q_learning:updates_count")
        r.set("q_learning:last_update_ts", str(int(_t.time())))
        # Use a Redis SET for distinct buckets; cheap and bounded (max ~24 entries).
        r.sadd("q_learning:distinct_buckets", bucket)
    except Exception:
        pass


def get_evidence() -> dict:
    """Return current evidence-key snapshot for feature_health and dashboards."""
    try:
        r = redis_client.get()
        return {
            "updates_count": int(r.get("q_learning:updates_count") or 0),
            "last_update_ts": (int(r.get("q_learning:last_update_ts"))
                               if r.get("q_learning:last_update_ts") else None),
            "distinct_buckets": int(r.scard("q_learning:distinct_buckets") or 0),
        }
    except Exception:
        return {"updates_count": 0, "last_update_ts": None, "distinct_buckets": 0}
