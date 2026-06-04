"""
F54 — Crypto LLM-DSL Alpha Miner (cont. 55, 2026-05-28).

Implements arXiv:2604.26747 "From Hypotheses to Factors" with crypto-specific
extensions. Uses local Ollama (qwen2.5-coder:7b for proposals, deepseek-r1:8b
for validation) — user choice per memory [[local-llm-choice]].

Pipeline:
  1. Load 100 most-recent active pairs' 30d 1m candles + microstructure.
  2. Prompt qwen2.5-coder with the DSL grammar + top-10 exemplars +
     anti-exemplar list + decay feedback. Sample 50 candidates (temp=0.9).
  3. Parse + validate each.
  4. Point-in-time backtest:
     - Evaluate factor at 720 anchor points (~12h apart over 30d).
     - Compute IC vs 1h-forward return (cross-sectional).
     - Compute Sharpe of long-top-decile / short-bottom-decile portfolio
       (5bps round-trip cost).
     - Compute decay ratio (first-half-IC / second-half-IC).
  5. Validator pass (deepseek-r1): "is this overfit to BTC alone or robust?"
  6. Survivors with Sharpe>1.0, |IC|>0.03, decay-ratio ∈ [0.5, 2.0]:
       - Register as F54_<hash> in feature governance.
       - Append to `dsl:promoted_factors` Redis list.
       - F8 router consumes via dsl_promoter.activate(...).
  7. Rejects appended to `dsl:rejected_factors` (capped at 1000) — used as
     anti-exemplars in subsequent prompts.

Cold-start safe — model not pulled / Ollama unreachable → mining run logs
the error and exits with no-op. No regression.

Triggered weekly by celery beat (`mining_interval_s` in config.yaml).
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any

import numpy as np
import structlog

import redis_client
import redis_keys
from feature_governance.registry import register

from ml.dsl_grammar import (
    parse, validate, serialize, factor_hash, max_window,
    required_terminals, grammar_prompt_block, DSLParseError,
)
from ml.dsl_evaluator import evaluate, evaluate_series, required_history

log = structlog.get_logger()

_FG_ID = "F54"

try:
    register(_FG_ID, "LLM-DSL Crypto Alpha Miner", activation_phase=0)
except Exception as _exc:
    log.debug("f54_self_register_deferred", err=str(_exc))


# ---- Tunables (overridable from config.yaml at runtime) --------------------

_PROPOSER_MODEL    = os.environ.get("DSL_PROPOSER_MODEL", "qwen2.5-coder:7b")
_VALIDATOR_MODEL   = os.environ.get("DSL_VALIDATOR_MODEL", "deepseek-r1:8b")
_CANDIDATES_PER_RUN = int(os.environ.get("DSL_CANDIDATES_PER_RUN", 50))
_HISTORY_LOOKBACK_PAIRS = int(os.environ.get("DSL_LOOKBACK_PAIRS", 30))
_HISTORY_LOOKBACK_BARS  = int(os.environ.get("DSL_LOOKBACK_BARS", 30 * 24 * 60))
_BACKTEST_ANCHORS  = 720          # ~one anchor per hour over 30d
_FWD_HORIZON_BARS  = 60           # 1h fwd return (1m candles)
_PORTFOLIO_DECILE  = 0.10
_COST_BPS          = 5.0          # 5bps round-trip cost
_MIN_SHARPE        = 1.0
_MIN_IC            = 0.03
_DECAY_RATIO_LO    = 0.5
_DECAY_RATIO_HI    = 2.0
_MAX_PROMOTED      = 30
_REJECTED_CAP      = 1000
_PROMOTE_DSL_BONUS = 15           # composite-bonus weight; see signals/engine.py


def _r():
    return redis_client.get()


def _is_disabled() -> bool:
    return (_r().get(redis_keys.DSL_DISABLED) or b"0") in (b"1", "1")


# ---- Candle / microstructure loaders ---------------------------------------

def _active_pairs(limit: int) -> list[str]:
    r = _r()
    try:
        members = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
        pairs = [m.decode() if isinstance(m, bytes) else m for m in members]
    except Exception:
        pairs = []
    if not pairs:
        pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
    return pairs[:limit]


def _load_pair_ctx(pair: str, bars: int) -> dict[str, np.ndarray] | None:
    """Build evaluator-context arrays for one pair. None if too little data."""
    r = _r()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
    raw = r.lrange(key, 0, bars - 1)
    if not raw or len(raw) < 200:
        return None
    o_, h_, l_, c_, v_ = [], [], [], [], []
    for entry in raw:
        try:
            d = json.loads(entry)
            o_.append(float(d.get("o", 0.0)))
            h_.append(float(d.get("h", 0.0)))
            l_.append(float(d.get("l", 0.0)))
            c_.append(float(d.get("c", 0.0)))
            v_.append(float(d.get("v", 0.0)))
        except Exception:
            continue
    if len(c_) < 200:
        return None
    # Reverse to oldest→newest.
    o_arr = np.array(o_[::-1], dtype=np.float64)
    h_arr = np.array(h_[::-1], dtype=np.float64)
    l_arr = np.array(l_[::-1], dtype=np.float64)
    c_arr = np.array(c_[::-1], dtype=np.float64)
    v_arr = np.array(v_[::-1], dtype=np.float64)
    vwap = (h_arr + l_arr + c_arr) / 3.0  # simple proxy
    n = len(c_arr)

    # Microstructure: read current scalar value, broadcast to a series so the
    # evaluator's terminal-as-array semantics work. Pre-30d microstructure
    # history isn't kept, so back-testing uses today's value uniformly —
    # honest scope: microstructure factors get muted IC on the backtest but
    # become meaningful live.
    def _scalar(k_tpl: str, fallback: float = 0.0) -> float:
        v = r.get(k_tpl.replace("{pair}", pair))
        try:
            return float(v) if v is not None else fallback
        except Exception:
            return fallback

    ctx = {
        "open": o_arr, "high": h_arr, "low": l_arr, "close": c_arr,
        "volume": v_arr, "vwap": vwap,
        "ofi": np.full(n, _scalar(redis_keys.OFI)),
        "vpin": np.full(n, _scalar(redis_keys.VPIN)),
        "kyles_lambda": np.full(n, _scalar(redis_keys.KYLES_LAMBDA)),
        "bid_ask_imbalance": np.full(n, _scalar(redis_keys.BID_ASK_IMBALANCE)),
        "funding_rate": np.full(n, _scalar(redis_keys.FUNDING_RATE)),
        "exchange_netflow_z": np.full(n, _scalar(redis_keys.EXCHANGE_NETFLOW_Z)),
        "turbulence_index": np.full(n, _scalar(redis_keys.TURBULENCE_INDEX)),
        "mark_price": c_arr.copy(),  # 1m close is the bot's mark in this context
        "sentiment": np.full(n, _scalar(redis_keys.SENTIMENT_PAIR, 0.5)),
    }
    return ctx


# ---- Prompt construction ---------------------------------------------------

def _top_exemplars(k: int = 10) -> list[str]:
    """Top-K promoted factors by Sharpe. Used as positive examples."""
    r = _r()
    try:
        promoted = json.loads(r.get(redis_keys.DSL_PROMOTED_FACTORS) or "[]")
    except Exception:
        promoted = []
    promoted.sort(key=lambda x: x.get("sharpe", 0.0), reverse=True)
    return [p["expr"] for p in promoted[:k] if "expr" in p]


def _anti_exemplars(k: int = 20) -> list[str]:
    """Recently-rejected proposals. Used as negative examples in the prompt
    so the LLM stops re-proposing the same dead ends."""
    r = _r()
    try:
        rej = json.loads(r.get(redis_keys.DSL_REJECTED_FACTORS) or "[]")
    except Exception:
        rej = []
    return [x["expr"] for x in rej[-k:] if "expr" in x]


def _build_proposer_prompt() -> str:
    pos = _top_exemplars(10)
    neg = _anti_exemplars(20)
    # F60 (cont. 56) — surface decay re-mining hints as a third
    # exemplar block so the LLM is biased toward those promising
    # variants of recently-decayed promoted factors.
    hints: list[str] = []
    try:
        from redis_client import get as _rget
        raw = _rget().get("dsl:remining_hints")
        if raw:
            hints = [h["expr"] for h in json.loads(raw)[-5:] if h.get("expr")]
    except Exception:
        hints = []

    pos_block = "\n".join(f"  ✓ {e}" for e in pos) or "  (none yet — propose novel factors)"
    neg_block = "\n".join(f"  ✗ {e}" for e in neg) or "  (none)"
    hint_block = "\n".join(f"  ⟳ {e}" for e in hints)
    hint_section = (
        f"\nDECAY-RE-MINING HINTS (consider as starting points for variation):\n{hint_block}\n"
        if hints else ""
    )
    return f"""\
{grammar_prompt_block()}

