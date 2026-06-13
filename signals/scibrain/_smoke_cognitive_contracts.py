"""Smoke for the Cognitive-OS typed contracts (cognitive_contracts.py). No external deps.
Run: python -m signals.scibrain._smoke_cognitive_contracts"""
from __future__ import annotations


def main() -> int:
    from signals.scibrain import cognitive_contracts as C
    fails: list[str] = []

    # 1. each contract constructs valid and round-trips to_dict→from_dict→to_dict identically
    valid = {
        "CognitiveMessage": C.CognitiveMessage(source="wavelet", role="direction",
            evidence_family="microstructure", belief_delta=0.6, uncertainty=0.3, salience=0.8,
            horizon=30.0, support_distance=0.4, source_version={"code": "abc"}, evidence_ids=["t1"]),
        "BeliefState": C.BeliefState(latent_mean=[0.1, -0.2], latent_dispersion=0.5,
            regime_posterior={"trending": 0.6, "mean_revert": 0.4}, changepoint_posterior=0.2,
            disagreement=0.3, uncertainty_decomposition={"epistemic": 0.2, "aleatoric": 0.1},
            source_ids=["wavelet", "koopman"], versions={"config": "h1"}),
        "LearningSignal": C.LearningSignal(kind="reward_error", magnitude=-0.4, target="router.gain.chaos",
            confidence=0.7, evidence_ids=["trade-9"], authority="observe"),
        "CompetenceRecord": C.CompetenceRecord(component="koopman", context="trending", task="direction",
            calibration=0.82, utility_delta=0.05, support=140, drift=0.1, version={"code": "z"}),
        "ModelChangeSpec": C.ModelChangeSpec(kind="objective", target="direction_model.loss",
            intervention={"add_term": "calibration_penalty", "weight": 0.1},
            bounds={"weight": [0.0, 0.5]}, expected_effect="improve calibration without hurting utility",
            falsifier="Brier worsens or LCB(ΔU) <= 0", proposer="ai_scientist"),
    }
    for name, obj in valid.items():
        errs = obj.validate()
        if errs:
            fails.append(f"{name} valid instance reported errors: {errs}")
        cls = C.CONTRACTS[name]
        rt = cls.from_dict(obj.to_dict())
        if rt.to_dict() != obj.to_dict():
            fails.append(f"{name} round-trip mismatch")
    print(f"[1] constructed + round-tripped {len(valid)} contracts; CONTRACTS registry={len(C.CONTRACTS)}")

    # 2. validation CATCHES bad ranges / missing required fields
    bad_cases = {
        "CognitiveMessage bad uncertainty": (C.CognitiveMessage(source="x", uncertainty=2.0), "uncertainty"),
        "CognitiveMessage bad role": (C.CognitiveMessage(source="x", role="nonsense"), "role"),
        "LearningSignal bad kind": (C.LearningSignal(kind="bogus", target="t"), "kind"),
        "LearningSignal bad authority": (C.LearningSignal(kind="surprise", target="t", authority="god"), "authority"),
        "CompetenceRecord bad calibration": (C.CompetenceRecord(component="c", calibration=1.5), "calibration"),
        "ModelChangeSpec no falsifier": (C.ModelChangeSpec(kind="model", target="t",
            intervention={"a": 1}, bounds={"a": [0, 1]}, expected_effect="x"), "falsifier"),
        "ModelChangeSpec unbounded": (C.ModelChangeSpec(kind="model", target="t",
            intervention={"a": 1}, expected_effect="x", falsifier="y"), "bounds"),
        "BeliefState bad posterior sum": (C.BeliefState(regime_posterior={"a": 0.2, "b": 0.2}), "sum"),
    }
    for label, (obj, expect_substr) in bad_cases.items():
        errs = obj.validate()
        if not any(expect_substr in e for e in errs):
            fails.append(f"{label}: expected an error containing '{expect_substr}', got {errs}")
    print(f"[2] validation caught all {len(bad_cases)} bad cases")

    # 3. from_dict coerces junk defensively (no raise, clamps bounded fields)
    junk = C.CognitiveMessage.from_dict({"source": "y", "uncertainty": "9", "salience": None,
                                         "evidence_ids": "oops", "horizon": "bad"})
    if junk.uncertainty != 1.0 or junk.salience != 0.0 or junk.evidence_ids != ["oops"] or junk.horizon != 30.0:
        fails.append(f"from_dict coercion wrong: {junk.to_dict()}")
    print(f"[3] from_dict coercion: uncertainty={junk.uncertainty} salience={junk.salience} horizon={junk.horizon}")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
