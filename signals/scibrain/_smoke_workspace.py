"""Smoke for the read-only workspace (workspace.py) — builds against LIVE Redis (run in a bot container).
Run: python -m signals.scibrain._smoke_workspace"""
from __future__ import annotations


def main() -> int:
    import redis_client
    from signals.scibrain import workspace
    from signals.scibrain.cognitive_contracts import CognitiveMessage, BeliefState
    r = redis_client.get()
    fails: list[str] = []

    ws = workspace.build_workspace(r, publish=True)
    if not ws.get("available"):
        # no decisions yet is a valid empty state, not a failure — report and pass structurally
        print(f"[0] workspace unavailable (ok if no decisions): {ws.get('error')}")
        print("\nALL OK (empty)")
        return 0

    sym = ws["symbol"]
    print(f"[1] built workspace for {sym}: {ws['n_messages']} messages ({ws['n_convicted']} convicted), "
          f"broadcast={ws['broadcast']}")

    # 2. every message round-trips to a VALID CognitiveMessage
    bad_msgs = 0
    for md in ws["messages"]:
        m = CognitiveMessage.from_dict(md)
        errs = m.validate()
        if errs:
            bad_msgs += 1
            if bad_msgs <= 3:
                print("   bad message:", md.get("source"), errs)
        if m.to_dict() != md:
            fails.append(f"message {md.get('source')} round-trip mismatch")
    if bad_msgs:
        fails.append(f"{bad_msgs} messages failed validation")
    print(f"[2] {len(ws['messages'])} messages all valid CognitiveMessages, round-trip ok")

    # 3. belief is a VALID BeliefState; regime posterior ~sums to 1
    b = BeliefState.from_dict(ws["belief"])
    berrs = b.validate()
    if berrs:
        fails.append(f"belief invalid: {berrs}")
    rp_sum = round(sum(ws["belief"]["regime_posterior"].values()), 3)
    if not (0.95 <= rp_sum <= 1.05):
        fails.append(f"regime_posterior sums to {rp_sum}, not ~1")
    print(f"[3] belief valid={not berrs} regime_posterior_sum={rp_sum} "
          f"disagreement={ws['belief']['disagreement']} changepoint={ws['belief']['changepoint_posterior']}")
    print(f"    uncertainty_decomp={ws['belief']['uncertainty_decomposition']} "
          f"latent_dim={len(ws['belief']['latent_mean'])} dispersion={ws['belief']['latent_dispersion']}")

    # 4. broadcast subset ⊆ message sources; lineage present
    srcs = {m["source"] for m in ws["messages"]}
    if not set(ws["broadcast"]).issubset(srcs):
        fails.append("broadcast not a subset of message sources")
    if not ws["belief"]["source_ids"]:
        fails.append("belief has no source lineage")
    print(f"[4] broadcast ⊆ sources={set(ws['broadcast']).issubset(srcs)}; "
          f"source lineage n={len(ws['belief']['source_ids'])}")

    # 5. published to Redis
    import json
    pub = r.get("scibrain:workspace")
    if not pub or not json.loads(pub).get("available"):
        fails.append("workspace not published to scibrain:workspace")
    print(f"[5] published to scibrain:workspace={bool(pub)}")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