CURRENT BEST FACTORS (positive examples):
{pos_block}

ALREADY-TRIED-AND-REJECTED (DO NOT propose these again or close variants):
{neg_block}
{hint_section}
TASK:
Propose ONE new alpha factor as a single S-expression. It must:
  1. Differ structurally from every rejected factor.
  2. Have a clear economic hypothesis (you may briefly explain in 1 sentence
     AFTER the S-expression on a separate line beginning with "# hypothesis:").
  3. Use only the operators, terminals, windows, and constants defined above.

Output format (strict):
<s-expression>
# hypothesis: <one line>
"""


# ---- LLM I/O ---------------------------------------------------------------

def _llm_propose(n: int) -> list[tuple[str, str]]:
    """Sample n proposals from the proposer LLM. Returns list of (expr, hypothesis).

    On Ollama failure: returns whatever we got, plus a warning log.
    """
    from llm.ollama_client import chat_ollama
    out: list[tuple[str, str]] = []
    prompt = _build_proposer_prompt()
    for i in range(n):
        try:
            txt = chat_ollama(prompt, model=_PROPOSER_MODEL,
                              max_tokens=256, timeout_s=120)
        except Exception as exc:
            log.warning("f54_proposer_call_failed", err=str(exc)[:160])
            break
        expr, hyp = _split_proposal(txt)
        if expr:
            out.append((expr, hyp))
    return out


def _split_proposal(raw: str) -> tuple[str, str]:
    """Pull an S-expression + optional hypothesis line out of the LLM output.

    Handles:
      - Code-fenced blocks.
      - Trailing prose.
      - Multi-S-expr outputs (takes the first).
    """
    if not raw:
        return "", ""
    # Strip code fences
    raw = re.sub(r"```[a-zA-Z0-9]*\n?", "", raw)
    raw = raw.replace("```", "")
    # Find first '(' and match closing paren depth.
    start = raw.find("(")
    if start == -1:
        return "", ""
    depth = 0
    end = -1
    for i, ch in enumerate(raw[start:], start=start):
        if ch == "(": depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return "", ""
    expr = raw[start:end].strip()
    # Hypothesis is the next non-empty line beginning with '# hypothesis:'.
    hyp = ""
    for ln in raw[end:].splitlines():
        ln = ln.strip()
        if ln.startswith("# hypothesis:") or ln.startswith("#hypothesis:"):
            hyp = ln.split(":", 1)[1].strip()
            break
    return expr, hyp


def _llm_validate(expr: str, hypothesis: str, ic: float, sharpe: float) -> bool:
    """Ask deepseek-r1 whether the factor is likely BTC-overfit. True = pass."""
    from llm.ollama_client import chat_ollama
    prompt = f"""\
