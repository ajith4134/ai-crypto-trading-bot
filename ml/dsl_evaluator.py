"""
F54 — DSL evaluator (point-in-time, no lookahead).

Evaluates a parsed DSL AST against a per-pair time-series window.
The caller supplies OHLCV + microstructure arrays oldest→newest and
optionally an `anchor_t` index — the evaluator only sees data at or
before `anchor_t`, so backtests over different anchors are guaranteed
free of lookahead.

Returns either:
  - a single float (the factor value at anchor_t), or
  - a numpy array of factor values across multiple anchors when called
    via `evaluate_series` for IC / Sharpe backtests.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from ml.dsl_grammar import Const, Terminal, Op, Node, OPS, validate


class DSLEvalError(RuntimeError):
    pass


# Window functions on a 1-D array: return a single float computed over the
# trailing-N samples ending at index -1 (most recent). All functions return
# np.nan when window > len(arr) or when computation is ill-defined.

def _w_mean(a, n): return float(np.nanmean(a[-n:])) if len(a) >= n else np.nan
def _w_std(a, n):  return float(np.nanstd(a[-n:], ddof=1)) if len(a) >= n else np.nan
def _w_min(a, n):  return float(np.nanmin(a[-n:])) if len(a) >= n else np.nan
def _w_max(a, n):  return float(np.nanmax(a[-n:])) if len(a) >= n else np.nan

def _w_rank(a, n):
    if len(a) < n: return np.nan
    w = a[-n:]
    cur = w[-1]
    if not np.isfinite(cur): return np.nan
    return float(np.sum(w < cur) / n)

def _w_skew(a, n):
    if len(a) < n: return np.nan
    w = a[-n:]
    mu = np.nanmean(w); sd = np.nanstd(w, ddof=1)
    if sd == 0 or not np.isfinite(sd): return 0.0
    return float(np.nanmean(((w - mu) / sd) ** 3))

def _w_kurt(a, n):
    if len(a) < n: return np.nan
    w = a[-n:]
    mu = np.nanmean(w); sd = np.nanstd(w, ddof=1)
    if sd == 0 or not np.isfinite(sd): return 0.0
    return float(np.nanmean(((w - mu) / sd) ** 4) - 3.0)

def _w_zscore(a, n):
    if len(a) < n: return np.nan
    w = a[-n:]
    sd = np.nanstd(w, ddof=1)
    if sd == 0 or not np.isfinite(sd): return 0.0
    return float((w[-1] - np.nanmean(w)) / sd)

def _w_argmax(a, n):
    if len(a) < n: return np.nan
    w = a[-n:]
    return float(np.nanargmax(w) / max(n - 1, 1))   # normalised to [0,1]

def _w_argmin(a, n):
    if len(a) < n: return np.nan
    w = a[-n:]
    return float(np.nanargmin(w) / max(n - 1, 1))

def _w_decay(a, n):
    """Linear-decay weighted mean, weights summing to 1."""
    if len(a) < n: return np.nan
    w = a[-n:]
    weights = np.arange(1, n + 1, dtype=np.float64)
    weights /= weights.sum()
    return float(np.nansum(w * weights))

def _w_corr(a, b, n):
    if len(a) < n or len(b) < n: return np.nan
    x = a[-n:]; y = b[-n:]
    if np.nanstd(x) == 0 or np.nanstd(y) == 0: return 0.0
    try:
        return float(np.corrcoef(x, y)[0, 1])
    except Exception:
        return 0.0

def _w_cov(a, b, n):
    if len(a) < n or len(b) < n: return np.nan
    x = a[-n:]; y = b[-n:]
    return float(np.cov(x, y, ddof=1)[0, 1])


# Non-windowed unary ops on a scalar (the result of a sub-expression).
def _unary_log(x):  return float(np.log(abs(x) + 1e-12))
def _unary_abs(x):  return float(abs(x))
def _unary_sign(x): return float(np.sign(x))
def _unary_neg(x):  return float(-x)
def _unary_sqrt(x): return float(np.sqrt(abs(x)))


def _eval_node(node: Node, ctx: dict[str, np.ndarray]) -> Any:
    """Evaluate `node`.

    Behaviour:
      - Terminal → returns the numpy array (so windowed ops can read history).
      - Const → returns a float.
      - Op → returns a float (the windowed/unary/binary result).

    Scalar ops on arrays use the array's MOST-RECENT value. This matches
    the Qlib factor convention.
    """
    if isinstance(node, Const):
        return float(node.value)
    if isinstance(node, Terminal):
        arr = ctx.get(node.name)
        if arr is None:
            raise DSLEvalError(f"missing_terminal:{node.name}")
        return arr
    if not isinstance(node, Op):
        raise DSLEvalError(f"unknown_node:{type(node).__name__}")

    # Operator dispatch
    args = [_eval_node(a, ctx) for a in node.args]

    def _as_scalar(x):
        if isinstance(x, np.ndarray):
            return float(x[-1]) if len(x) else np.nan
        return float(x)

    def _as_series(x):
        if isinstance(x, np.ndarray): return x
        return np.array([float(x)], dtype=np.float64)

    def _binop_series(a, b, fn):
        """Apply fn elementwise when both ops are arrays — preserves
        series-ness so the result can feed into a downstream windowed op.
        Mixed scalar/array → broadcasts to the array's shape.
        Both scalar → returns a Python float (Op consumers handle that)."""
        if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
            n = min(a.shape[0], b.shape[0])
            return fn(a[-n:], b[-n:])
        if isinstance(a, np.ndarray):
            return fn(a, float(b))
        if isinstance(b, np.ndarray):
            return fn(float(a), b)
        return fn(float(a), float(b))

    op = node.name
    w = node.window

    if op == "add": return _binop_series(args[0], args[1], lambda x, y: x + y)
    if op == "sub": return _binop_series(args[0], args[1], lambda x, y: x - y)
    if op == "mul": return _binop_series(args[0], args[1], lambda x, y: x * y)
    if op == "div":
        # Protect against divide-by-zero by floor-ing the denom magnitude.
        def _safe_div(x, y):
            if isinstance(y, np.ndarray):
                y_safe = np.where(np.abs(y) < 1e-12, 1e-12, y)
                return x / y_safe
            return x / y if abs(y) > 1e-12 else 0.0
        return _binop_series(args[0], args[1], _safe_div)
    if op == "log":   return _unary_log(_as_scalar(args[0]))
    if op == "abs":   return _unary_abs(_as_scalar(args[0]))
    if op == "sign":  return _unary_sign(_as_scalar(args[0]))
    if op == "neg":
        # Preserve series-ness for unary neg too — common pattern is to negate
        # a microstructure terminal then feed into ts_corr.
        x = args[0]
        return -x if isinstance(x, np.ndarray) else _unary_neg(_as_scalar(x))
    if op == "sqrt":  return _unary_sqrt(_as_scalar(args[0]))

    if op in ("ts_mean", "ts_std", "ts_min", "ts_max", "ts_rank",
              "ts_skew", "ts_kurt", "ts_zscore",
              "ts_argmax", "ts_argmin", "ts_decay"):
        arr = _as_series(args[0])
        fn = {
            "ts_mean": _w_mean, "ts_std": _w_std, "ts_min": _w_min,
            "ts_max": _w_max,  "ts_rank": _w_rank, "ts_skew": _w_skew,
            "ts_kurt": _w_kurt, "ts_zscore": _w_zscore,
            "ts_argmax": _w_argmax, "ts_argmin": _w_argmin,
            "ts_decay": _w_decay,
        }[op]
        return fn(arr, w)
    if op == "ts_corr": return _w_corr(_as_series(args[0]), _as_series(args[1]), w)
    if op == "ts_cov":  return _w_cov(_as_series(args[0]), _as_series(args[1]), w)

    if op == "rank":
        # Cross-sectional rank not available in single-pair context;
        # fall back to ts_rank-60 of the same series.
        return _w_rank(_as_series(args[0]), 60)
    if op == "scale":
        v = _as_scalar(args[0])
        return v   # already O(1) scale; placeholder for cross-sectional scale
    if op == "clip":
        x = _as_scalar(args[0]); lo = _as_scalar(args[1]); hi = _as_scalar(args[2])
        return float(max(lo, min(hi, x)))
    if op == "where_gt":
        x = _as_scalar(args[0]); thr = _as_scalar(args[1]); then = _as_scalar(args[2])
        return float(then if x > thr else 0.0)
    if op == "where_lt":
        x = _as_scalar(args[0]); thr = _as_scalar(args[1]); then = _as_scalar(args[2])
        return float(then if x < thr else 0.0)
    raise DSLEvalError(f"unimplemented_op:{op}")


def evaluate(node: Node, ctx: dict[str, np.ndarray]) -> float | None:
    """Evaluate factor at the latest-sample anchor. None on error / non-finite."""
    ok, why = validate(node)
    if not ok:
        return None
    try:
        v = _eval_node(node, ctx)
        if isinstance(v, np.ndarray):
            v = float(v[-1]) if len(v) else float("nan")
        v = float(v)
        if not math.isfinite(v):
            return None
        return v
    except DSLEvalError:
        return None
    except Exception:
        return None


def evaluate_series(node: Node, ctx_full: dict[str, np.ndarray],
                    anchors: list[int]) -> np.ndarray:
    """Evaluate factor at multiple anchor indices over the same series.

    anchors are 0-indexed positions into the full ctx arrays. At each
    anchor we slice `ctx_full[k][:anchor+1]` so the evaluator sees only
    past data. Used by the miner's Sharpe / IC backtests.

    Returns an array len(anchors), with np.nan for failed evaluations.
    """
    out = np.full(len(anchors), np.nan, dtype=np.float64)
    for i, t in enumerate(anchors):
        if t < 0: continue
        sliced = {k: v[: t + 1] for k, v in ctx_full.items()}
        v = evaluate(node, sliced)
        if v is not None:
            out[i] = v
    return out


def required_history(node: Node) -> int:
    """Minimum number of OHLCV bars required to evaluate this factor."""
    from ml.dsl_grammar import max_window
    return max(max_window(node), 5) + 1
