# PnL Drift Fix — DB over-states PnL vs Binance (cont. 77)

**Status:** DEPLOYED 2026-06-11 02:49 UTC (brain restarted). Causes #1–#3 fixed + verified.
**Trigger:** `reconciler:drift_24h` ≈ +$6.5 (DB closed-trade net > Binance net). Binance is ground truth.

## Diagnosis (3 systematic, all optimistic — mis-recording, NOT lost money)
1. **Exit price from estimate, not fill.** On an already-flat/exchange-native-stop close,
   `execution/live.py` booked `exit_price = trailing_sl_level` (or mark fallback), not the real fill.
   Stops slip past trigger → booked PnL > realized.
2. **Fees one-sided (~half).** Entry fee lived in TTL'd Redis key `trade:{tid}:entry_fee_usdt`;
   when missing (and the empty already-flat close order carries 0 commission) only the exit side counted.
3. **Reconciler ghost double-count.** `risk/reconciler._income_by_type(pair, entry_ms)` summed ALL of a
   symbol's income since entry → re-traded symbols (CARV×3, COLLECT×4) attributed later trades' PnL.

## Fixes
- **#1+#2 — `execution/live.py` `close_trade`:** new `_income_since()` (line ~106) pulls actual
  realized/commission/funding from Binance income at close (4× retry for income lag). On already-flat
  close: `final_pnl=realized`, `fees=abs(commission)`, exit_price derived from realized. Fee fallback
  (line ~431) pulls total commission from income when the entry-fee stash is missing.
- **Defect fixed (Rule 18):** `raise _SkipPartials()` (undefined → NameError swallowed by bare except,
  accidental control flow) replaced with explicit `if not _income_used:` guard (line ~443).
- **#3 — `risk/reconciler.py`:** `_income_by_type` gained an `end_ms` upper bound; new `_next_open_ms`
  finds the next open on the same symbol (one-position-per-symbol → that's where this position was
  already flat). **`exclude_id=tid` is required** — the ghost's own fractional entry-ms is `> int(start_ms)`
  so it would self-match and collapse the window to $0 (caught by the disconfirming test, Rule 9).

## Verification (Rule 19)
- LOAD-CHECK: brain restarted 02:46 then 02:49 (newer than edits); modules import in-container (py3.11);
  `reconciler_started interval_s=120`; `_next_open_ms` signature has `exclude_id` (in-container `True`).
- **Cause #3 on REAL Binance income (COLLECTUSDT, 4 live trades):**
  - WITHOUT exclude → next_open = self (`…412.221` ≈ start) → window empty (the bug).
  - WITH exclude → next_open = real 2nd open `…312239` (~55 min later).
  - UNBOUNDED realized `5.5839` (all 4 trades) vs BOUNDED `1.7874` (this position) → **$3.80 double-count removed.**
- **VERIFICATION-PENDING (Cause #1+#2 end-to-end):** `bot:running=0` (trading paused), so no organic live
  close has fired since deploy. Helpers verified callable on real data; full proof = next live close.
  **Observe:** on next live close, log `live_close_booked_from_income` with realized/commission/funding;
  then `reconciler:drift_24h` should trend toward 0 as new closes book from income.

## Files
- `execution/live.py` (close_trade booking + _income_since + _SkipPartials guard)
- `risk/reconciler.py` (_income_by_type end_ms bound + _next_open_ms with exclude_id)
