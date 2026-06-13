"""SciBrain Phase-7e — ABSTENTION memory: reward correct abstention + preserve the non-trade events (§3.5/§3.11).

Current memory only keeps TRADES — it drops the events the design says replay MUST include: "wins, false
alarms, rejected actions, near misses, and safety interventions" (§3.5). And §3.11: "the brain must be
REWARDED for correct abstention and penalized for confident unsupported action." This builds that:

  • REJECTED ACTIONS — every signal the bot rejected lives in the counterfactuals ledger with whether it
    `would_have_won`. Each is classified: CORRECT_ABSTENTION (rejected, would have LOST — the bot dodged a
    loser) | MISSED_OPPORTUNITY (rejected, would have WON) | NEAR_MISS (a close call by peak profit/loss).
  • FALSE ALARMS — the decoder's miss-tag breakdown of WHY a rejection was a miss.
  • ABSTENTION REWARD — reward correct abstention by its avoided loss, penalize missed opportunity by its
    forgone gain → a net reward signal for metacognition.
  • ABSTENTION SKILL — does the bot reject DISCRIMINATINGLY? rejected-set would-win-rate vs the accepted-
    set win rate: a skilled abstainer rejects signals that would win LESS often than the ones it takes.

Tier-0: pure read; it surfaces the reward + preserves the counts. No trading authority.
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()


def _q(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()


def build_abstention_memory(r, *, limit: int = 20000, publish: bool = True) -> dict:
    """Classify resolved rejected signals, reward correct abstention, and diagnose abstention skill +
    whether the non-trade events are now preserved. Pure read; never raises."""
    out = {"contract": "AbstentionMemory", "available": False, "ts": round(time.time(), 3)}
    try:
        from db import db_conn
        with db_conn() as c:
            with c.cursor() as cur:
                # resolved rejected signals (the counterfactual ledger)
                row = _q(cur,
                    "SELECT COUNT(*), "
                    "COUNT(*) FILTER (WHERE would_have_won) , "
                    "AVG(CASE WHEN would_have_won THEN peak_profit_pct END), "
                    "AVG(CASE WHEN NOT would_have_won THEN peak_loss_pct END), "
                    "COUNT(*) FILTER (WHERE NOT would_have_won AND peak_loss_pct < -1.0), "
                    "COUNT(*) FILTER (WHERE would_have_won AND peak_profit_pct < 1.5) "
                    "FROM counterfactuals WHERE would_have_won IS NOT NULL "
                    "AND created_at > NOW() - INTERVAL '30 days'")[0]
                n_rej, n_rej_win, avg_missed_gain, avg_avoided_loss, n_big_avoided, n_near_miss = row
                n_rej = int(n_rej or 0)
                # false-alarm (miss-tag) breakdown
                tags = _q(cur,
                    "SELECT COALESCE(miss_tag,'untagged'), COUNT(*) FROM counterfactuals "
                    "WHERE would_have_won IS NOT NULL AND created_at > NOW() - INTERVAL '30 days' "
                    "GROUP BY miss_tag ORDER BY COUNT(*) DESC LIMIT 8")
                # accepted-signal win rate (the trades that WERE taken) — the discrimination baseline
                arow = _q(cur,
                    "SELECT COUNT(*) FILTER (WHERE net_pnl_usdt > 0), COUNT(*) FROM trades "
                    "WHERE status='closed' AND entry_time > NOW() - INTERVAL '30 days'")[0]
                n_acc_win, n_acc = int(arow[0] or 0), int(arow[1] or 0)

        if n_rej < 50:
            out.update({"health": "cold", "n_rejected": n_rej, "issues": [],
                        "note": "Abstention memory cold — too few resolved counterfactuals yet."})
            if publish:
                r.set(K.ABSTENTION_MEMORY, json.dumps(out))
            return out

        rej_win_rate = (n_rej_win or 0) / n_rej                      # would-win rate among REJECTED
        correct_abstention_rate = 1.0 - rej_win_rate                 # rejected & would have LOST
        accepted_win_rate = (n_acc_win / n_acc) if n_acc else None
        # skill = the bot rejects winners LESS often than it accepts them (discrimination)
        skill = (accepted_win_rate - rej_win_rate) if accepted_win_rate is not None else None
        avoided = abs(float(avg_avoided_loss or 0.0))               # avg avoided loss% (correct abstention)
        missed = float(avg_missed_gain or 0.0)                      # avg forgone gain% (missed opportunity)
        # net abstention reward: reward avoided losses, penalize forgone gains, weighted by how often each
        abstention_reward = round(correct_abstention_rate * avoided - rej_win_rate * missed, 4)

        classes = {
            "correct_abstention": {"n": int(n_rej - (n_rej_win or 0)), "rate": round(correct_abstention_rate, 4),
                                   "avg_avoided_loss_pct": round(avoided, 3)},
            "missed_opportunity": {"n": int(n_rej_win or 0), "rate": round(rej_win_rate, 4),
                                   "avg_forgone_gain_pct": round(missed, 3)},
            "near_miss": {"n": int(n_near_miss or 0), "note": "rejected & would-have-won but only marginally"},
            "false_alarm_tags": [{"tag": t, "n": int(cn)} for t, cn in tags],
        }

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        # non-trade events NOW preserved (the §3.5 gap closed)
        add("non_trade_events_preserved", "info",
            f"Non-trade events preserved — {n_rej} rejected signals retained (was: trades only)",
            "Memory now keeps rejected actions / correct abstentions / near misses, not just executed trades.",
            "Feed these into prioritized replay so the brain learns from what it DIDN'T do.")
        if skill is not None and skill > 0.03:
            add("abstention_skilled", "info",
                f"Abstention is DISCRIMINATING — rejected would-win {rej_win_rate*100:.0f}% < accepted win {accepted_win_rate*100:.0f}%",
                "The bot rejects signals that would win less often than the ones it takes (skilled abstention).",
                "Reward correct abstention in the value function (done: abstention_reward).")
        elif skill is not None and skill < 0.0:
            add("abstention_anti_skilled", "critical",
                f"Abstention is ANTI-skilled — rejected would-win {rej_win_rate*100:.0f}% > accepted win {accepted_win_rate*100:.0f}%",
                "The bot rejects BETTER signals than it accepts — it's throwing away winners and keeping losers.",
                "Audit the rejection gates urgently; the accept/reject decision is mis-ordered.")
        else:
            add("abstention_indiscriminate", "warn",
                f"Abstention barely discriminates (skill {None if skill is None else round(skill,3)})",
                "Rejected signals would win about as often as accepted ones — the gate adds little.",
                "Sharpen the rejection criteria or it's near-random.")
        if rej_win_rate > 0.55:
            add("over_conservative", "warn",
                f"Possibly over-conservative — {rej_win_rate*100:.0f}% of rejected signals would have won",
                "A majority of rejected signals would have won — the bot may be abstaining too much.",
                "Loosen the gate if the missed-opportunity gain outweighs the avoided-loss.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        worst = max((sev_rank[i["severity"]] for i in issues), default=0)
        health = "anti_skilled" if any(i["code"] == "abstention_anti_skilled" for i in issues) \
            else "watch" if worst >= 2 else "healthy"

        out.update({
            "available": True, "health": health, "n_rejected": n_rej, "n_accepted": n_acc,
            "abstention_reward": abstention_reward,
            "skill": {"rejected_would_win_rate": round(rej_win_rate, 4),
                      "accepted_win_rate": round(accepted_win_rate, 4) if accepted_win_rate is not None else None,
                      "discrimination": round(skill, 4) if skill is not None else None},
            "classes": classes,
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Abstention memory (design §3.5/§3.11): the rejected-action / correct-abstention / "
                     "near-miss / false-alarm events current memory drops, now preserved + classified, with "
                     "a net ABSTENTION REWARD (avoided losses − forgone gains) and a SKILL read (does the "
                     "bot reject discriminatingly?). Reward correct abstention; penalize confident "
                     "unsupported action. Pure read; no trading authority."),
        })
        if publish:
            try:
                r.set(K.ABSTENTION_MEMORY, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_abstention_failed", error=str(exc)[:200])
    return out
