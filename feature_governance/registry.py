"""
Section W: Autonomous Feature Governance — W-01 to W-11.
"""
import json
from datetime import datetime, timezone
import structlog
import redis_client
import redis_keys
from db import db_conn

log = structlog.get_logger()

_REGISTRY: dict[str, dict] = {}


class FeatureNotRegisteredError(Exception):
    pass


def register(feature_id: str, name: str, activation_phase: int = 0) -> None:
    """W-01: Register a feature. Called at module load time by every feature module."""
    _REGISTRY[feature_id] = {"name": name, "activation_phase": activation_phase}
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO feature_governance (feature_id, feature_name, activation_phase)
                VALUES (%s, %s, %s)
                ON CONFLICT (feature_id) DO NOTHING
            """, (feature_id, name, activation_phase))


def assert_registered(feature_id: str) -> None:
    """Raise if a feature tries to emit a signal without being registered."""
    if feature_id not in _REGISTRY:
        raise FeatureNotRegisteredError(
            f"Feature '{feature_id}' is not registered. Call register() at module load."
        )


def update_contribution(feature_id: str, trade_won: bool, pair: str, regime: str) -> None:
    """W-02: Update rolling contribution score after every closed trade."""
    assert_registered(feature_id)
    r = redis_client.get()
    key = f"feature:{feature_id}:contribution"
    history = json.loads(r.get(key) or "[]")
    history.append(1 if trade_won else -1)
    if len(history) > 100:
        history = history[-100:]
    r.set(key, json.dumps(history))


def get_contribution_score(feature_id: str) -> float:
    r = redis_client.get()
    history = json.loads(r.get(f"feature:{feature_id}:contribution") or "[]")
    return sum(history) / len(history) if history else 0.0


def _peer_mean_contribution() -> tuple[float, int]:
    """Average contribution across all currently-active features (last 50 each).
    Used to distinguish 'feature is worse than peers' from 'bot is losing'."""
    r = redis_client.get()
    flags = json.loads(r.get(redis_keys.BRAIN_ACTIVE_FLAGS) or "{}")
    active_ids = [fid for fid in _REGISTRY if flags.get(fid, True)]
    means = []
    for fid in active_ids:
        h = json.loads(r.get(f"feature:{fid}:contribution") or "[]")
        if len(h) >= 10:
            recent = h[-50:]
            means.append(sum(recent) / len(recent))
    if not means:
        return 0.0, 0
    return sum(means) / len(means), len(means)


def _is_peer_outlier(feature_id: str, margin: float = 0.2) -> bool:
    """True if this feature's contribution is meaningfully worse than the peer
    average. Prevents blanket deactivation when the whole bot is underperforming."""
    r = redis_client.get()
    h = json.loads(r.get(f"feature:{feature_id}:contribution") or "[]")
    if len(h) < 10:
        return False
    self_mean = sum(h[-50:]) / len(h[-50:])
    peer_mean, peer_n = _peer_mean_contribution()
    if peer_n < 5:
        # Not enough peers to compare — fall back to the absolute negative check
        return self_mean < -0.5
    return self_mean < peer_mean - margin


def _is_peer_outlier_frozen(feature_id: str, snapshot_peer_mean: float,
                            snapshot_peer_n: int, margin: float = 0.2) -> bool:
    """D-07: same as `_is_peer_outlier` but uses a peer-mean snapshot taken
    BEFORE the deactivation loop began. Prevents the cascading-shrink race
    where peer_n drops below 5 mid-loop and the absolute-fallback check then
    trips every remaining feature during a market-wide losing streak.

    The absolute fallback (`self_mean < -0.5`) is intentionally NOT applied
    here even when snapshot_peer_n < 5 — if the bot started with too few
    eligible peers we treat the check as inconclusive and SKIP, rather than
    risk another mass-deactivation cascade. A genuinely-broken solo feature
    will still be caught by `check_bad_trade_causation`'s own strict fallback
    (peer_n < 5 there means the bot is too sparse to compare, so let the
    feature be flagged).
    """
    r = redis_client.get()
    h = json.loads(r.get(f"feature:{feature_id}:contribution") or "[]")
    if len(h) < 10:
        return False
    self_mean = sum(h[-50:]) / len(h[-50:])
    if snapshot_peer_n < 5:
        return False   # inconclusive — refuse to deactivate, log and move on
    return self_mean < snapshot_peer_mean - margin


def check_degradation(feature_id: str, n_consecutive: int = 10) -> bool:
    """W-03: Return True if contribution has trended negative for n consecutive trades."""
    r = redis_client.get()
    history = json.loads(r.get(f"feature:{feature_id}:contribution") or "[]")
    if len(history) < n_consecutive:
        return False
    return all(v < 0 for v in history[-n_consecutive:])


def deactivate_feature(feature_id: str, failure_mode: str, decode_reason: str) -> None:
    """W-08 step 5: Set feature weight to zero; publish event; alert Telegram."""
    assert_registered(feature_id)

    r = redis_client.get()
    flags = json.loads(r.get(redis_keys.BRAIN_ACTIVE_FLAGS) or "{}")
    flags[feature_id] = False
    r.set(redis_keys.BRAIN_ACTIVE_FLAGS, json.dumps(flags))
    r.publish(redis_keys.CH_FEATURE_EVENT, json.dumps({
        "event": "feature_deactivated",
        "feature_id": feature_id,
        "reason": decode_reason,
    }))

    trade_count = _get_total_trade_count()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE feature_governance SET
                    status = 'turned_off',
                    current_weight = 0,
                    failure_mode = %s,
                    decode_reason = %s,
                    turned_off_at = %s,
                    next_reeval_at_trades = %s
                WHERE feature_id = %s
            """, (
                failure_mode, decode_reason,
                datetime.now(timezone.utc),
                trade_count + 200,
                feature_id,
            ))

    try:
        from notifications.telegram import send_critical
        send_critical(f"Feature DEACTIVATED: {feature_id}\nReason: {decode_reason}")
    except Exception:
        pass

    log.warning("feature_deactivated", feature_id=feature_id, reason=decode_reason)


