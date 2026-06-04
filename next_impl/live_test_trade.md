# Next Impl — Live Test Trade (REAL MONEY)
_Created: 2026-05-25 cont. 47 | Status: ACTIVE_

## Goal
First-ever real-money trade on the user's Binance MAINNET account. Single trade, max 50 USDT capital, F48 models degraded (not yet trained — proceed without them). Bot picks pair + direction same as paper trading.

## User confirmations received
- [CONFIRMED] (A) Mainnet API keys are configured in `.env`
- [CONFIRMED] (B) Proceed with F48 degraded — OFI + regime + TFT + PatchTST + Direction Model only
- [CONFIRMED] (C) Bot picks pair + direction autonomously (same logic as paper)

## Sequence (in order — verify each step before next)

- [ ] **Pre-flight checks:**
  - [ ] Close any open paper trades (avoid live/paper contamination)
  - [ ] Confirm `bot:running = 0` (bot stopped)
  - [ ] Confirm `.env` has BINANCE_API_KEY + BINANCE_API_SECRET set (do not print values)
  - [ ] Verify current Binance balance via API call so we know real account has funds

- [ ] **Switch to live mode:**
  - [ ] Set `.env` `TRADING_MODE=live`
  - [ ] Set `.env` `BINANCE_TESTNET=false`
  - [ ] Restart brain + celery_worker containers (env propagation)
  - [ ] Verify brain logs `binance_client_ready testnet=False`
  - [ ] Verify execution engine is `LiveExecutionEngine`

- [ ] **Configure trade envelope:**
  - [ ] PUT `/bot/settings` with: min_open_trades=1, max_open_trades=1, max_position_usdt=50, starting_capital_usdt=50
  - [ ] Verify Redis: `bot:max_open_trades=1`, `bot:starting_capital_usdt=50`

- [ ] **Start new session + start bot:**
  - [ ] POST `/sessions/start` with reset_virtual_balance=true (clean session marker)
  - [ ] POST `/bot/start` → sets `bot:running=1`, initialises VIRTUAL_BALANCE=50

- [ ] **Trade execution:**
  - [ ] Watch brain logs for first signal → trade open
  - [ ] Verify trade hit Binance mainnet (order ID in logs)
  - [ ] Verify position exists on Binance via API

- [ ] **Report:**
  - [ ] Trade pair + direction + entry price + SL + (TP if set)
  - [ ] User can monitor via dashboard

## Safety notes
- F48 ML stack is degraded (no .pth files). Signal will use OFI + regime + TFT + PatchTST + direction model only.
- Default leverage is 5x — 50 USDT capital = up to 250 USDT notional exposure.
- Initial SL is VPIN-derived (currently ~5-15% adverse from entry). Worst-case loss on this trade is bounded by capital × leverage × (SL%).
- DCA is permanently disabled — bot won't add to loser.
- Profit-lock ratchet locks ≥80% of peak profit.
- The bot CANNOT be reverted to paper mid-trade — only after trade closes.

## Session Handoff
_Last updated: 2026-05-25 cont. 47_
**Done:** A/B/C confirmed by user. Topic file opened.
**Next step:** Pre-flight checks.
**Blockers:** Need to confirm mainnet keys are actually mainnet (only the user can confirm).
**Containers to rebuild:** None (env-only changes).
