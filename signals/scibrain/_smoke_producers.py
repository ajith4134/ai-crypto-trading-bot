"""Isolated smoke for the unified producer bus (producers.py). Uses an in-process fake Redis so it
NEVER touches the live registry. Run:  python -m signals.scibrain._smoke_producers"""
from __future__ import annotations

import json


class FakeRedis:
    """Just enough of the redis surface used by producers/experiments/changespec."""
    def __init__(self):
        self.kv: dict = {}
        self.h: dict = {}
        self.z: dict = {}
        self.l: dict = {}

    def lpush(self, k, *vs):
        self.l.setdefault(k, [])
        for v in vs:
            self.l[k].insert(0, v)
        return len(self.l[k])

    def ltrim(self, k, lo, hi):
        if k in self.l:
            self.l[k] = self.l[k][lo:hi + 1]
        return True

    def lrange(self, k, lo, hi):
        items = self.l.get(k, [])
        return items[lo:(hi + 1 if hi >= 0 else None)]

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, **kw):
        self.kv[k] = v if isinstance(v, (str, bytes)) else str(v)
        return True

    def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None)
        return True

    def hget(self, h, f):
        return self.h.get(h, {}).get(f)

    def hset(self, h, f, v):
        self.h.setdefault(h, {})[f] = v
        return 1

    def hgetall(self, h):
        return dict(self.h.get(h, {}))

    def hincrby(self, h, f, n=1):
        cur = int(self.h.get(h, {}).get(f, 0))
        self.h.setdefault(h, {})[f] = cur + n
        return cur + n

    def zadd(self, z, mapping):
        self.z.setdefault(z, {}).update(mapping)
        return 1

    def zrevrange(self, z, lo, hi):
        items = sorted(self.z.get(z, {}).items(), key=lambda kv: kv[1], reverse=True)
        return [k for k, _ in items[lo:(hi + 1 if hi >= 0 else None)]]

    def pipeline(self):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def hset(self, *a):
        self.ops.append(("hset", a)); return self

    def zadd(self, *a):
        self.ops.append(("zadd", a)); return self

    def lpush(self, *a):
        self.ops.append(("lpush", a)); return self

    def ltrim(self, *a):
        self.ops.append(("ltrim", a)); return self

    def execute(self):
        for op, a in self.ops:
            getattr(self.r, op)(*a)
        self.ops = []


