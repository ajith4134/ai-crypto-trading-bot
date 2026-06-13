"""Smoke for the brainstem safety projection (brainstem.py) — builds against LIVE Redis (run in container).
Run: python -m signals.scibrain._smoke_brainstem"""
from __future__ import annotations


def main() -> int:
    import redis_client
    from signals.scibrain import brainstem as B
    r = redis_client.get()
    fails: list[str] = []

    ss = B.safe_set(r)
    print(f"[1] SafeSet: running={ss['kill_switch_running']} size[{ss['min_position_usdt']},"
          f"{ss['max_position_usdt']}] lev_cap={ss['leverage_ceiling']} max_open={ss['max_open_trades']} "
          f"open_now={ss['open_now']} headroom={ss['exposure_headroom']}")
    if ss["max_position_usdt"] > B._HARD_POSITION_CEIL or ss["leverage_ceiling"] > B._HARD_LEVERAGE_CEIL:
        fails.append("SafeSet exceeds hard ceilings")

    # 2. projection CLAMPS an over-spec action to the caps (use a permissive injected safeset so the
    #    clamp path is deterministic regardless of live exposure)
    free_ss = dict(ss, kill_switch_running=True, exposure_headroom=5,
                   min_position_usdt=10.0, max_position_usdt=25.0, leverage_ceiling=5.0)
    a = B.project_action(r, {"direction": "long", "size_usdt": 999.0, "leverage": 50.0},
                         symbol="SMOKETESTUSDT", safeset=free_ss)
    if not a.admissible or a.size_usdt != 25.0 or a.leverage != 5.0:
        fails.append(f"clamp failed: {a.to_dict()}")
    print(f"[2] clamp 999/50x → {a.to_dict()['size_usdt']}/{a.to_dict()['leverage']}x admissible={a.admissible} "
          f"interventions={a.interventions}")

    # 3. undersized clamps UP to the floor
    a2 = B.project_action(r, {"direction": "short", "size_usdt": 1.0, "leverage": 0.2},
                          symbol="SMOKETESTUSDT", safeset=free_ss)
    if a2.size_usdt != 10.0 or a2.leverage != 1.0:
        fails.append(f"floor clamp failed: {a2.to_dict()}")
    print(f"[3] floor 1/0.2x → {a2.size_usdt}/{a2.leverage}x")

    # 4. hard VETOES → baseline abstain (no position)
    veto_cases = {
        "kill_switch_off": dict(free_ss, kill_switch_running=False),
        "max_open_reached": dict(free_ss, exposure_headroom=0),
    }
    for label, vss in veto_cases.items():
        av = B.project_action(r, {"direction": "long", "size_usdt": 20.0, "leverage": 5.0},
                              symbol="X", safeset=vss)
        if av.admissible or av.direction is not None or av.size_usdt != 0.0:
            fails.append(f"{label} should abstain, got {av.to_dict()}")
    # no-direction + stale-data also abstain
    a_nd = B.project_action(r, {"direction": None, "size_usdt": 20.0}, safeset=free_ss)
    a_stale = B.project_action(r, {"direction": "long", "size_usdt": 20.0}, safeset=free_ss, data_stale=True)
    if a_nd.admissible or a_stale.admissible:
        fails.append("no-direction or stale-data did not abstain")
    print(f"[4] vetoes abstain: kill_switch/max_open/no_dir/stale → all admissible=False")

    # 5. baseline_fallback is abstain; safety_status invariants hold + published
    bf = B.baseline_fallback("test")
    if bf.direction is not None or bf.size_usdt != 0.0 or bf.admissible:
        fails.append(f"baseline_fallback not abstain: {bf.to_dict()}")
    status = B.safety_status(r, publish=True)
    if not status.get("available") or not status.get("all_invariants_ok"):
        fails.append(f"safety_status invariants failed: {[i for i in status.get('invariants',[]) if not i['ok']]}")
    print(f"[5] baseline=abstain; status invariants_ok={status.get('all_invariants_ok')} "
          f"reflexes={status.get('reflexes')}")
    import json
    pub = r.get("scibrain:brainstem")
    if not pub or not json.loads(pub).get("available"):
        fails.append("not published to scibrain:brainstem")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
