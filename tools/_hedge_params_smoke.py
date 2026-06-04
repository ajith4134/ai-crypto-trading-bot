"""Smoke test for risk/hedge_params.py — verify the constant-α MC update
applies correctly against real Redis. Cleans up afterwards."""
import sys
import redis_client
from risk.hedge_params import (
    sample_open_params, record_outcome, get_all_learned,
)


def main() -> int:
    r = redis_client.get()
    # Snapshot any existing keys so we don't clobber live state.
    prior_keys = {
        k.decode() if isinstance(k, bytes) else k: r.get(k)
        for k in r.keys("hedge:learned_params:*")
    }
    prior_counters = {
        k: r.get(k) for k in
        ("hedge_params:updates_count", "hedge_params:last_update_ts",
         "hedge_params:last_reward")
    }

    # Wipe so the smoke test starts from defaults
    for k in prior_keys:
        r.delete(k)
    for k in prior_counters:
        r.delete(k)

    print("STAGE 1: defaults ---")
    for p in get_all_learned():
        print(f"  {p['name']:<28} current={p['current']:.4f} n={p['n_samples']}")

    # Winning hedge — params should drift toward the value used
    params1 = sample_open_params()
    print("\nSTAGE 2: simulate WIN with sampled params:")
    for k, v in params1.items():
        print(f"  used  {k:<28} {v:.4f}")
    res1 = record_outcome(params1, net_pnl_usdt=12.0, capital_usdt=100.0)
    print(f"win  → reward={res1['reward']}")

    # Losing hedge
    params2 = sample_open_params()
    print("\nSTAGE 3: simulate LOSS with sampled params:")
    for k, v in params2.items():
        print(f"  used  {k:<28} {v:.4f}")
    res2 = record_outcome(params2, net_pnl_usdt=-8.0, capital_usdt=80.0)
    print(f"loss → reward={res2['reward']}")

    print("\nSTAGE 4: final learned state ---")
    for p in get_all_learned():
        print(f"  {p['name']:<28} current={p['current']:.4f} n={p['n_samples']} "
              f"trusted={p['trusted']}")

    print(f"\nupdates_count = {r.get('hedge_params:updates_count')}")
    print(f"last_reward   = {r.get('hedge_params:last_reward')}")

    # Restore prior live state (so this test doesn't contaminate production
    # counters or any partial learning that was in progress).
    for k in prior_keys:
        r.delete(k)
    for k, v in prior_keys.items():
        if v is not None:
            r.set(k, v)
    for k in prior_counters:
        r.delete(k)
    for k, v in prior_counters.items():
        if v is not None:
            r.set(k, v)
    print("\nsmoke cleanup complete (restored prior state)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
