# TP-as-Checkpoint — Lock-Profit Ratchet at TP1/TP2

Topic slug: `tp_as_checkpoint`
Created: 2026-05-30 (cont. 64 follow-up)
Trigger: Owner — "change tp1 and tp2 into the checkpoints that when profit reached this tp levels the profit is locked along with sl tailgating locked profit search and think how to implement this idea"

---

## A. Current behavior (Rule 2 — verified by reading `risk/manager.py:604-755`)

**TP2** (line 604): `_tp_confirmed(tp2,...)` → `_capture_final_peak(mark)` → `_guarded_close("candlenet_tp2")`. **Full close.**
**TP1** (line 682): `_tp_confirmed(tp1,...)` → `engine.close_partial(qty * close_frac)` where `close_frac = 0.33` (bull) or `0.50` (other). **Partial close, then trade continues on residual qty.**
**TP1 capital gate** (line 658-681): blocks TP1 firing until `peak_profit_pct >= cap_act_frac / leverage` (now 10% capital per cont. 64).
**TP2 has no capital gate** — fires the moment wickless debounce confirms a TP2 cross.

The new behavior the owner wants: **no closes at TP1/TP2 at all** — both TPs become *SL-ratchet checkpoints*. The trade runs on FULL size until either (a) trailing SL hits, (b) `mtf_15m_reversal_confirmed` fires (now gated to losing trades only), (c) the time-decay / dead-trade exit fires.

---

## B. Prior art (sources at bottom)

Textbook "free trade" pattern across forex/crypto bot literature:

- **TP1 hit → move SL to entry (breakeven)**. Worst case becomes a zero-PnL exit.
- **TP2 hit → move SL to TP1 (lock TP1 profit)**. Worst case becomes a TP1-profit exit.
- **Beyond TP2 → trailing SL only.** Pure peak-tracking from then on.

Variants:
- 50/50 partial-close at each TP — owner explicitly rejected.
- Triple-scale (TP1/TP2/TP3) — only TP1/TP2 in this codebase.
- "Lock at TP1" instead of breakeven — locks ALL of TP1 profit on TP1 hit, more aggressive. Some bots default to entry + 50% of TP1 distance (half-profit lock).

---

## C. Proposed design

### C.1 State machine per trade

```
state ∈ {pre_tp1, tp1_locked, tp2_locked}
```

- **pre_tp1** (default at open): normal trailing logic governs SL.
- **tp1_locked**: SL has been bumped to TP1-lock level. Trailing still updates IF its candidate is tighter (higher for long, lower for short). TP2 still active.
- **tp2_locked**: SL has been bumped to TP2-lock level. Trailing still updates IF tighter. No more TP-driven moves; trailing is the only upward force.

Persisted via two Redis flags (replacing the existing `tp1_fired`):
- `trade:{id}:tp1_locked`  ("1" once TP1 lock applied)
- `trade:{id}:tp2_locked`  ("1" once TP2 lock applied)

### C.2 Lock levels (DECISION POINT — see §F)

Three options for TP1 lock target:
- **L1-entry**: SL = entry price (breakeven). Most conservative.
- **L1-mid**: SL = entry + 0.5 × (TP1 − entry). Half-profit lock.
- **L1-tp1**: SL = TP1. Lock the full TP1 profit.

Three options for TP2 lock target:
- **L2-tp1**: SL = TP1. Lock the TP1 profit (canonical "TP2 → SL to TP1").
- **L2-mid**: SL = TP1 + 0.5 × (TP2 − TP1).
- **L2-tp2**: SL = TP2. Lock the full TP2 profit.

Default recommendation (matches search-result canonical pattern):
> **TP1 → L1-entry (breakeven); TP2 → L2-tp1 (lock TP1).**

### C.3 Integration with existing ratchet

`risk/manager.py:monitor_trailing_sl` already computes `ratchet_sl` from Path A (vol) + Path B ($-tier), then trail-update at line 1524+ takes the tighter of `ratchet_sl` vs the vol-based `new_sl`. **TP-checkpoint becomes a 4th candidate**, evaluated alongside:

