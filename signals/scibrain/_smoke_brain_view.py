"""Smoke for the whole-brain BrainPulse (brain_view.py) — builds against LIVE Redis (run in container).
Run: python -m signals.scibrain._smoke_brain_view"""
from __future__ import annotations


def main() -> int:
    import redis_client
    from signals.scibrain import brain_view
    r = redis_client.get()
    fails: list[str] = []

    p = brain_view.build_brain_pulse(r, publish=True)
    if not p.get("available"):
        fails.append(f"brain pulse unavailable: {p.get('error')}")
        print("FAIL:", fails); return 1

    regions = p["regions"]
    expect = {"brainstem", "thalamus", "workspace", "metacortex", "basal_ganglia", "amygdala", "learning"}
    got = {rg["region"] for rg in regions}
    if expect - got:
        fails.append(f"missing regions: {expect - got}")
    for rg in regions:
        for k in ("region", "label", "role", "active", "authority", "health", "metric", "source"):
            if k not in rg:
                fails.append(f"region {rg.get('region')} missing {k}")
    print(f"[1] {len(regions)} regions: " + ", ".join(
        f"{rg['region']}({'on' if rg['active'] else 'off'}/{'ok' if rg['health'] else 'X'})" for rg in regions))

    # 2. cross-cutting views present
    for sec in ("broadcast", "uncertainty", "competence", "authority", "compute"):
        if sec not in p:
            fails.append(f"missing cross-cut section {sec}")
    u = p["uncertainty"]; a = p["authority"]; c = p["compute"]
    print(f"[2] uncertainty: epistemic={u.get('epistemic')} aleatoric={u.get('aleatoric')} "
          f"disagreement={u.get('disagreement')} P(supported)={u.get('p_action_supported')}")
    print(f"    authority: live_capital={a.get('live_capital_components')} "
          f"n_open_bypasses={a.get('n_open_bypasses')} invariants_ok={a.get('all_invariants_ok')}")
    print(f"    compute: load_factor={c.get('load_factor')} compute_fraction={c.get('compute_fraction')} "
          f"budgets={c.get('budgets')}")
    print(f"    competence: mean_cal={p['competence'].get('mean_calibration')} "
          f"n_ood={p['competence'].get('n_ood')} top={p['competence'].get('top')}")
    print(f"    broadcast: ws={p['broadcast'].get('workspace')} thal={p['broadcast'].get('thalamus_evidence')}")

    # 3. the no-bypass + safety facts surface through the pulse (ties this session together)
    if a.get("n_open_bypasses") != 0:
        fails.append(f"pulse should show 0 open bypasses, got {a.get('n_open_bypasses')}")
    bstem = next((rg for rg in regions if rg["region"] == "brainstem"), {})
    if not bstem.get("health"):
        fails.append("brainstem region not healthy in pulse")
    print(f"[3] all_regions_healthy={p.get('all_regions_healthy')} brainstem_health={bstem.get('health')} "
          f"n_open_bypasses={a.get('n_open_bypasses')}")

    # 4. published
    import json
    pub = r.get("scibrain:brain_pulse")
    if not pub or not json.loads(pub).get("available"):
        fails.append("not published to scibrain:brain_pulse")
    print(f"[4] published to scibrain:brain_pulse={bool(pub)}")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