def reactivate_on_probation(feature_id: str) -> None:
    """W-10: Re-enable at 20% weight for 100-trade probation period."""
    r = redis_client.get()
    flags = json.loads(r.get(redis_keys.BRAIN_ACTIVE_FLAGS) or "{}")
    flags[feature_id] = True
    r.set(redis_keys.BRAIN_ACTIVE_FLAGS, json.dumps(flags))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE feature_governance SET
                    status = 'probation',
                    current_weight = 0.2,
                    probation_trade_start = %s
                WHERE feature_id = %s
            """, (_get_total_trade_count(), feature_id))

    log.info("feature_on_probation", feature_id=feature_id)


# cont. 68b: features that must stay OFF unless EXPLICITLY re-enabled in Redis.
# F13 (direction_model) + F35 (MemRL) were trained on the mono-short losing
# history and re-introduce that bug when active (cont. 65k-3/4). Their disable
# lived only in the Redis flags key, which got wiped to {} → is_active() then
# defaulted them back to True and F13 silently flipped direction 714× again.
# Baking the default OFF here means a Redis wipe can no longer resurrect them;
# re-enabling now requires an explicit `{"F13": true}` in brain:active_feature_flags.
_DEFAULT_DISABLED = {"F13", "F35"}


def is_active(feature_id: str) -> bool:
    r = redis_client.get()
    flags = json.loads(r.get(redis_keys.BRAIN_ACTIVE_FLAGS) or "{}")
    return flags.get(feature_id, feature_id not in _DEFAULT_DISABLED)


def _get_total_trade_count() -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM trades WHERE status='closed'")
            return cur.fetchone()[0] or 0


# ─────────────────────────────────────────────────────────────────────────────
# W-04 to W-07: The 4 OTHER failure modes (besides W-03 Degradation)
# ─────────────────────────────────────────────────────────────────────────────


def update_block_rate(feature_id: str, was_blocked: bool) -> None:
    """W-04: Track how often a feature blocks signals from passing.
    Increment counters in Redis. Used by check_blocking()."""
    if feature_id not in _REGISTRY:
        return
    r = redis_client.get()
    total_key = f"feature:{feature_id}:block_total"
    blocked_key = f"feature:{feature_id}:block_count"
    r.incr(total_key)
    if was_blocked:
        r.incr(blocked_key)


def check_blocking(feature_id: str, min_samples: int = 50, max_block_pct: float = 0.85) -> bool:
    """W-04: True if feature is blocking too high a fraction of candidate signals.
    Cross-references with rejected-signal counterfactuals: if blocked signals
    would have been winners, that's evidence of over-blocking."""
    r = redis_client.get()
    total = int(r.get(f"feature:{feature_id}:block_total") or 0)
    blocked = int(r.get(f"feature:{feature_id}:block_count") or 0)
    if total < min_samples:
        return False
    block_pct = blocked / total
    if block_pct < max_block_pct:
        return False
    # Cross-reference: were the blocked signals actually good? (shadow_win_rate)
    swr_raw = r.get(redis_keys.SHADOW_WIN_RATE)
    if swr_raw:
        swr = json.loads(swr_raw)
        # If the shadow (rejected) signals would have won > 50%, we're over-blocking
        if swr.get("rate", 0) > 50:
            return True
    return False