def main() -> int:
    from signals.scibrain import producers as P

    r = FakeRedis()
    fails = []

    # 1. registry well-formed
    ids = [p["id"] for p in P.PRODUCERS]
    if len(ids) != len(set(ids)):
        fails.append("duplicate producer ids")
    required = {"id", "label", "family", "task", "gate", "base_mode", "applies_to", "mappable_target"}
    for p in P.PRODUCERS:
        if not required.issubset(p):
            fails.append(f"producer {p.get('id')} missing keys {required - set(p)}")
    # the §11 named subsystems must all be present
    must_have = {"llm_council", "opro_prompts", "dgm_code_rewrite", "ai_scientist", "ga_params",
                 "feature_governance", "metacog_eval", "direction_model", "strategy_pool", "dsl_miner",
                 "f9f12_decoder", "ic_router_learner"}
    if not must_have.issubset(set(ids)):
        fails.append(f"missing §11 producers: {must_have - set(ids)}")
    print(f"[1] registry: {len(P.PRODUCERS)} producers, ids unique={len(ids)==len(set(ids))}")

    # 2. submit a valid bounded proposal → registers; resubmit same → deduped
    prop = {"target": "scibrain.router_strength", "role": "meta",
            "context_predicate": "always", "intervention": {"kind": "param_set", "candidate": 0.9},
            "parameter_bounds": [0.0, 1.0], "expected_effect": "blend gains toward 1.0",
            "falsifier": "LCB(delta_utility) <= 0", "evidence_ids": ["smoke-1"]}
    s1 = P.submit(r, "ga_params", prop)
    s2 = P.submit(r, "ga_params", dict(prop))   # identical content → dedup by fingerprint
    if not s1.get("ok"):
        fails.append(f"valid submit failed: {s1.get('errors')}")
    if not s2.get("deduped"):
        fails.append("resubmit of identical proposal was not deduped")
    if s1.get("hypothesis_id") != s2.get("hypothesis_id"):
        fails.append("dedup returned a different hypothesis_id")
    if r.hget(P.K.UNIFY_SUBMITS, "ga_params") is None:
        fails.append("submit counter not incremented")
    print(f"[2] submit ok={s1.get('ok')} hid={str(s1.get('hypothesis_id'))[:8]} deduped2={s2.get('deduped')} "
          f"count={r.hget(P.K.UNIFY_SUBMITS, 'ga_params')}")

    # 3. invalid proposal (no falsifier, out-of-bounds candidate) → rejected, not applied
    bad = {"target": "scibrain.router_strength", "intervention": {"kind": "param_set", "candidate": 9.9},
           "parameter_bounds": [0.0, 1.0], "expected_effect": "x"}
    s3 = P.submit(r, "ga_params", bad)
    if s3.get("ok"):
        fails.append("invalid proposal was accepted")
    print(f"[3] invalid submit rejected={not s3.get('ok')} errors={len(s3.get('errors') or [])}")

    # 4. unknown proposer recorded but flagged
    s4 = P.submit(r, "totally_unknown", dict(prop, intervention={"kind": "param_set", "candidate": 0.8}))
    if s4.get("known_producer"):
        fails.append("unknown proposer flagged as known")
    print(f"[4] unknown proposer known={s4.get('known_producer')} ok={s4.get('ok')}")

    # 5. audit_bypass: every producer accounted; open_bypasses ⊆ ids; invariants ok
    a = P.audit_bypass(r)
    if not a.get("available"):
        fails.append("audit unavailable")
    if a.get("n_producers") != len(P.PRODUCERS):
        fails.append("audit n_producers mismatch")
    if not set(a.get("open_bypasses", [])).issubset(set(ids)):
        fails.append("open_bypasses not a subset of producer ids")
    if not all(i["ok"] for i in a.get("invariants", [])):
        fails.append("an audit invariant failed")
    # with no governance module importable here, fcode gates return None → conservative 'could apply';
    # legacy_direct producers therefore show as open bypasses → audit must still be internally consistent
    print(f"[5] audit available={a.get('available')} n={a.get('n_producers')} "
          f"open_bypasses={a.get('n_open_bypasses')} kernel_routed={a.get('kernel_routed')} "
          f"by_mode={a.get('by_effective_mode')}")

    # 5b. bayes_threshold is now bounded_recorded (kernel-applied controller), NOT an open bypass
    bayes_row = next((row for row in a["producers"] if row["id"] == "bayes_threshold"), None)
    if bayes_row is None or bayes_row["base_mode"] != "bounded_recorded":
        fails.append("bayes_threshold not reclassified to bounded_recorded")
    if bayes_row and bayes_row["open_bypass"]:
        fails.append("bayes_threshold still flagged as an open bypass")
    print(f"[5b] bayes_threshold base_mode={bayes_row and bayes_row['base_mode']} "
          f"open_bypass={bayes_row and bayes_row['open_bypass']}")

    # 5c. apply_controller: bounded clamp + audit ledger + byte-identical in-bounds write
    ac1 = P.apply_controller(r, "bayes_threshold", redis_key="bayes_threshold:t_high",
                             value=42.0, bounds=(25.0, 60.0), reason="smoke")
    if not ac1["applied"] or ac1["value"] != 42.0 or ac1["clamped"]:
        fails.append(f"in-bounds apply_controller wrong: {ac1}")
    if float(r.get("bayes_threshold:t_high")) != 42.0:   # redis stores str/bytes — compare numerically
        fails.append("apply_controller did not write the live key")
    ac2 = P.apply_controller(r, "bayes_threshold", redis_key="bayes_threshold:t_high",
                             value=99.0, bounds=(25.0, 60.0), reason="smoke-oob")
    if not ac2["clamped"] or ac2["value"] != 60.0:
        fails.append(f"out-of-bounds value not clamped: {ac2}")
    ledger = r.lrange(P.K.UNIFY_CONTROLLER.format(proposer="bayes_threshold"), 0, -1)
    last = P.controller_last(r, "bayes_threshold")
    if len(ledger) != 2:
        fails.append(f"controller ledger should have 2 records, has {len(ledger)}")
    if not last or last.get("applies") != 2:
        fails.append(f"controller_last applies counter wrong: {last}")
    print(f"[5c] apply_controller in_bounds={ac1['value']} clamped_oob={ac2['value']} "
          f"ledger_len={len(ledger)} applies={last and last.get('applies')}")

    # 5d. f9f12_decoder is bounded_recorded; apply_set writes membership + records added/removed diff
    f9_row = next((row for row in a["producers"] if row["id"] == "f9f12_decoder"), None)
    if not f9_row or f9_row["base_mode"] != "bounded_recorded" or f9_row["open_bypass"]:
        fails.append("f9f12_decoder not reclassified to bounded_recorded / still a bypass")
    r.kv["brain:pair_probation"] = json.dumps({"AAAUSDT": {"added_ts": 1}, "BBBUSDT": {"added_ts": 2}})
    sset = P.apply_set(r, "f9f12_decoder", redis_key="brain:pair_probation",
                       mapping={"BBBUSDT": {"added_ts": 2}, "CCCUSDT": {"added_ts": 3}},
                       max_size=200, count_key="pair:probation:count", reason="smoke")
    if sset["added"] != ["CCCUSDT"] or sset["removed"] != ["AAAUSDT"]:
        fails.append(f"apply_set diff wrong: {sset}")
    if json.loads(r.get("brain:pair_probation")).keys() != {"BBBUSDT", "CCCUSDT"}:
        fails.append("apply_set did not write the membership")
    if r.get("pair:probation:count") != "2":
        fails.append(f"apply_set count_key wrong: {r.get('pair:probation:count')}")
    # max_size cap (runaway guard): 5 entries, cap 3 → keep the 3 newest by added_ts
    big = {f"P{i}USDT": {"added_ts": i} for i in range(5)}
    scap = P.apply_set(r, "f9f12_decoder", redis_key="brain:pair_suspension", mapping=big,
                       max_size=3, count_key="pair:suspension:count", reason="smoke-cap")
    if not scap["capped"] or scap["n"] != 3:
        fails.append(f"apply_set max_size cap failed: {scap}")
    kept = set(json.loads(r.get("brain:pair_suspension")).keys())
    if kept != {"P2USDT", "P3USDT", "P4USDT"}:
        fails.append(f"cap kept wrong entries: {kept}")
    print(f"[5d] f9f12 base_mode={f9_row and f9_row['base_mode']} added={sset['added']} "
          f"removed={sset['removed']} cap_kept={sorted(kept)} capped={scap['capped']}")

    # 5e. ga_params bounded_recorded; apply_params clamps per-param + records per-param diff
    ga_row = next((row for row in a["producers"] if row["id"] == "ga_params"), None)
    if not ga_row or ga_row["base_mode"] != "bounded_recorded" or ga_row["open_bypass"]:
        fails.append("ga_params not reclassified to bounded_recorded / still a bypass")
    gbounds = {"kelly_fraction": (0.10, 0.50), "turbulence_cap": (1.5, 5.0)}
    r.kv["ga:best_params"] = json.dumps({"kelly_fraction": 0.20, "turbulence_cap": 3.0})
    ap = P.apply_params(r, "ga_params", redis_key="ga:best_params",
                        params={"kelly_fraction": 0.99, "turbulence_cap": 3.0},  # kelly out of bounds
                        bounds=gbounds, reason="smoke")
    if "kelly_fraction" not in ap["clamped"] or ap["params"]["kelly_fraction"] != 0.50:
        fails.append(f"apply_params did not clamp out-of-bounds kelly: {ap}")
    if "kelly_fraction" not in ap["changed"] or "turbulence_cap" in ap["changed"]:
        fails.append(f"apply_params changed-diff wrong: {ap['changed']}")
    written = json.loads(r.get("ga:best_params"))
    if written != {"kelly_fraction": 0.50, "turbulence_cap": 3.0}:
        fails.append(f"apply_params wrote wrong params: {written}")
    print(f"[5e] ga_params base_mode={ga_row and ga_row['base_mode']} clamped={ap['clamped']} "
          f"changed={ap['changed']} written={written}")

    # 5f. the 5 model producers are model_recorded; apply_model_change records a ModelChangeSpec
    model_ids = {"opro_prompts", "dgm_code_rewrite", "ai_scientist", "strategy_pool", "dsl_miner"}
    for mid_ in model_ids:
        row = next((x for x in a["producers"] if x["id"] == mid_), None)
        if not row or row["base_mode"] != "model_recorded" or row["open_bypass"]:
            fails.append(f"{mid_} not reclassified to model_recorded / still a bypass")
    mc = P.apply_model_change(r, "opro_prompts", kind="prompt", target="opro:prompt_addendum",
                              summary="smoke prompt change", reason="smoke")
    if not mc["recorded"] or not mc["model_id"]:
        fails.append(f"apply_model_change did not record: {mc}")
    P.apply_model_change(r, "opro_prompts", kind="prompt", target="opro:prompt_addendum",
                         summary="smoke prompt change 2", reason="smoke")
    mlast = P.model_last(r, "opro_prompts")
    mled = r.lrange(P.K.UNIFY_MODEL.format(proposer="opro_prompts"), 0, -1)
    if not mlast or mlast.get("records") != 2 or len(mled) != 2:
        fails.append(f"model ledger/last wrong: last={mlast} ledger_len={len(mled)}")
    print(f"[5f] 5 model producers model_recorded; apply_model_change model_id={mc['model_id'][:8]} "
          f"records={mlast and mlast.get('records')} ledger_len={len(mled)}")

    # 5g. THE GOAL: with all producers migrated, NO open bypasses remain
    if a["n_open_bypasses"] != 0:
        fails.append(f"expected 0 open bypasses, got {a['n_open_bypasses']}: {a['open_bypasses']}")
    print(f"[5g] open_bypasses={a['n_open_bypasses']} (GOAL=0) by_mode={a['by_effective_mode']}")

    # 6. summary publishes UNIFY_SUMMARY
    P.summary(r)
    pub = r.get(P.K.UNIFY_SUMMARY)
    if not pub or not json.loads(pub).get("available"):
        fails.append("summary did not publish a valid UNIFY_SUMMARY")
    print(f"[6] summary published={bool(pub)}")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
