"""Phase 5 in-RAM universe snapshot — correctness: frames must be BIT-IDENTICAL to live Redis.

Run: docker compose exec -T brain python -m signals.scibrain._smoke_snapshot
Uses live Redis (real symbols). The whole point of the cache is to change WHERE the bytes come
from (RAM vs socket), never WHAT the frame contains — so a real symbol's SensorFrame built from the
snapshot must equal the one built straight from Redis.
"""
from __future__ import annotations

import numpy as np

from . import keys as K
from . import sensor_bus
from .sensor_bus import SnapshotReader, build_frame, bulk_load


def _frames_equal(a, b) -> list[str]:
    diffs = []
    if set(a.candles) != set(b.candles):
        diffs.append(f"candle TFs differ: {set(a.candles)} vs {set(b.candles)}")
    for tf in set(a.candles) & set(b.candles):
        if not np.array_equal(a.candles[tf], b.candles[tf]):
            diffs.append(f"candles[{tf}] differ shape {a.candles[tf].shape} vs {b.candles[tf].shape}")
    for fld in ("ofi", "vpin", "funding", "oi_change_5m", "oi_change_z", "sentiment", "last_price"):
        if getattr(a, fld) != getattr(b, fld):
            diffs.append(f"{fld}: {getattr(a, fld)} != {getattr(b, fld)}")
    if a.cn_forecasts != b.cn_forecasts:
        diffs.append("cn_forecasts differ")
    return diffs


def test_snapshot_reader_unit():
    snap = {"k:str": "1.5", "k:list": ["a", "b"], "k:none": None}
    rdr = SnapshotReader(snap, fallback=None)
    assert rdr.get("k:str") == "1.5"
    assert rdr.lrange("k:list", 0, 10) == ["a", "b"]
    assert rdr.lrange("k:none", 0, 10) == []          # None stored ⇒ empty list, not crash
    assert rdr.get("absent") is None                  # miss, no fallback ⇒ None

    class _FB:
        def get(self, k): return "FB"
        def lrange(self, k, s, e): return ["FB"]
    rdr2 = SnapshotReader(snap, fallback=_FB())
    assert rdr2.get("absent") == "FB"                 # miss falls back to live
    print("  PASS SnapshotReader get/lrange/fallback")


def test_bit_identical_frames():
    import redis_client
    r = redis_client.get()
    # pick real symbols that actually have candles right now
    syms = [s.decode() if isinstance(s, bytes) else s
            for s in (r.zrevrange(K.LAST_DECISIONS, 0, 40) or [])]
    tested = 0
    for sym in syms:
        live = build_frame(r, sym)                    # straight from Redis
        if not live.candles:
            continue
        reader = bulk_load(r, [sym])                  # bulk pipelined → SnapshotReader
        snap = build_frame(reader, sym)               # _reader() returns the passed reader (global unset)
        diffs = _frames_equal(live, snap)
        assert not diffs, f"{sym} frame mismatch: {diffs}"
        tested += 1
        if tested >= 8:
            break
    assert tested >= 1, "no symbols with candles to test"
    print(f"  PASS bit-identical frames on {tested} real symbols (snapshot == live Redis)")


def test_global_install_and_clear():
    import redis_client
    r = redis_client.get()
    syms = [s.decode() if isinstance(s, bytes) else s
            for s in (r.zrevrange(K.LAST_DECISIONS, 0, 5) or [])]
    assert syms, "need a recent symbol"
    sym = syms[0]
    reader = bulk_load(r, [sym])
    sensor_bus.set_snapshot(reader)
    try:
        # with the global installed, build_frame(r, sym) must use the snapshot (not r)
        f_global = build_frame(r, sym)
        f_direct = build_frame(reader, sym)
        assert not _frames_equal(f_global, f_direct), "global-installed build_frame must use the snapshot"
    finally:
        sensor_bus.set_snapshot(None)
    assert sensor_bus._SNAPSHOT_READER is None, "snapshot must clear"
    print("  PASS global snapshot install routes build_frame through cache, then clears")


if __name__ == "__main__":
    test_snapshot_reader_unit()
    test_bit_identical_frames()
    test_global_install_and_clear()
    print("ALL PHASE-5 SNAPSHOT CORRECTNESS TESTS PASS")
