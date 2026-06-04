"""F2 / Blueprint Feature 2 — Trade Memory Pattern Mining.

Blueprint Feature 2 (line 140-141): "Every closed trade is saved in full detail …
The AI mines this database to identify what winning trades have in common vs losing
trades — finding patterns in timing, market structure, indicator combinations, pair
behaviour, and more."

This module covers all four blueprint-named dimensions plus derived insights:

  Section 1 — Coarse buckets (timing, market structure, pair):
              by (regime, direction), per-pair, sentiment, hour-of-day,
              weekday, hold-duration, brain_stage

  Section 2 — Multi-dim INDICATOR COMBINATIONS (mini-Apriori, no external deps):
              all 2-way and 3-way combinations of (regime, direction, sentiment,
              stage) with at least min_n samples; ranked by lift from 50% win rate

  Section 3 — WINNERS vs LOSERS comparative analysis:
              per-feature mean ± std for winners vs losers; Welch's t-test
              (scipy.stats.ttest_ind) for significance; Cohen's d for effect size

  Section 4 — Strategy-level rollup + DCA effectiveness

  Section 5 — Derived plain-English insights (top 5) for LLM / dashboard consumption

Output: Redis key brain:patterns_mined (JSON). Scheduled via Celery beat every 6h.

NOT covered (deliberately scoped out — would need new data capture):
- Hold-time / SL movement patterns at trade-level granularity (only bucketed here).
- True association rule mining beyond 3-way (apriori with confidence/lift would need
  a proper library; mini-apriori covers the practical actionable subset).
- Time-series cluster analysis (would need DTW or similar — overkill at 500 trades).
"""
import json
import math
from datetime import datetime, timezone
from collections import defaultdict
from itertools import combinations
import structlog
import redis_client
from db import db_conn

log = structlog.get_logger()

_RECENT_LIMIT          = 500
_MIN_BUCKET_N          = 5
_MIN_COMBO_2WAY_N      = 10
_MIN_COMBO_3WAY_N      = 15
_MIN_FEATURE_TTEST_N   = 20  # min per side for valid t-test
_TOP_N_PAIRS           = 10
_TOP_N_COMBOS          = 20
_MAX_INSIGHTS          = 5


# ─────────────────────────────────────────────────────────────────────────────
# Bucket helpers
# ─────────────────────────────────────────────────────────────────────────────

def _empty_bucket() -> dict:
    return {"wins": 0, "losses": 0, "pnl_sum": 0.0, "pnl_sq_sum": 0.0}


def _add(bucket: dict, won: bool, pnl: float) -> None:
    bucket["wins" if won else "losses"] += 1
    bucket["pnl_sum"] += pnl
    bucket["pnl_sq_sum"] += pnl * pnl


def _records(buckets: dict, key_name: str, min_n: int = _MIN_BUCKET_N) -> list[dict]:
    out = []
    for k, v in buckets.items():
        n = v["wins"] + v["losses"]
        if n < min_n:
            continue
        wr = v["wins"] / n
        avg = v["pnl_sum"] / n
        # Sample variance from sum and sum-of-squares
        var = max(0.0, v["pnl_sq_sum"] / n - avg * avg)
        out.append({
            key_name: k,
            "n": n,
            "wins": v["wins"],
            "losses": v["losses"],
            "win_rate": round(wr, 3),
            "avg_pnl": round(avg, 3),
            "std_pnl": round(math.sqrt(var), 3),
            "lift_from_neutral": round(abs(wr - 0.5), 3),
        })
    return out


def _sentiment_bucket(s: float) -> str:
    if s < 0.4:
        return "fear"
    if s < 0.6:
        return "neutral"
    return "greed"


def _hour_bucket(ts) -> str:
    if ts is None:
        return "unknown"
    try:
        return f"h{ts.hour:02d}"
    except Exception:
        return "unknown"