```
candidates = [trail_vol_sl, path_a_ratchet_sl, path_b_tier_sl, tp_checkpoint_sl]
new_sl     = max(candidates) for long  (tighter is higher)
           = min(candidates) for short (tighter is lower)
```

Wickless debounce (the existing 30-second continuous-crossing guard at line 559) must still apply — a 1-tick wick to TP1 should not lock prematurely. Reuse `_tp_confirmed(tp1, direction, mark, "tp1")` exactly as today; just change the *consequence* of confirmation.

### C.4 Capital-activation gate handling

The TP1 capital gate at line 658-681 was added cont. 63 specifically to prevent TP1 from firing the instant mark touches the price level — under the old 15%-capital rule. Now that:
- TP1 no longer partially-closes (just bumps SL)
- The owner explicitly wants TP1/TP2 to act as checkpoints whenever crossed

…the capital gate becomes **redundant and harmful** for the new TP1 semantics. *Proposal*: drop the capital gate on TP1-checkpoint (let any wickless-confirmed cross lock the profit), keep wickless debounce as the only filter. The 10%-capital activation gate continues to guard the *trailing* Path A/B ratchet — independent layer, no conflict.

### C.5 Counters / observability

Replace existing `trail:tp1_capital_gate_blocked_count`, `candlenet_tp2_hit`, `candlenet_tp1_hit_partial` counters with checkpoint-semantics counters:
- `trail:tp1_checkpoint_locked_count`
- `trail:tp2_checkpoint_locked_count`
- `trail:tp1_lock_skipped_count` (locked-SL already tighter than the TP1 level — no-op)
- `trail:tp2_lock_skipped_count`

Keep `trail:mtf_15m_skip_winner_count` (from earlier cont. 64) — relevant to the broader winner-protection theme.

### C.6 Migration / safe rollout

- The change is additive in the SL direction (only ratchets UP, never widens). Worst case if buggy: trade gets stopped out at entry on a 1-tick wick during wickless debounce window — same downside as the old partial-close path.
- F46 governance gate optional — owner mandate suggests skipping (this is a behavior change owner wants live, not an experiment).
- No DB migration needed — `tp1_fired` / `tp2_fired` columns can keep their existing semantics (write `True` on lock, for backward-compatible audit).

---

## D. Confirmed vs Proposed

### Confirmed (already on disk — do not duplicate)
- `_tp_hit(level, dir, mark)` — instant cross check ✅
- `_tp_confirmed(level, dir, mark, label)` — wickless-debounce 30s gate ✅
- `engine.modify_sl(trade_id, new_sl, force=False)` with monotonic guard ✅
- `_capture_final_peak(mark)` peak-write helper ✅
- TP1 capital gate (cont. 63) — REMOVE on TP1 (no longer needed)

### Proposed (this slice)
- **TP1 hit** (was: partial close + tp1_fired flag) → set `tp1_locked` Redis flag + `engine.modify_sl(trade_id, tp1_lock_sl, force=False)`. No close, no qty change.
- **TP2 hit** (was: full close) → set `tp2_locked` Redis flag + `engine.modify_sl(trade_id, tp2_lock_sl, force=False)`. No close.
- **Trailing path** stays as-is — monotonic guard inside `modify_sl` ensures TP locks are not overwritten by a looser trailing candidate later.
- **Drop TP1 capital gate** on the TP1-checkpoint path (justified §C.4).
- **`tp1_fired` / `tp2_fired` DB columns**: keep writing `True` on lock for audit continuity; semantic meaning becomes "checkpoint reached" instead of "partial closed".

### Deferred (future)
- **Dynamic TP1/TP2 levels**: today they're derived from CandleNet magnitude predictions. The checkpoint pattern doesn't change the level computation — it only changes the consequence. Re-tuning the magnitudes is a separate next_impl.
- **TP3** as a third checkpoint — not in current code, not in this scope.
- **F47-style learner** for the lock-level choice (L1-entry vs L1-mid vs L1-tp1) — once we have ≥ 200 closed trades under the new logic, fold an online learner that picks per-regime.

