"""cont. 74 — Coherence-vs-legacy SHADOW SCOREBOARD.

The Kuramoto ensemble-coherence score replaced the legacy past-trades win-rate
(hist_acc) as the per-signal strength component (signals/engine.py). Both scores are
snapshotted into trades.signals_at_entry at open time (memory/write.py). Once a trade
closes we know its realized net PnL, so we can finally answer the question that the live
divergence panel CANNOT: does coherence actually PREDICT winning trades better than the
legacy win-rate did?

The honest test is NOT "which number was higher" — it is which score is monotonically
related to realized outcome. We measure that two ways per source:
  1. Win-rate (and avg net PnL) by score bucket — does a higher score => more wins?
  2. Spearman rank IC between score and net PnL — single predictiveness number in [-1,1].

Verdict = the source with the higher (signed) Spearman IC vs net PnL.

CAVEAT (selection bias): coherence is LIVE — it helps GATE which trades open, legacy is
shadow-only. We only observe outcomes for trades the composite allowed. So this compares
the two scores' predictiveness ON THE TAKEN TRADES (a fair relative ranking), not on the
universe of all candidate signals. Surfaced in the panel; do not over-read tiny samples.
"""
from __future__ import annotations
import json
import structlog
from db import db_conn

log = structlog.get_logger()

# Score buckets (per-signal scores live in [0,100], centered at 50 = neutral).
_BUCKETS = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 100.01)]
_MIN_MEANINGFUL = 40   # below this many closed trades, flag the verdict as low-confidence


def _rank(xs: list[float]) -> list[float]:
    """Average-rank (1-based) with tie handling — for Spearman."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0          # average of the 1-based positions i..j
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = sum((a[i] - ma) ** 2 for i in range(n)) ** 0.5
    db = sum((b[i] - mb) ** 2 for i in range(n)) ** 0.5
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def _spearman(scores: list[float], pnls: list[float]) -> float | None:
    if len(scores) < 3:
        return None
    return _pearson(_rank(scores), _rank(pnls))


def _bucketize(scores: list[float], wins: list[bool], pnls: list[float]) -> list[dict]:
    out = []
    for lo, hi in _BUCKETS:
        idx = [i for i, s in enumerate(scores) if lo <= s < hi]
        n = len(idx)
        out.append({
            "bucket": f"{lo:g}-{min(hi,100):g}",
            "n": n,
            "win_rate": round(100.0 * sum(1 for i in idx if wins[i]) / n, 1) if n else None,
            "avg_pnl": round(sum(pnls[i] for i in idx) / n, 4) if n else None,
            "total_pnl": round(sum(pnls[i] for i in idx), 4) if n else None,
        })
    return out


def _tercile_spread(scores: list[float], wins: list[bool]) -> float | None:
    """Win-rate in the top third of scores minus the bottom third (pp). Intuitive
    'does ranking by this score separate winners from losers' number."""
    n = len(scores)
    if n < 9:
        return None
    order = sorted(range(n), key=lambda i: scores[i])
    t = n // 3
    bot = order[:t]
    top = order[-t:]
    wb = 100.0 * sum(1 for i in bot if wins[i]) / t
    wt = 100.0 * sum(1 for i in top if wins[i]) / t
    return round(wt - wb, 1)


def state(limit: int = 4000) -> dict:
    """Build the coherence-vs-legacy scoreboard from closed trades whose
    signals_at_entry carries both scores. Returns a dashboard-ready dict."""
    rows: list[tuple] = []
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT (signals_at_entry->>'coherence_score')::float       AS coh,
                           (signals_at_entry->>'legacy_winrate_score')::float  AS leg,
                           (signals_at_entry->>'coherence_live')               AS coh_live,
                           net_pnl_usdt::float                                 AS pnl
                    FROM trades
                    WHERE status = 'closed'
                      AND net_pnl_usdt IS NOT NULL
                      AND signals_at_entry IS NOT NULL
                      AND signals_at_entry ? 'coherence_score'
                      AND (signals_at_entry->>'coherence_score') IS NOT NULL
                      AND (signals_at_entry->>'legacy_winrate_score') IS NOT NULL
                    ORDER BY entry_time DESC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
                rows = cur.fetchall() or []
    except Exception as exc:
        log.warning("coherence_eval_query_failed", error=str(exc)[:200])
        return {"status": "error", "error": str(exc)[:200], "n_trades": 0}

    coh, leg, pnls, wins = [], [], [], []
    for r in rows:
        try:
            c, l, _live, p = float(r[0]), float(r[1]), r[2], float(r[3])
        except (TypeError, ValueError):
            continue
        coh.append(c)
        leg.append(l)
        pnls.append(p)
        wins.append(p > 0)

    n = len(pnls)
    if n == 0:
        return {
            "status": "collecting",
            "n_trades": 0,
            "note": ("No closed trades carry the dual-score snapshot yet. Persistence "
                     "started this deploy (cont. 74); the scoreboard populates as trades "
                     "opened from now on close. Each new closed trade adds a data point."),
        }

    overall_win = round(100.0 * sum(wins) / n, 1)
    overall_pnl = round(sum(pnls), 4)
    coh_spearman = _spearman(coh, pnls)
    leg_spearman = _spearman(leg, pnls)

    if coh_spearman is None or leg_spearman is None:
        verdict = "insufficient"
    elif coh_spearman > leg_spearman:
        verdict = "coherence"
    elif leg_spearman > coh_spearman:
        verdict = "legacy"
    else:
        verdict = "tie"

    return {
        "status": "ok" if n >= _MIN_MEANINGFUL else "low_confidence",
        "n_trades": n,
        "min_meaningful": _MIN_MEANINGFUL,
        "overall_win_rate": overall_win,
        "overall_net_pnl": overall_pnl,
        "coherence": {
            "spearman_ic": round(coh_spearman, 4) if coh_spearman is not None else None,
            "tercile_win_spread_pp": _tercile_spread(coh, wins),
            "buckets": _bucketize(coh, wins, pnls),
        },
        "legacy": {
            "spearman_ic": round(leg_spearman, 4) if leg_spearman is not None else None,
            "tercile_win_spread_pp": _tercile_spread(leg, wins),
            "buckets": _bucketize(leg, wins, pnls),
        },
        "verdict": verdict,
        "verdict_note": {
            "coherence": "Coherence ranks realized outcomes better — keep it as the live source.",
            "legacy": "Legacy win-rate ranks outcomes better on this sample — investigate before trusting coherence.",
            "tie": "Both rank outcomes equally on this sample.",
            "insufficient": "Not enough spread/data to rank the two sources yet.",
        }.get(verdict, ""),
        "caveat": ("Selection bias: coherence is LIVE (helps gate trades), legacy is "
                   "shadow. This ranks the two scores' predictiveness on TAKEN trades, "
                   "not all candidates. Treat n<%d as directional only." % _MIN_MEANINGFUL),
    }