def _weekday_bucket(ts) -> str:
    if ts is None:
        return "unknown"
    try:
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][ts.weekday()]
    except Exception:
        return "unknown"


def _duration_bucket(seconds) -> str:
    if seconds is None:
        return "unknown"
    try:
        s = int(seconds)
    except Exception:
        return "unknown"
    if s < 3600:
        return "0-1h"
    if s < 14400:
        return "1-4h"
    if s < 86400:
        return "4-24h"
    return ">24h"


# ─────────────────────────────────────────────────────────────────────────────
# Section 1 — Coarse buckets (timing, market structure, pair)
# ─────────────────────────────────────────────────────────────────────────────

def _coarse_buckets(trades: list[dict]) -> dict:
    """Single-dimension buckets across blueprint's named dimensions."""
    dim_defs = [
        ("by_regime_direction", "context"),
        ("by_pair",             "pair"),
        ("by_sentiment",        "sentiment_bucket"),
        ("by_hour",             "hour"),
        ("by_weekday",          "weekday"),
        ("by_duration",         "duration"),
        ("by_brain_stage",      "stage"),
    ]
    dims = {name: defaultdict(_empty_bucket) for name, _ in dim_defs}

    for t in trades:
        net_pnl = float(t.get("net_pnl_usdt") or 0)
        won = net_pnl > 0
        regime    = t.get("market_regime") or "unknown"
        direction = t.get("direction")     or "unknown"
        pair      = t.get("pair")          or "unknown"
        entry_ts  = t.get("entry_time")
        hold      = t.get("hold_time_seconds")
        stage     = t.get("brain_stage")
        sent      = t.get("_sentiment")

        _add(dims["by_regime_direction"][f"{regime}/{direction}"], won, net_pnl)
        _add(dims["by_pair"][pair], won, net_pnl)
        if sent is not None:
            _add(dims["by_sentiment"][_sentiment_bucket(sent)], won, net_pnl)
        _add(dims["by_hour"][_hour_bucket(entry_ts)], won, net_pnl)
        _add(dims["by_weekday"][_weekday_bucket(entry_ts)], won, net_pnl)
        _add(dims["by_duration"][_duration_bucket(hold)], won, net_pnl)
        if stage is not None:
            _add(dims["by_brain_stage"][f"stage{stage}"], won, net_pnl)

    out: dict = {}
    for name, key_name in dim_defs:
        records = _records(dims[name], key_name)
        if name == "by_pair":
            records.sort(key=lambda x: x["win_rate"], reverse=True)
            out["top_pairs"] = records[:_TOP_N_PAIRS]
            if len(records) > _TOP_N_PAIRS:
                out["worst_pairs"] = list(reversed(records[-_TOP_N_PAIRS:]))
            else:
                out["worst_pairs"] = []
        else:
            records.sort(key=lambda x: x["win_rate"])
            out[name] = records
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Section 2 — Multi-dim INDICATOR COMBINATIONS (mini-Apriori)
# ─────────────────────────────────────────────────────────────────────────────

def _combo_buckets(trades: list[dict], k: int, min_n: int) -> list[dict]:
    """K-way feature combinations across categorical dims with at least min_n samples.
    Ranked by |win_rate - 0.5| (lift_from_neutral) — most actionable patterns first."""
    combos: dict = defaultdict(_empty_bucket)
    for t in trades:
        items = []
        regime    = t.get("market_regime")
        direction = t.get("direction")
        sent      = t.get("_sentiment")
        stage     = t.get("brain_stage")
        hold_b    = _duration_bucket(t.get("hold_time_seconds"))
        wkday     = _weekday_bucket(t.get("entry_time"))
        if regime:    items.append(("regime", regime))
        if direction: items.append(("dir", direction))
        if sent is not None:
            items.append(("sent", _sentiment_bucket(sent)))
        if stage is not None:
            items.append(("stage", f"s{stage}"))
        if hold_b != "unknown":
            items.append(("hold", hold_b))
        if wkday != "unknown":
            items.append(("wday", wkday))

        if len(items) < k:
            continue
        net_pnl = float(t.get("net_pnl_usdt") or 0)
        won = net_pnl > 0
        for combo in combinations(items, k):
            key = " & ".join(f"{d}={v}" for d, v in sorted(combo))
            _add(combos[key], won, net_pnl)

    records = _records(combos, "combo", min_n=min_n)
    records.sort(key=lambda x: x["lift_from_neutral"], reverse=True)
    return records[:_TOP_N_COMBOS]