---

## E. Implementation checklist (post user approval of §F)

- [ ] `risk/manager.py:604-637` — TP2 branch: replace `_guarded_close("candlenet_tp2")` with `engine.modify_sl(...)` to the chosen TP2 lock level + set `trade:{id}:tp2_locked` Redis flag + DB persist `tp2_fired=True` (keep existing audit semantics).
- [ ] `risk/manager.py:639-755` — TP1 branch: replace `engine.close_partial(...)` with `engine.modify_sl(...)` to the chosen TP1 lock level + set `trade:{id}:tp1_locked` Redis flag + DB persist `tp1_fired=True`.
- [ ] `risk/manager.py:658-681` — drop the TP1 capital gate (`_tp1_capital_gate_clear` block) entirely, OR rephrase to gate only on `vol_unit > 0` (sanity).
- [ ] `risk/manager.py:534` — replace `_tp1_already_fired` read of `tp1_fired` with `_tp1_already_locked` read of `tp1_locked`; add `_tp2_already_locked` read.
- [ ] Add gate at top of TP1 branch: `if _tp1_already_locked: skip`. Add same gate at TP2 branch: `if _tp2_already_locked: skip` (currently the only guard is the `_tp1_already_fired` arg passed to the TP2 log — that's just diagnostics, not a gate; TP2 was always allowed to re-fire which is irrelevant since it full-closes; under checkpoint semantics we need the explicit guard).
- [ ] New counters: `trail:tp1_checkpoint_locked_count`, `trail:tp2_checkpoint_locked_count`, `trail:tp1_lock_skipped_count`, `trail:tp2_lock_skipped_count`.
- [ ] PROGRESS.md cont. 64 follow-up entry.
- [ ] Memory update: `feedback_profit_lock.md` add a §"TP-checkpoint integration".
- [ ] Rebuild brain image, recreate brain container.

---

## F. Decision point awaiting owner

**TP1 lock target** (where does SL go when TP1 confirms):

| Option | SL becomes | Worst case after lock |
|---|---|---|
| **L1-entry** (Recommended) | entry price | breakeven exit (zero PnL) |
| L1-mid | entry + 0.5×(TP1 − entry) | half-TP1-profit exit |
| L1-tp1 | TP1 itself | full TP1-profit exit |

**TP2 lock target** (where does SL go when TP2 confirms):

| Option | SL becomes | Worst case after lock |
|---|---|---|
| **L2-tp1** (Recommended) | TP1 price | TP1-profit exit |
| L2-mid | TP1 + 0.5×(TP2 − TP1) | TP1 + half of TP1→TP2 range |
| L2-tp2 | TP2 itself | full TP2-profit exit |

Defaults match the canonical textbook pattern. Aggressive variants (L1-tp1, L2-tp2) lock more profit per checkpoint but stop out the trade sooner on a 2-tick reversal off the TP level — losing the post-TP trend continuation that the trailing SL would otherwise capture.

---

## G. References

- [3Commas — Stop Loss Breakeven feature (DCA bot)](https://help.3commas.io/en/articles/9464682-dca-bot-stop-loss-breakeven) — "TP1 → SL to breakeven" is the documented default pattern.
- [TradingView — Take Profit in Trading (CryptoVision)](https://www.tradingview.com/chart/ETHUSDT.P/MZAOYgJJ-Take-Profit-in-Trading-How-Profit-Levels-Work/) — multi-TP checkpoint discussion.
- [MQL5 Auto Trailing Stop By TP Percent with Profit Lock](https://www.mql5.com/en/market/product/44904) — MT4 EA implementing the checkpoint pattern.
- [Kanga — SL/TP/Trailing in futures](https://kanga.exchange/how-to-use-stop-loss-take-profit-and-trailing-stops-in-futures-trading) — combines trailing with TP levels (full close at TPs by default).
- [Forex Factory — Is breakeven a good move?](https://www.forexfactory.com/thread/1008871-is-breakeven-really-a-good-move) — discussion on whether TP1 → entry is too conservative; common counter-argument is L1-mid.

---