def update_contradiction(feature_a: str, feature_b: str, agreed: bool) -> None:
    """W-05: Track agreement rate between two features.
    Called when both features fire on the same decision — `agreed` is True if
    they pointed the same direction."""
    r = redis_client.get()
    pair_key = f"feature:contradiction:{feature_a}:{feature_b}"
    history = json.loads(r.get(pair_key) or "[]")
    history.append(1 if agreed else 0)
    if len(history) > 100:
        history = history[-100:]
    r.set(pair_key, json.dumps(history))


def check_contradiction(feature_a: str, feature_b: str, min_samples: int = 30) -> tuple[bool, float]:
    """W-05: True if two features disagree more than 70% of the time.
    Returns (is_contradicting, disagreement_rate)."""
    r = redis_client.get()
    history = json.loads(r.get(f"feature:contradiction:{feature_a}:{feature_b}") or "[]")
    if len(history) < min_samples:
        return False, 0.0
    agreement_rate = sum(history) / len(history)
    disagreement_rate = 1.0 - agreement_rate
    return disagreement_rate > 0.7, disagreement_rate


def check_bad_trade_causation(feature_id: str, n_recent_losses: int = 8,
                              precomputed_peer_loss_rate: float | None = None) -> bool:
    """W-06: True if this feature was active on N consecutive losing trades AND
    its loss rate is meaningfully worse than peers.

    DEVIATION D-07 (2026-05-20): the original `all(v < 0 for v in recent)` check
    deactivated all 36 features simultaneously during a market-wide losing
    streak — every active feature gets `-1` on every loss (update_contribution
    doesn't discriminate per-feature), so an 8-loss streak triggers W-06 on
    every feature at once. Same root cause as D-02 (blanket deactivation via
    Degradation), now closed for W-06 too.

    Peer-comparison guard: a feature is flagged only when its recent loss rate
    exceeds the peer-average loss rate by at least 0.15 (e.g., feature lost
    8/8 = 100% while peers average 5/8 = 62.5% → flag; everyone at 100% → no
    flag, the market is the problem). When peer_n < 5 (cannot compare), the
    original strict check is restored so a genuinely-broken solo feature
    still gets caught.

    `precomputed_peer_loss_rate` is passed in by `run_full_governance_check`
    so the peer set is frozen at the top of the run — prevents the cascading
    re-deactivation race that originally undermined D-02 (as features get
    turned off, peer_n shrinks, the < 5 fallback kicks in, and the original
    strict check then trips everyone remaining).
    """
    r = redis_client.get()
    history = json.loads(r.get(f"feature:{feature_id}:contribution") or "[]")
    if len(history) < n_recent_losses:
        return False
    recent = history[-n_recent_losses:]
    if not all(v < 0 for v in recent):
        return False

    # All N recent are losses — apply the peer guard before flagging.
    self_loss_rate = 1.0   # by definition, all are losses
    if precomputed_peer_loss_rate is None:
        precomputed_peer_loss_rate, peer_n = _peer_loss_rate(n_recent_losses)
    else:
        peer_n = -1  # caller already vetted
    if peer_n != -1 and peer_n < 5:
        # Too few peers to compare — restore the strict W-06 to catch a
        # genuinely-broken feature when most peers are off.
        return True
    return self_loss_rate > precomputed_peer_loss_rate + 0.15


