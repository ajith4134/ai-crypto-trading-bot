"""VS-1 smoke test — run the SciBrain compute core on live Redis symbols.
Run inside the brain container:  docker exec trading-bot-brain-1 python -m signals.scibrain._smoke
Read-only; no orders, no writes.
"""
import os
import redis

from signals.scibrain.sensor_bus import build_frame
from signals.scibrain.modules import MODULES
from signals.scibrain import fusion


def main():
    r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"),
                    port=int(os.environ.get("REDIS_PORT", "6379")),
                    decode_responses=True)
    print("redis ping:", r.ping())
    syms = list(r.srandmember("scanner:active_pairs", 6) or [])
    print("test symbols:", syms)
    for sym in syms:
        frame = build_frame(r, sym)
        tfs = {tf: len(a) for tf, a in frame.candles.items()}
        outs = [m.evaluate(frame) for m in MODULES]
        dec = fusion.fuse(sym, outs, frame)
        print(f"\n=== {sym} candles={tfs} last={frame.last_price} "
              f"ofi={frame.ofi} ===")
        for o in outs:
            print(f"  [{o.module:8s}] dir={o.direction:+.3f} conv={o.conviction:.3f} "
                  f"regime={o.regime_tag} :: {o.explanation}")
        print(f"  >> DECISION: {dec.direction} conviction={dec.conviction:.3f} "
              f"exp_move={dec.expected_move_pct} size={dec.size_frac:.4f} "
              f"regime={dec.regime}")


if __name__ == "__main__":
    main()
