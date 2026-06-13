"""Smoke for the thalamus salience router (thalamus.py) — builds against LIVE Redis (run in a container).
Run: python -m signals.scibrain._smoke_thalamus"""
from __future__ import annotations


def main() -> int:
    import redis_client
    from signals.scibrain import thalamus, workspace
    r = redis_client.get()
    fails: list[str] = []

    ws = workspace.build_workspace(r, publish=False)
    t = thalamus.route_salience(r, ws, publish=True)
    if not t.get("available"):
        print(f"[0] thalamus unavailable (ok if no decisions): {t.get('error')}")
        print("\nALL OK (empty)")
        return 0

    print(f"[1] routed {t['symbol']}: {t['n_messages']} msgs, {t['n_abstained']} abstained, "
          f"load_factor={t['load_factor']}")
    print(f"    drivers={t['drivers']}")

    # 2. ranked is salience-sorted, every component present
    ranked = t["ranked"]
    if ranked != sorted(ranked, key=lambda x: x["salience"], reverse=True):
        fails.append("ranked not sorted by salience desc")
    for row in ranked[:3]:
        if not all(k in row["components"] for k in
                   ("info_gain", "relevance", "anomaly", "risk_urgency", "compute_cost", "redundancy")):
            fails.append(f"missing salience components on {row['source']}")
    print(f"[2] ranked sorted; top: " + ", ".join(
        f"{x['source']}={x['salience']}" for x in ranked[:4]))

    # 3. evidence selection is sparse, family-balanced, ⊆ non-abstained sources, within budget
    ev = t["evidence_selected"]
    budgets = t["budgets"]
    non_abstain = {x["source"] for x in ranked if not x["abstain"]}
    if not set(ev).issubset(non_abstain):
        fails.append("evidence_selected includes an abstained/unknown source")
    if len(ev) > budgets["evidence"]:
        fails.append(f"evidence_selected {len(ev)} exceeds evidence budget {budgets['evidence']}")
    # family balance: no family appears > _MAX_PER_FAMILY in the selected set
    fam_of = {x["source"]: x["evidence_family"] for x in ranked}
    fam_sel: dict = {}
    for s in ev:
        fam_sel[fam_of.get(s)] = fam_sel.get(fam_of.get(s), 0) + 1
    if any(c > thalamus._MAX_PER_FAMILY for c in fam_sel.values()):
        fails.append(f"family load-balance violated: {fam_sel}")
    print(f"[3] evidence_selected={ev} (budget {budgets['evidence']}); family counts {fam_sel}")

    # 4. budgets present, bounded, non-negative; compute_fraction == load_factor (load-aware)
    for k in ("evidence", "memory_retrieval_depth", "planning_depth", "audit_depth", "compute_fraction"):
        if k not in budgets:
            fails.append(f"missing budget {k}")
        elif budgets[k] < 0:
            fails.append(f"budget {k} negative")
    if abs(budgets["compute_fraction"] - t["load_factor"]) > 1e-6:
        fails.append("compute_fraction != load_factor (not load-aware)")
    print(f"[4] budgets={budgets}")

    # 5. published
    import json
    pub = r.get("scibrain:thalamus")
    if not pub or not json.loads(pub).get("available"):
        fails.append("thalamus not published to scibrain:thalamus")
    print(f"[5] published to scibrain:thalamus={bool(pub)}")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
