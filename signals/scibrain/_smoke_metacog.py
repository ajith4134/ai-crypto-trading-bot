"""Smoke for the metacognition competence maps (metacog.py) — builds against LIVE Redis (run in container).
Run: python -m signals.scibrain._smoke_metacog"""
from __future__ import annotations


def main() -> int:
    import redis_client
    from signals.scibrain import metacog
    from signals.scibrain.cognitive_contracts import CompetenceRecord
    r = redis_client.get()
    fails: list[str] = []

    m = metacog.build_competence_map(r, publish=True)
    if not m.get("available"):
        fails.append(f"metacog unavailable: {m.get('error')}")
        print("FAIL:", fails); return 1

    comps = m["components"]
    g = m["global"]
    print(f"[1] {g['n_components']} components, {g['n_ood']} OOD; mean_cal={g['mean_calibration']} "
          f"mean_epistemic={g['mean_epistemic']} mean_aleatoric={g['mean_aleatoric']}")
    print(f"    audit_calibration={g['audit_calibration']}")

    # 2. each component carries all required facets + a VALID CompetenceRecord; ranges bounded
    req = ("component", "calibration", "epistemic_uncertainty", "aleatoric_uncertainty",
           "support_distance", "ood", "abstention_utility", "utility_delta", "support", "drift")
    for c in comps:
        for k in req:
            if k not in c:
                fails.append(f"{c.get('component')} missing facet {k}")
        for k in ("calibration", "epistemic_uncertainty", "aleatoric_uncertainty", "support_distance",
                  "abstention_utility"):
            if not (0.0 <= c.get(k, -1) <= 1.0):
                fails.append(f"{c['component']}.{k}={c.get(k)} out of [0,1]")
        if not c.get("_record_valid"):
            fails.append(f"{c['component']} CompetenceRecord invalid")
        # round-trip the record portion
        rec = CompetenceRecord.from_dict(c)
        if rec.validate():
            fails.append(f"{c['component']} record fails validate(): {rec.validate()}")
    top = comps[0] if comps else {}
    print(f"[2] facets present + records valid; top component: {top.get('component')} "
          f"cal={top.get('calibration')} epi={top.get('epistemic_uncertainty')} "
          f"ale={top.get('aleatoric_uncertainty')} ood={top.get('ood')} abst={top.get('abstention_utility')}")

    # 3. P(action_supported) present + bounded; decision context coherent
    p = m.get("p_action_supported")
    dc = m.get("decision_context") or {}
    if p is not None and not (0.0 <= p <= 1.0):
        fails.append(f"p_action_supported {p} out of [0,1]")
    print(f"[3] P(action_supported)={p} abstain_recommended={m.get('abstention_recommended')} "
          f"sym={dc.get('symbol')} drivers_cal={dc.get('mean_driver_calibration')} "
          f"disagreement={dc.get('disagreement')}")

    # 4. published
    import json
    pub = r.get("scibrain:metacognition")
    if not pub or not json.loads(pub).get("available"):
        fails.append("not published to scibrain:metacognition")
    print(f"[4] published to scibrain:metacognition={bool(pub)}")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