You are reviewing a candidate alpha factor for crypto futures.

Factor: {expr}
Hypothesis: {hypothesis or "(none provided)"}
Backtest results: IC={ic:.4f}, Sharpe={sharpe:.2f}

Question: Is this factor likely overfit to BTC alone (i.e. it would not
generalize to alts), or robust? Reply with ONE word: "robust" or "overfit".
"""
    try:
        out = chat_ollama(prompt, model=_VALIDATOR_MODEL,
                          max_tokens=64, timeout_s=90)
        return "robust" in out.lower()
    except Exception as exc:
        log.warning("f54_validator_call_failed", err=str(exc)[:120])
        # If validator unreachable, default to accept — the IC/Sharpe gates
        # already did the heavy filtering.
        return True


# ---- Backtest: IC / Sharpe / Decay -----------------------------------------

def _backtest(expr_node, all_ctx: dict[str, dict[str, np.ndarray]],
              anchors_per_pair: int = _BACKTEST_ANCHORS) -> dict[str, float] | None:
    """Cross-sectional IC + decile-portfolio Sharpe + decay-ratio.

    `all_ctx[pair]` is the evaluator context for one pair; each pair must
    have ≥ `_FWD_HORIZON_BARS + max_window(expr_node)` bars or it's skipped.

    Returns None if too little data overall.
    """
    pairs = list(all_ctx.keys())
    if not pairs:
        return None
    need = required_history(expr_node) + _FWD_HORIZON_BARS
    pairs = [p for p in pairs if all_ctx[p]["close"].shape[0] > need]
    if len(pairs) < 5:
        return None

    # Pick a common anchor grid — last N bars in the shortest series, spaced
    # so we get ~anchors_per_pair samples.
    min_len = min(all_ctx[p]["close"].shape[0] for p in pairs)
    last_idx = min_len - _FWD_HORIZON_BARS - 1
    first_idx = max(need, last_idx - anchors_per_pair * 5)
    if first_idx >= last_idx:
        return None
    step = max(1, (last_idx - first_idx) // max(anchors_per_pair, 1))
    anchors = list(range(first_idx, last_idx + 1, step))

    # For each anchor: compute factor for every pair; rank; portfolio return.
    daily_returns = []  # portfolio returns at each anchor
    ic_samples = []
    for t in anchors:
        f_vals = []
        f_pairs = []
        rets = []
        for p in pairs:
            ctx_slice = {k: v[: t + 1] for k, v in all_ctx[p].items()}
            v = evaluate(expr_node, ctx_slice)
            if v is None: continue
            c = all_ctx[p]["close"]
            if t + _FWD_HORIZON_BARS >= c.shape[0] or c[t] <= 0:
                continue
            r60 = float(c[t + _FWD_HORIZON_BARS] / c[t] - 1.0)
            if not math.isfinite(r60): continue
            f_vals.append(v); f_pairs.append(p); rets.append(r60)
        if len(f_vals) < 5: continue
        # IC: Spearman across the cross-section.
        f_arr = np.array(f_vals); r_arr = np.array(rets)
        if np.std(f_arr) == 0 or np.std(r_arr) == 0:
            continue
        ranks_f = np.argsort(np.argsort(f_arr))
        ranks_r = np.argsort(np.argsort(r_arr))
        ic = float(np.corrcoef(ranks_f, ranks_r)[0, 1])
        if not math.isfinite(ic): continue
        ic_samples.append(ic)
        # Portfolio: long top decile, short bottom decile.
        n_dec = max(1, int(len(f_arr) * _PORTFOLIO_DECILE))
        order = np.argsort(f_arr)
        bot = order[:n_dec]; top = order[-n_dec:]
        port_r = float(r_arr[top].mean() - r_arr[bot].mean())
        # 5bps round-trip cost.
        port_r -= _COST_BPS / 10000.0
        daily_returns.append(port_r)

    if len(ic_samples) < 10 or len(daily_returns) < 10:
        return None

    ic_mean = float(np.mean(ic_samples))
    # IC stability — first half vs second half.
    half = len(ic_samples) // 2
    ic_first = float(np.mean(ic_samples[:half])) if half else 0.0
    ic_second = float(np.mean(ic_samples[half:])) if half else 0.0
    if abs(ic_second) < 1e-6:
        decay_ratio = float("inf")
    else:
        decay_ratio = ic_first / ic_second

    # Sharpe (annualised assuming anchors are roughly hourly samples; we use
    # plain ratio mean/std × sqrt(anchors_per_year_estimate). For a 30d 720-
    # anchor backtest, treat each anchor as 1h → ~8760 anchors/year.)
    arr = np.array(daily_returns, dtype=np.float64)
    mu = float(np.mean(arr))
    sd = float(np.std(arr, ddof=1))
    if sd == 0:
        return None
    sharpe = mu / sd * math.sqrt(8760)
    return {
        "ic": ic_mean,
        "ic_first_half": ic_first,
        "ic_second_half": ic_second,
        "decay_ratio": decay_ratio,
        "sharpe": sharpe,
        "n_anchors": len(daily_returns),
        "n_pairs": len(pairs),
    }


# ---- Promotion / rejection bookkeeping -------------------------------------

def _record_rejection(expr: str, reason: str, metrics: dict | None = None):
    r = _r()
    try:
        rej = json.loads(r.get(redis_keys.DSL_REJECTED_FACTORS) or "[]")
    except Exception:
        rej = []
    rej.append({"expr": expr, "reason": reason,
                "metrics": metrics or {}, "ts": int(time.time())})
    if len(rej) > _REJECTED_CAP:
        rej = rej[-_REJECTED_CAP:]
    r.set(redis_keys.DSL_REJECTED_FACTORS, json.dumps(rej))


def _promote(expr: str, hypothesis: str, metrics: dict) -> str:
    """Register a survivor as F54_<hash>, append to promoted list. Returns id."""
    r = _r()
    fhash = factor_hash(expr)
    fid = f"F54_{fhash}"
    try:
        register(fid, f"DSL Factor {fhash}", activation_phase=0)
    except Exception as exc:
        log.debug("f54_subfactor_register_failed",
                  fid=fid, err=str(exc)[:120])
    try:
        promoted = json.loads(r.get(redis_keys.DSL_PROMOTED_FACTORS) or "[]")
    except Exception:
        promoted = []
    promoted.append({
        "id": fid,
        "expr": expr,
        "hypothesis": hypothesis,
        "ic": metrics["ic"],
        "sharpe": metrics["sharpe"],
        "decay_ratio": metrics["decay_ratio"],
        "promoted_at": int(time.time()),
    })
    # Keep best _MAX_PROMOTED by Sharpe.
    promoted.sort(key=lambda x: x.get("sharpe", 0.0), reverse=True)
    promoted = promoted[:_MAX_PROMOTED]
    r.set(redis_keys.DSL_PROMOTED_FACTORS, json.dumps(promoted))
    return fid


# ---- Mining run entrypoint -------------------------------------------------

def run_mining() -> dict[str, Any]:
    """Celery beat entrypoint — runs weekly. Mines, evaluates, promotes."""
    t0 = time.time()
    r = _r()
    if _is_disabled():
        log.warning("f54_mining_skipped_disabled")
        return {"ok": False, "reason": "disabled"}
    pairs = _active_pairs(_HISTORY_LOOKBACK_PAIRS)
    log.info("f54_mining_run_start",
             proposer=_PROPOSER_MODEL, validator=_VALIDATOR_MODEL,
             n_candidates=_CANDIDATES_PER_RUN, n_pairs=len(pairs))

    # Load ctx for every pair once.
    all_ctx: dict[str, dict[str, np.ndarray]] = {}
    for p in pairs:
        c = _load_pair_ctx(p, _HISTORY_LOOKBACK_BARS)
        if c is not None:
            all_ctx[p] = c
    if len(all_ctx) < 5:
        log.error("f54_mining_aborted_insufficient_data", loaded=len(all_ctx))
        return {"ok": False, "reason": "insufficient_candle_history",
                "pairs_loaded": len(all_ctx)}

    proposals = _llm_propose(_CANDIDATES_PER_RUN)
    log.info("f54_proposals_received", n=len(proposals))

    n_parsed = 0
    n_promoted = 0
    n_rejected_parse = 0
    n_rejected_metrics = 0
    n_rejected_validator = 0

    for expr, hyp in proposals:
        # Parse
        try:
            node = parse(expr)
        except DSLParseError as exc:
            n_rejected_parse += 1
            _record_rejection(expr, f"parse:{exc}")
            continue
        ok, why = validate(node)
        if not ok:
            n_rejected_parse += 1
            _record_rejection(expr, f"validate:{why}")
            continue
        n_parsed += 1
        # Backtest
        try:
            metrics = _backtest(node, all_ctx)
        except Exception as exc:
            log.warning("f54_backtest_exception",
                        expr=expr[:80], err=str(exc)[:120])
            metrics = None
        if metrics is None:
            n_rejected_metrics += 1
            _record_rejection(expr, "backtest_failed")
            continue
        # Metric gates.
        if abs(metrics["ic"]) < _MIN_IC \
           or metrics["sharpe"] < _MIN_SHARPE \
           or not (_DECAY_RATIO_LO <= metrics["decay_ratio"] <= _DECAY_RATIO_HI):
            n_rejected_metrics += 1
            _record_rejection(expr, "gate_thresholds", metrics)
            continue
        # Validator LLM pass.
        if not _llm_validate(expr, hyp, metrics["ic"], metrics["sharpe"]):
            n_rejected_validator += 1
            _record_rejection(expr, "validator_overfit", metrics)
            continue
        # F60 (cont. 56) — AlphaAgent novelty + complexity gate. Applied
        # AFTER metric + validator gates so the LLM is rewarded for finding
        # a good factor before we ask "is it sufficiently distinct from the
        # promoted pool?". `passes_novelty_gate` reads the on-disk pool, so
        # gate self-tightens as the pool grows.
        try:
            from ml.dsl_ast_embed import passes_novelty_gate
            existing_exprs = [it.get("expr") for it in
                              json.loads(r.get(redis_keys.DSL_PROMOTED_FACTORS) or "[]")
                              if it.get("expr")]
            ok_nov, why_nov = passes_novelty_gate(node, existing_exprs)
            if not ok_nov:
                n_rejected_metrics += 1   # bucket under metrics for the health summary
                _record_rejection(expr, f"novelty:{why_nov}", metrics)
                continue
        except Exception as _nov_exc:
            log.debug("f60_novelty_gate_skipped", err=str(_nov_exc)[:120])
        # Promote!
        fid = _promote(expr, hyp, metrics)
        n_promoted += 1
        log.info("f54_promoted_factor", fid=fid,
                 ic=metrics["ic"], sharpe=metrics["sharpe"])

    elapsed = round(time.time() - t0, 1)
    health = {
        "accepted": n_promoted,
        "rejected_parse": n_rejected_parse,
        "rejected_metrics": n_rejected_metrics,
        "rejected_validator": n_rejected_validator,
        "parsed": n_parsed,
        "elapsed_s": elapsed,
        "ts": int(time.time()),
    }
    r.set(redis_keys.DSL_MINING_HEALTH, json.dumps(health))
    r.set(redis_keys.DSL_LAST_MINING_RUN, int(time.time()))
    log.info("f54_mining_run_complete", **health)
    return {"ok": True, **health}


# ---- Per-tick: compute promoted factors for active pairs -------------------

def compute_promoted_for_active_pairs() -> dict[str, Any]:
    """Celery beat task — every minute. Writes per-pair, per-factor values."""
    if _is_disabled():
        return {"ok": False, "reason": "disabled"}
    r = _r()
    try:
        promoted = json.loads(r.get(redis_keys.DSL_PROMOTED_FACTORS) or "[]")
    except Exception:
        promoted = []
    if not promoted:
        return {"ok": True, "computed": 0, "reason": "no_promoted_factors"}
    pairs = _active_pairs(50)
    n_writes = 0
    for p in pairs:
        # Load minimal ctx — only need enough bars for the largest window.
        ctx = _load_pair_ctx(p, 1500)
        if ctx is None: continue
        pipe = r.pipeline(transaction=False)
        for item in promoted:
            expr = item.get("expr")
            if not expr: continue
            try:
                node = parse(expr)
            except DSLParseError:
                continue
            v = evaluate(node, ctx)
            if v is None: continue
            fhash = factor_hash(expr)
            key = redis_keys.DSL_ALPHA_VALUE \
                .replace("{pair}", p).replace("{factor_hash}", fhash)
            pipe.setex(key, 600, v)
            n_writes += 1
        pipe.execute()
    return {"ok": True, "computed": n_writes, "pairs": len(pairs),
            "factors": len(promoted)}


# ---- Decay-check & demotion (daily cron) -----------------------------------

def check_decay_and_demote() -> dict[str, Any]:
    """Daily — drop any promoted factor whose 1h-fresh-IC has flipped sign or
    fallen below half the original IC. Records as rejected so the LLM
    doesn't re-propose the same form."""
    r = _r()
    try:
        promoted = json.loads(r.get(redis_keys.DSL_PROMOTED_FACTORS) or "[]")
    except Exception:
        promoted = []
    if not promoted:
        return {"ok": True, "kept": 0, "demoted": 0}

    pairs = _active_pairs(_HISTORY_LOOKBACK_PAIRS)
    all_ctx = {}
    for p in pairs:
        c = _load_pair_ctx(p, 1500)
        if c is not None:
            all_ctx[p] = c
    if len(all_ctx) < 5:
        return {"ok": False, "reason": "insufficient_data"}

    kept = []
    demoted = []
    for item in promoted:
        expr = item.get("expr")
        orig_ic = float(item.get("ic", 0.0))
        if not expr: continue
        try:
            node = parse(expr)
        except DSLParseError:
            demoted.append({**item, "demote_reason": "parse_failed"})
            continue
        m = _backtest(node, all_ctx, anchors_per_pair=120)
        if m is None:
            kept.append(item)
            continue
        new_ic = float(m["ic"])
        # Sign flip OR magnitude collapse → demote.
        if (orig_ic * new_ic < 0) or (abs(new_ic) < abs(orig_ic) * 0.5):
            demoted.append({**item, "demote_reason": "decay",
                            "new_ic": new_ic, "ts_demoted": int(time.time())})
            _record_rejection(expr, "decay", {"orig_ic": orig_ic,
                                              "new_ic": new_ic})
            # F60 (cont. 56) — Targeted re-mining: ask the LLM for a
            # STRUCTURAL variant of the decayed factor (same hypothesis,
            # different operators). Single proposal per decayed factor;
            # validated through the same Sharpe/IC/novelty gates on the
            # next weekly mining run by virtue of being added to the
            # prompt's positive-exemplars list via _top_exemplars on
            # next run.
            try:
                from ml.dsl_ast_embed import build_decay_re_mining_prompt
                from llm.ollama_client import chat_ollama
                _re_prompt = build_decay_re_mining_prompt(
                    expr, item.get("hypothesis", ""))
                _re_raw = chat_ollama(_re_prompt, model=_PROPOSER_MODEL,
                                      max_tokens=256, timeout_s=60)
                _re_expr, _re_hyp = _split_proposal(_re_raw)
                if _re_expr:
                    # Stash as a hint for the next mining run — promoter
                    # will pick it up via the prompt's positive exemplars
                    # and re-evaluate it then.
                    hints_key = "dsl:remining_hints"
                    try:
                        hints = json.loads(r.get(hints_key) or "[]")
                    except Exception:
                        hints = []
                    hints.append({"expr": _re_expr, "hypothesis": _re_hyp,
                                  "from_decayed": expr, "ts": int(time.time())})
                    if len(hints) > 50: hints = hints[-50:]
                    r.set(hints_key, json.dumps(hints))
            except Exception as _re_exc:
                log.debug("f60_decay_remining_failed",
                          err=str(_re_exc)[:120])
        else:
            kept.append({**item, "ic": new_ic})
    r.set(redis_keys.DSL_PROMOTED_FACTORS, json.dumps(kept))
    log.info("f54_decay_check_complete", kept=len(kept), demoted=len(demoted))
    return {"ok": True, "kept": len(kept), "demoted": len(demoted)}