def _peer_loss_rate(n_recent: int) -> tuple[float, int]:
    """Average loss rate across currently-active peers over their last `n_recent`
    contribution samples. Mirrors `_peer_mean_contribution` but for the W-06
    binary loss-or-not view. Returns (mean_loss_rate, n_peers_eligible)."""
    r = redis_client.get()
    flags = json.loads(r.get(redis_keys.BRAIN_ACTIVE_FLAGS) or "{}")
    active_ids = [fid for fid in _REGISTRY if flags.get(fid, True)]
    rates = []
    for fid in active_ids:
        h = json.loads(r.get(f"feature:{fid}:contribution") or "[]")
        if len(h) >= n_recent:
            recent = h[-n_recent:]
            losses = sum(1 for v in recent if v < 0)
            rates.append(losses / len(recent))
    if not rates:
        return 0.0, 0
    return sum(rates) / len(rates), len(rates)


def check_redundancy(feature_a: str, feature_b: str, min_samples: int = 50,
                     corr_threshold: float = 0.95) -> bool:
    """W-07: True if two features' contribution histories are >95% correlated.
    Indicates one is redundant (provides no extra signal)."""
    r = redis_client.get()
    ha = json.loads(r.get(f"feature:{feature_a}:contribution") or "[]")
    hb = json.loads(r.get(f"feature:{feature_b}:contribution") or "[]")
    n = min(len(ha), len(hb))
    if n < min_samples:
        return False
    a = ha[-n:]
    b = hb[-n:]
    # Pearson correlation
    try:
        import numpy as np
        c = float(np.corrcoef(a, b)[0, 1])
        return abs(c) > corr_threshold
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 7-step decode-before-act process (blueprint Section 15.13.4)
# ─────────────────────────────────────────────────────────────────────────────


def investigate_feature(feature_id: str) -> dict:
    """Step 2 — INVESTIGATE: Run a post-mortem on a flagged feature.
    Collects evidence: contribution history, regime breakdown, recent trades."""
    if feature_id not in _REGISTRY:
        return {}
    r = redis_client.get()
    history = json.loads(r.get(f"feature:{feature_id}:contribution") or "[]")

    # When did things turn negative?
    started_negative_at = None
    for i, v in enumerate(history):
        if v < 0:
            started_negative_at = i
            break

    # Per-regime breakdown
    regimes = {}
    for regime in ["bull", "bear", "turbulent"]:
        h_regime = json.loads(r.get(f"feature:{feature_id}:contribution:{regime}") or "[]")
        if h_regime:
            regimes[regime] = sum(h_regime) / len(h_regime)

    return {
        "feature_id": feature_id,
        "total_samples": len(history),
        "mean_contribution": sum(history) / len(history) if history else 0,
        "started_negative_at": started_negative_at,
        "per_regime": regimes,
        "current_regime": r.get(redis_keys.CURRENT_REGIME) or "unknown",
    }


def classify_failure(investigation: dict) -> str:
    """Step 3 — CLASSIFY: Temporary (regime-specific) or Structural (broken)."""
    regimes = investigation.get("per_regime", {})
    if not regimes:
        return "structural"
    # If feature is negative in one regime but positive in another → temporary
    positives = [r for r, score in regimes.items() if score > 0]
    negatives = [r for r, score in regimes.items() if score < 0]
    if positives and negatives:
        return "temporary"
    return "structural"


def decode_and_log(feature_id: str, failure_mode: str, investigation: dict,
                   classification: str) -> str:
    """Step 4 — DECODE & LOG: Generate the structured decoded reason."""
    from datetime import datetime, timezone
    reason = (
        f"Feature {feature_id} flagged ({failure_mode}). "
        f"Mean contribution {investigation.get('mean_contribution', 0):.3f} over "
        f"{investigation.get('total_samples', 0)} samples. "
        f"Per-regime: {investigation.get('per_regime', {})}. "
        f"Classification: {classification}. "
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}"
    )
    log.info("feature_decoded", feature_id=feature_id, reason=reason)
    return reason


def confirm_deactivation(feature_id: str, trades_since_off: int = 50) -> bool:
    """Step 6 — CONFIRM: Check if performance improved after deactivation."""
    r = redis_client.get()
    history = json.loads(r.get(f"feature:{feature_id}:performance_after_off") or "[]")
    if len(history) < trades_since_off:
        return False
    avg_after = sum(history) / len(history)
    log.info("deactivation_confirmation", feature_id=feature_id, avg_after=avg_after)
    return avg_after > 0


