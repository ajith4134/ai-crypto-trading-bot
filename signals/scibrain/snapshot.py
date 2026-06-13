"""SciBrain Phase 7a — immutable EntrySnapshot (the scientific event ledger, design §5.1).

At every REAL open we freeze everything needed to (a) reproduce the decision deterministically and
(b) evaluate it off-policy later:

  • a unique snapshot_id + entry/decision timestamps;
  • deterministic raw-data replay fingerprints (per-TF candle window hash + last ts + scalar
    sensors) — storing only an explanation is NOT sufficient (§5.1); these fingerprints let a
    replay PROVE it used the same inputs;
  • exact version lineage: code fingerprint, git commit, config hash, contract/audit schema
    versions, and the active module set ("model versions" for this circuit);
  • the feasible ALTERNATIVE actions {long, short, abstain} and their logging PROPENSITIES, so the
    later counterfactual/IPS/DR evaluator can correct for action-selection bias;
  • the execution micro-state (mark, quote, spread, imbalance) at open;
  • a slot for the post-open LLM-audit provenance, merged later by audit.py.

Pure / best-effort: never raises into the opener — a failure returns a minimal snapshot carrying
the error so the open still proceeds and the gap is visible (C11).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid

import structlog

from .contracts import Decision, SensorFrame
from .interrogator import AUDIT_SCHEMA_VERSION

log = structlog.get_logger()

ENTRY_SNAPSHOT_SCHEMA_VERSION = 1

# Cached once per process — these only change on a code/config change (a process restart), never
# per-open, so it is correct (and cheap) to compute them once.
_CODE_FINGERPRINT: str | None = None
_CODE_COMMIT: str | None = None
_CONFIG_HASH: str | None = None


def _code_fingerprint() -> str:
    """Deterministic sha1 over the scibrain package source — a git-independent code version that
    pins WHICH circuit code produced this decision (so a replay/eval can detect code drift)."""
    global _CODE_FINGERPRINT
    if _CODE_FINGERPRINT is not None:
        return _CODE_FINGERPRINT
    try:
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        h = hashlib.sha1()
        for root, _dirs, files in os.walk(pkg_dir):
            if "__pycache__" in root:
                continue
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                with open(os.path.join(root, fn), "rb") as fh:
                    h.update(fn.encode("utf-8"))
                    h.update(fh.read())
        _CODE_FINGERPRINT = h.hexdigest()[:16]
    except Exception:
        _CODE_FINGERPRINT = "unknown"
    return _CODE_FINGERPRINT


def _code_commit() -> str:
    """Best-effort git commit (short). Falls back to env or 'unknown' — the code_fingerprint above
    is the authoritative, always-available code version."""
    global _CODE_COMMIT
    if _CODE_COMMIT is not None:
        return _CODE_COMMIT
    try:
        import subprocess
        _CODE_COMMIT = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL, timeout=3).decode().strip() or "unknown"
    except Exception:
        _CODE_COMMIT = os.environ.get("GIT_COMMIT", "unknown")
    return _CODE_COMMIT


def _config_hash() -> str:
    """sha1 of config.yaml content — pins the config lineage the gate requires. Cached."""
    global _CONFIG_HASH
    if _CONFIG_HASH is not None:
        return _CONFIG_HASH
    for path in ("/app/config.yaml", os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config.yaml")):
        try:
            with open(path, "rb") as fh:
                _CONFIG_HASH = hashlib.sha1(fh.read()).hexdigest()[:16]
                return _CONFIG_HASH
        except OSError:
            continue
    _CONFIG_HASH = "unknown"
    return _CONFIG_HASH


def _data_fingerprint(frame: SensorFrame) -> dict:
    """Per-TF deterministic fingerprint of the EXACT candle window the circuit saw: the closing
    series is hashed so a replay can verify byte-identical inputs, plus the last bar ts + count."""
    per_tf = {}
    for tf, arr in (frame.candles or {}).items():
        try:
            closes = arr[:, 4].astype(float)
            per_tf[tf] = {
                "n": int(len(closes)),
                "last_ts": (None if not len(arr) else float(arr[-1, 0])),
                "closes_sha1": hashlib.sha1(closes.tobytes()).hexdigest()[:16],
            }
        except Exception:
            continue
    return per_tf


def _action_propensities(dec: Decision, temperature: float) -> dict:
    """Feasible actions {long, short, abstain} + their LOGGING propensities.

    The live policy is a deterministic argmax, but off-policy evaluation (IPS/DR) needs a soft
    propensity for the action it took. We log a principled one:
        net_vote v = Σ(direction·conviction) / Σ|conviction|   over OK modules  (signed strength)
        p(act)     = conviction                                 (how decisively the circuit acted)
        p(long|act)= sigmoid(v / T)                             (direction split by signed strength)
      ⇒ p_long = p_act·σ(v/T),  p_short = p_act·(1−σ(v/T)),  p_abstain = 1 − p_act
    These sum to 1 and recover the chosen action's selection probability for later bias correction.
    """
    num = den = 0.0
    for m in dec.contributing:
        if getattr(m, "ok", False):
            num += float(m.direction) * float(m.conviction)
            den += abs(float(m.conviction))
    v = max(-1.0, min(1.0, (num / den) if den > 1e-9 else 0.0))
    c = max(0.0, min(1.0, float(dec.conviction)))
    T = max(1e-3, float(temperature))
    s = 1.0 / (1.0 + math.exp(-v / T))                 # σ(v/T) = P(long | acting)
    p_long = round(c * s, 4)
    p_short = round(c * (1.0 - s), 4)
    p_abstain = round(1.0 - c, 4)
    chosen = dec.direction if dec.direction in ("long", "short") else "abstain"
    chosen_p = {"long": p_long, "short": p_short, "abstain": p_abstain}[chosen]
    return {
        "feasible": ["long", "short", "abstain"],
        "propensities": {"long": p_long, "short": p_short, "abstain": p_abstain},
        "chosen": chosen,
        "chosen_propensity": chosen_p,
        "net_vote": round(v, 6),
        "conviction": round(c, 6),
        "temperature": round(T, 4),
        "policy": "deterministic_argmax_logged_soft",
    }


def _exec_microstate(r, pair: str, mark: float) -> dict:
    """The execution quote/spread state at open. Captures what the bot actually tracks; depth and
    realized slippage are NOT tracked per-pair (honest None), to be added when a feed exists."""
    def _g(key):
        try:
            v = r.get(key)
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None
    bid = _g(f"{pair}:bid")
    ask = _g(f"{pair}:ask")
    spread = _g(f"{pair}:micro:spread")
    if spread is None and bid and ask and (bid + ask) > 0:
        spread = (ask - bid) / ((ask + bid) / 2.0)
    return {
        "mark": (round(float(mark), 10) if mark else None),
        "bid": bid, "ask": ask,
        "spread_rel": (round(spread, 8) if spread is not None else None),
        "bid_ask_imbalance": _g(f"{pair}:bid_ask_imbalance"),
        "micro_ofi_l1": _g(f"{pair}:micro:ofi_l1"),
        "depth": None,          # not tracked per-pair yet
        "slippage": None,       # realized slippage unknown at open
    }


def build_entry_snapshot(r, dec: Decision, mark: float, *,
                         frame: SensorFrame | None = None) -> dict:
    """Assemble the immutable EntrySnapshot for one open. Embedded into signals_at_entry by the
    opener; the heavy decision/module/router/fusion state already lives alongside it under
    decision_snapshot/influence_manifest, which this references via replay_pointers (no dup)."""
    snap: dict = {
        "snapshot_id": uuid.uuid4().hex,
        "schema_version": ENTRY_SNAPSHOT_SCHEMA_VERSION,
        "symbol": dec.symbol,
        "entry_ts": round(time.time(), 3),
        "decision_ts": round(float(dec.ts), 3),
    }
    try:
        if frame is None:
            from .sensor_bus import build_frame
            frame = build_frame(r, dec.symbol)
        try:
            temp = float(r.get("scibrain:propensity_temp") or 0.5)
        except (TypeError, ValueError):
            temp = 0.5
        snap["replay"] = {
            "data_fingerprint": _data_fingerprint(frame),
            "sensor_scalars": {
                "ofi": frame.ofi, "vpin": frame.vpin, "funding": frame.funding,
                "oi_change_5m": frame.oi_change_5m, "oi_change_z": frame.oi_change_z,
                "sentiment": frame.sentiment, "last_price": frame.last_price,
            },
            "note": ("fingerprints recorded at open from the same closed-candle series the circuit "
                     "scored; entry_ts may differ from decision_ts by one throttle window"),
        }
        snap["versions"] = {
            "code_fingerprint": _code_fingerprint(),
            "code_commit": _code_commit(),
            "config_hash": _config_hash(),
            "entry_snapshot_schema": ENTRY_SNAPSHOT_SCHEMA_VERSION,
            "audit_schema": AUDIT_SCHEMA_VERSION,
            "decision_regime": dec.regime,
            "module_set": sorted(m.module for m in dec.contributing),
            "router_present": bool(dec.router),
        }
        snap["action_space"] = _action_propensities(dec, temp)
        snap["execution"] = _exec_microstate(r, dec.symbol, mark)
        snap["replay_pointers"] = {
            "decision_snapshot": "signals_at_entry.decision_snapshot",
            "influence_manifest": "signals_at_entry.influence_manifest",
            # the ex-ante LLM audit (provider/model/prompt_hash/schema_version) is merged here
            # post-open by audit.py; its close-time grade lands at signals_at_entry.audit_calibration
            "llm_audit": "signals_at_entry.audit",
            "audit_calibration": "signals_at_entry.audit_calibration",
        }
        snap["complete"] = True
    except Exception as exc:
        log.warning("scibrain_entry_snapshot_failed", symbol=dec.symbol, error=str(exc)[:160])
        snap["complete"] = False
        snap["error"] = str(exc)[:160]
    return snap
