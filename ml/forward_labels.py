"""
Standardized forward-return labels — P2 of next_impl/kline_corpus_model_reframe.md (cont. 69g).

ONE definition of "what happens next" reused by every kline-trained model (candlenet,
predict-all direction/move heads, forecasters) so their predictions are comparable and
conformal calibration is consistent. Pure numpy; reads the shared corpus CSVs from P1.

Two label families:
  * Fixed-horizon  : direction (up/down/flat-deadband) + signed magnitude over H bars.
  * Triple-barrier : first-touch of a +tp% / -sl% / time barrier → win/loss + bars held +
                     realised RR. This is the execution-style label for confidence/RR heads.

All functions are leak-safe: a label at index i only uses bars STRICTLY AFTER i.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

DATA_DIR = Path("data/historical")


# ─────────────────────────────────────────────────────────────────────────────
# Corpus loading (reuses the P1 store format: timestamp,open,high,low,close,volume)
# ─────────────────────────────────────────────────────────────────────────────
def load_ohlcv(pair: str, interval: str) -> dict | None:
    """Return {ts,open,high,low,close,volume} as float arrays (ascending time),
    or None if the corpus file is missing/empty."""
    p = DATA_DIR / pair / f"{interval}.csv"
    if not p.exists():
        return None
    try:
        a = np.genfromtxt(p, delimiter=",", skip_header=1, dtype=np.float64)
    except Exception:
        return None
    if a.ndim != 2 or a.shape[0] < 2 or a.shape[1] < 6:
        return None
    a = a[np.argsort(a[:, 0])]                 # ensure ascending by timestamp
    return {"ts": a[:, 0], "open": a[:, 1], "high": a[:, 2],
            "low": a[:, 3], "close": a[:, 4], "volume": a[:, 5]}


# ─────────────────────────────────────────────────────────────────────────────
# Fixed-horizon labels
# ─────────────────────────────────────────────────────────────────────────────
def forward_return(close: np.ndarray, horizon: int) -> np.ndarray:
    """Signed pct return over the next `horizon` bars, aligned to entry index i:
    r[i] = close[i+H]/close[i] - 1. Last H entries are NaN (no future)."""
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float64)
    if horizon < 1 or n <= horizon:
        return out
    fut = close[horizon:]
    cur = close[:-horizon]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[:-horizon] = np.where(cur > 0, fut / cur - 1.0, np.nan)
    return out


def direction_label(close: np.ndarray, horizon: int,
                    deadband: float = 0.0) -> np.ndarray:
    """1 = up, 0 = down, NaN = within ±deadband (flat) or no-future. `deadband`
    is a fractional move (e.g. 0.001 = 0.1%) so near-zero moves don't teach noise."""
    r = forward_return(close, horizon)
    lab = np.full_like(r, np.nan)
    up = r > deadband
    dn = r < -deadband
    lab[up] = 1.0
    lab[dn] = 0.0
    return lab


def magnitude_label(close: np.ndarray, horizon: int) -> np.ndarray:
    """Absolute forward move in PCT (×100). NaN where no future."""
    return forward_return(close, horizon) * 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Triple-barrier (first-touch) — execution-style win/loss + RR labels
# ─────────────────────────────────────────────────────────────────────────────
def triple_barrier(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                   tp_pct: float, sl_pct: float, max_bars: int,
                   direction: str = "long") -> dict:
    """For every index i, simulate a `direction` trade entered at close[i] with a
    +tp_pct / -sl_pct barrier and a `max_bars` time stop. First touched barrier
    wins (TP checked before SL within the same bar = optimistic; flip for
    conservatism). Returns aligned arrays:
        win  : 1.0 TP-first, 0.0 SL-first, NaN time-stop/no-future
        rr   : realised reward:risk (+tp/sl on win, -1 on loss, time-stop = pnl/sl)
        bars : bars held until resolution
    Leak-safe: only bars i+1..i+max_bars are inspected.
    """
    n = len(close)
    win = np.full(n, np.nan)
    rr = np.full(n, np.nan)
    bars = np.full(n, np.nan)
    sign = 1.0 if direction == "long" else -1.0
    for i in range(n - 1):
        entry = close[i]
        if entry <= 0:
            continue
        tp = entry * (1 + sign * tp_pct)
        sl = entry * (1 - sign * sl_pct)
        end = min(i + max_bars, n - 1)
        hit = None
        for j in range(i + 1, end + 1):
            hi, lo = high[j], low[j]
            if direction == "long":
                if hi >= tp:
                    hit = ("tp", j); break
                if lo <= sl:
                    hit = ("sl", j); break
            else:
                if lo <= tp:
                    hit = ("tp", j); break
                if hi >= sl:
                    hit = ("sl", j); break
        if hit is None:
            # time stop: mark-to-close pnl at the horizon end
            if end > i:
                pnl = sign * (close[end] / entry - 1.0)
                rr[i] = pnl / sl_pct if sl_pct > 0 else 0.0
                win[i] = 1.0 if pnl > 0 else 0.0
                bars[i] = end - i
            continue
        kind, j = hit
        bars[i] = j - i
        if kind == "tp":
            win[i] = 1.0
            rr[i] = tp_pct / sl_pct if sl_pct > 0 else 1.0
        else:
            win[i] = 0.0
            rr[i] = -1.0
    return {"win": win, "rr": rr, "bars": bars}


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: build a labelled training frame for one pair/interval
# ─────────────────────────────────────────────────────────────────────────────
def build_labels(pair: str, interval: str, horizon: int,
                 deadband: float = 0.0,
                 triple: dict | None = None) -> dict | None:
    """Load the corpus + attach standardized labels. `triple` (optional) =
    {tp_pct, sl_pct, max_bars, direction} adds first-touch win/rr/bars.
    Returns a dict of aligned arrays (NaN where no future) or None."""
    d = load_ohlcv(pair, interval)
    if d is None:
        return None
    out = dict(d)
    out["fwd_return"] = forward_return(d["close"], horizon)
    out["direction"] = direction_label(d["close"], horizon, deadband)
    out["magnitude"] = magnitude_label(d["close"], horizon)
    out["horizon"] = horizon
    if triple:
        tb = triple_barrier(d["high"], d["low"], d["close"],
                            tp_pct=triple.get("tp_pct", 0.01),
                            sl_pct=triple.get("sl_pct", 0.005),
                            max_bars=triple.get("max_bars", horizon),
                            direction=triple.get("direction", "long"))
        out.update({f"tb_{k}": v for k, v in tb.items()})
    return out