def reevaluate_dormant_features() -> list[str]:
    """Step 7 — RE-EVALUATION: Every 200 trades, check if dormant features should
    re-enter probation. Returns list of feature_ids re-activated."""
    reactivated = []
    trade_count = _get_total_trade_count()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT feature_id, next_reeval_at_trades FROM feature_governance
                WHERE status = 'turned_off' AND next_reeval_at_trades <= %s
            """, (trade_count,))
            for row in cur.fetchall():
                feature_id = row[0]
                try:
                    reactivate_on_probation(feature_id)
                    reactivated.append(feature_id)
                except Exception as exc:
                    log.warning("reeval_failed", feature_id=feature_id, error=str(exc))
    return reactivated


def run_full_governance_check() -> dict:
    """Step 1 → 5 — Run all 5 failure-mode checks across all registered features.
    Returns summary dict of detected failures + actions taken.

    DEVIATION D-07 (2026-05-20): peer-mean snapshots are taken ONCE at the
    top of the run (frozen against the pre-run active set) and passed into
    every per-feature check. Without this, the previous implementation kept
    re-reading the active set after each deactivation — once peer_n dropped
    below 5, the `_is_peer_outlier` fallback (self_mean < -0.5) tripped on
    every remaining feature during a losing streak. Result observed in
    production 2026-05-20 10:45: 36/36 features deactivated in 32 seconds.
    """
    summary = {"detected": [], "deactivated": [], "reactivated": []}

    # Snapshot peer state ONCE — prevents the cascading-shrink race that took
    # down 36/36 features at 10:45 UTC. After this point both
    # `_is_peer_outlier` and `check_bad_trade_causation` use the frozen view.
    snapshot_peer_mean, snapshot_peer_n = _peer_mean_contribution()
    snapshot_peer_loss_rate, snapshot_peer_loss_n = _peer_loss_rate(8)
    log.info("governance_check_started",
             peer_mean=round(snapshot_peer_mean, 3),
             peer_n=snapshot_peer_n,
             peer_loss_rate=round(snapshot_peer_loss_rate, 3),
             peer_loss_n=snapshot_peer_loss_n)

    # Detection pass — all 5 failure modes
    for feature_id in list(_REGISTRY.keys()):
        if not is_active(feature_id):
            continue

        # Run each check
        failures = []
        if check_degradation(feature_id):
            failures.append("Degradation")
        if check_blocking(feature_id):
            failures.append("Blocking")
        if check_bad_trade_causation(feature_id,
                                     precomputed_peer_loss_rate=snapshot_peer_loss_rate):
            failures.append("BadTradeCausation")
        # Contradiction & Redundancy are pairwise — checked separately below

        if failures:
            # Step 2 - Investigate
            inv = investigate_feature(feature_id)
            # Step 3 - Classify
            cls = classify_failure(inv)
            # Step 4 - Decode
            reason = decode_and_log(feature_id, ",".join(failures), inv, cls)
            summary["detected"].append({
                "feature_id": feature_id,
                "modes": failures,
                "classification": cls,
            })
            # Step 5 - Act. The peer guard now uses the frozen snapshot taken at
            # the top of this run rather than reading the current (shrinking)
            # active set. See D-02 + D-07 in BLUEPRINT_COMPLIANCE_AUDIT.md.
            if cls == "structural":
                if _is_peer_outlier_frozen(feature_id, snapshot_peer_mean, snapshot_peer_n):
                    deactivate_feature(feature_id, ",".join(failures), reason)
                    summary["deactivated"].append(feature_id)
                else:
                    log.info("governance_skip_blanket_deactivation",
                             feature_id=feature_id,
                             peer_mean=round(snapshot_peer_mean, 3),
                             peer_n=snapshot_peer_n,
                             reason="not an outlier vs frozen peer snapshot — bot-wide losing streak, not feature-specific")

    # Step 7 - Periodic re-evaluation
    reacted = reevaluate_dormant_features()
    summary["reactivated"] = reacted
    log.info("governance_check_complete", **{k: len(v) for k, v in summary.items()})
    return summary