# ─────────────────────────────────────────────────────────────────────────────
# Section 3 — WINNERS vs LOSERS feature comparison (significance + effect size)
# ─────────────────────────────────────────────────────────────────────────────

def _winner_loser_comparison(trades: list[dict]) -> list[dict]:
    """For each numeric feature in feature_vector, compute:
       - mean and std for winners vs losers
       - Welch's t-test p-value (scipy.stats.ttest_ind, equal_var=False)
       - Cohen's d effect size (pooled-std normalized mean difference)
    Skips features with fewer than _MIN_FEATURE_TTEST_N samples per side.
    Surfaces only features that have at least one valid measurement."""
    features = ["ofi", "vpin", "sentiment"]  # mark is price-scale-dependent, excluded
    wins_by_feat:   dict = {f: [] for f in features}
    losses_by_feat: dict = {f: [] for f in features}

    for t in trades:
        net_pnl = float(t.get("net_pnl_usdt") or 0)
        fv = t.get("_fv")
        if not fv:
            continue
        target = wins_by_feat if net_pnl > 0 else losses_by_feat
        for f in features:
            v = fv.get(f)
            if v is None:
                continue
            try:
                target[f].append(float(v))
            except (ValueError, TypeError):
                pass

    try:
        from scipy import stats as scistats
    except ImportError:
        scistats = None

    out = []
    for f in features:
        w, l = wins_by_feat[f], losses_by_feat[f]
        if len(w) < _MIN_FEATURE_TTEST_N or len(l) < _MIN_FEATURE_TTEST_N:
            continue
        mw, ml = sum(w) / len(w), sum(l) / len(l)
        # Sample std (Bessel-corrected)
        sw = math.sqrt(sum((x - mw) ** 2 for x in w) / max(1, len(w) - 1))
        sl = math.sqrt(sum((x - ml) ** 2 for x in l) / max(1, len(l) - 1))
        # Pooled std for Cohen's d
        denom = max(1, len(w) + len(l) - 2)
        pooled = math.sqrt(((len(w) - 1) * sw ** 2 + (len(l) - 1) * sl ** 2) / denom)
        d = (mw - ml) / pooled if pooled > 0 else 0.0
        # Welch's t-test (unequal variances)
        p_value = None
        if scistats is not None:
            try:
                _, p_value = scistats.ttest_ind(w, l, equal_var=False)
                p_value = float(p_value)
            except Exception:
                pass
        out.append({
            "feature": f,
            "mean_winners": round(mw, 5),
            "mean_losers": round(ml, 5),
            "std_winners": round(sw, 5),
            "std_losers": round(sl, 5),
            "n_winners": len(w),
            "n_losers": len(l),
            "cohens_d": round(d, 3),
            "p_value": round(p_value, 4) if p_value is not None else None,
            "significant": (p_value is not None and p_value < 0.05),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Section 4 — Strategy + DCA rollups
# ─────────────────────────────────────────────────────────────────────────────

def _strategy_buckets(trades: list[dict]) -> list[dict]:
    """Per-strategy_id rollup — useful when multiple strategies run concurrently."""
    buckets: dict = defaultdict(_empty_bucket)
    for t in trades:
        sid = str(t.get("strategy_id") or "none")
        net_pnl = float(t.get("net_pnl_usdt") or 0)
        _add(buckets[sid], net_pnl > 0, net_pnl)
    records = _records(buckets, "strategy_id")
    records.sort(key=lambda x: x["win_rate"], reverse=True)
    return records


def _dca_effectiveness(trades: list[dict]) -> list[dict]:
    """Did adding DCA capital actually improve outcomes?
    Three groups: never DCA'd, DCA1 only, DCA1+DCA2."""
    buckets: dict = defaultdict(_empty_bucket)
    for t in trades:
        ds = t.get("dca_status")
        if isinstance(ds, str):
            try:
                ds = json.loads(ds)
            except Exception:
                ds = {}
        ds = ds or {}
        r1 = bool(ds.get("round_1_triggered"))
        r2 = bool(ds.get("round_2_triggered"))
        if not r1:
            key = "no_dca"
        elif not r2:
            key = "dca1_only"
        else:
            key = "dca1_and_dca2"
        net_pnl = float(t.get("net_pnl_usdt") or 0)
        _add(buckets[key], net_pnl > 0, net_pnl)
    # min_n=1 so small DCA groups still show — caller can ignore small n
    return _records(buckets, "dca_status", min_n=1)


# ─────────────────────────────────────────────────────────────────────────────
# Section 5 — Derived insights (plain English for LLM / dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def _derive_insights(out: dict) -> list[dict]:
    """Surface most actionable patterns as plain-English strings.
    These are intended to be spliced into OPRO / Research-Engine LLM prompts."""
    insights: list[dict] = []

    def _add(insight_type: str, summary: str) -> None:
        insights.append({"rank": len(insights) + 1, "type": insight_type, "summary": summary})

    # 1. Worst (regime, direction) bucket
    rd = out.get("by_regime_direction", [])
    if rd:
        worst = rd[0]
        if worst["win_rate"] < 0.25 and worst["n"] >= 10:
            _add("avoid_setup",
                 f"Setup '{worst['context']}' has {worst['win_rate']*100:.0f}% win rate "
                 f"over {worst['n']} trades (avg PnL ${worst['avg_pnl']:+.2f}, σ ${worst['std_pnl']:.2f}). "
                 f"Brain should heavily downweight or hard-reject this setup.")

    # 2. Worst 2-way combo
    for c in out.get("multi_dim_combos_2way", []):
        if c["win_rate"] < 0.30 and c["n"] >= 15:
            _add("avoid_combo_2way",
                 f"Combo '{c['combo']}' has {c['win_rate']*100:.0f}% win rate over "
                 f"{c['n']} trades (avg PnL ${c['avg_pnl']:+.2f}). Strong reject candidate.")
            break

    # 3. Best 3-way combo (most specific signal we can find)
    best_3way = max(
        (c for c in out.get("multi_dim_combos_3way", []) if c["win_rate"] > 0.55 and c["n"] >= 15),
        key=lambda x: x["win_rate"], default=None,
    )
    if best_3way:
        _add("favor_combo_3way",
             f"Combo '{best_3way['combo']}' has {best_3way['win_rate']*100:.0f}% win rate over "
             f"{best_3way['n']} trades (avg PnL ${best_3way['avg_pnl']:+.2f}). Boost confidence here.")

    # 4. Significant feature signals (winners vs losers)
    for fstat in out.get("winners_vs_losers", []):
        if fstat["significant"] and abs(fstat["cohens_d"]) > 0.3:
            direction = "higher" if fstat["mean_winners"] > fstat["mean_losers"] else "lower"
            _add("feature_signal",
                 f"Winners have {direction} {fstat['feature']} "
                 f"({fstat['mean_winners']:.4f} vs losers {fstat['mean_losers']:.4f}); "
                 f"p={fstat['p_value']}, Cohen's d={fstat['cohens_d']}. "
                 f"Signal strong enough to weight in entry decisions.")

    # 5. Hour-of-day extremes
    hours = sorted(out.get("by_hour", []), key=lambda x: x["win_rate"])
    if hours and len(hours) >= 6:
        worst_h = hours[0]
        best_h  = hours[-1]
        if worst_h["win_rate"] < 0.30 and worst_h["n"] >= 10:
            _add("avoid_timing",
                 f"Hour {worst_h['hour']} UTC has {worst_h['win_rate']*100:.0f}% win rate "
                 f"over {worst_h['n']} trades. Consider pausing new entries this hour.")
        elif best_h["win_rate"] > 0.55 and best_h["n"] >= 10:
            _add("favor_timing",
                 f"Hour {best_h['hour']} UTC has {best_h['win_rate']*100:.0f}% win rate "
                 f"over {best_h['n']} trades — favorable trading window.")

    # 6. Pair extremes
    top_pairs = out.get("top_pairs", [])
    if top_pairs and top_pairs[0]["win_rate"] > 0.55 and top_pairs[0]["n"] >= 10:
        bp = top_pairs[0]
        _add("favor_pair",
             f"Pair {bp['pair']} has {bp['win_rate']*100:.0f}% win rate over {bp['n']} trades — prioritize.")

    return insights[:_MAX_INSIGHTS]


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def mine_patterns() -> dict:
    """Load last N closed paper trades; run all 5 sections; persist to Redis."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pair, direction, market_regime, net_pnl_usdt, feature_vector,
                       entry_time, hold_time_seconds, brain_stage, strategy_id, dca_status
                FROM trades
                WHERE status='closed' AND is_paper=true
                ORDER BY exit_time DESC
                LIMIT %s
            """, (_RECENT_LIMIT,))
            cols = [d[0] for d in cur.description]
            trades = [dict(zip(cols, row)) for row in cur.fetchall()]

    if not trades:
        log.warning("pattern_miner_no_trades")
        return {"status": "no_trades"}

    # Pre-parse feature_vector and extract sentiment so downstream helpers don't re-parse
    for t in trades:
        fv = t.get("feature_vector")
        t["_fv"] = None
        t["_sentiment"] = None
        if fv:
            try:
                fv_dict = fv if isinstance(fv, dict) else json.loads(fv)
                t["_fv"] = fv_dict
                if "sentiment" in fv_dict and fv_dict["sentiment"] is not None:
                    t["_sentiment"] = float(fv_dict["sentiment"])
            except Exception:
                pass

    out: dict = {}
    out.update(_coarse_buckets(trades))
    out["multi_dim_combos_2way"] = _combo_buckets(trades, k=2, min_n=_MIN_COMBO_2WAY_N)
    out["multi_dim_combos_3way"] = _combo_buckets(trades, k=3, min_n=_MIN_COMBO_3WAY_N)
    out["winners_vs_losers"]     = _winner_loser_comparison(trades)
    out["by_strategy"]           = _strategy_buckets(trades)
    out["dca_effectiveness"]     = _dca_effectiveness(trades)
    out["insights"]              = _derive_insights(out)

    out["ts"] = datetime.now(timezone.utc).isoformat()
    out["trades_analyzed"] = len(trades)

    r = redis_client.get()
    r.set("brain:patterns_mined", json.dumps(out, default=str))

    log.info("pattern_miner_complete",
             trades_analyzed=len(trades),
             insights=len(out["insights"]),
             rd_buckets=len(out.get("by_regime_direction", [])),
             combos_2way=len(out["multi_dim_combos_2way"]),
             combos_3way=len(out["multi_dim_combos_3way"]),
             significant_features=sum(1 for f in out["winners_vs_losers"] if f.get("significant")),
             dca_groups=len(out["dca_effectiveness"]))

    return {
        "status": "mined",
        "trades_analyzed": len(trades),
        "insights_count": len(out["insights"]),
        "sections_populated": [k for k in out.keys() if k not in ("ts", "trades_analyzed")],
    }