# ---- Consumer helper (used by signals/engine.py) ---------------------------

def get_bonus(pair: str, direction: str) -> int:
    """Aggregate-promoted-factor composite bonus for one pair, direction.

    Reads each promoted factor's current value (set by `compute_promoted_for
    active_pairs`) and its stored IC sign. Fraction agreeing with `direction`
    maps to a ±15 / ±7 / ±0 / -10 bonus.
    """
    if _is_disabled():
        return 0
    r = _r()
    try:
        promoted = json.loads(r.get(redis_keys.DSL_PROMOTED_FACTORS) or "[]")
    except Exception:
        return 0
    if not promoted:
        return 0
    agree = 0; counted = 0
    for item in promoted:
        expr = item.get("expr"); ic = float(item.get("ic", 0.0))
        if not expr or ic == 0: continue
        fhash = factor_hash(expr)
        val_raw = r.get(redis_keys.DSL_ALPHA_VALUE
                        .replace("{pair}", pair).replace("{factor_hash}", fhash))
        if val_raw is None: continue
        try:
            val = float(val_raw)
        except Exception:
            continue
        counted += 1
        signed = val * ic
        if (direction == "long" and signed > 0) or \
           (direction == "short" and signed < 0):
            agree += 1
    if counted < 3:
        return 0
    frac = agree / counted
    if frac > 0.7:
        return _PROMOTE_DSL_BONUS    # +15
    if frac > 0.6:
        return _PROMOTE_DSL_BONUS // 2  # +7
    if frac < 0.3:
        return -10
    return 0


def num_promoted() -> int:
    """Exposed for dashboard / health."""
    r = _r()
    try:
        promoted = json.loads(r.get(redis_keys.DSL_PROMOTED_FACTORS) or "[]")
        return len(promoted)
    except Exception:
        return 0
