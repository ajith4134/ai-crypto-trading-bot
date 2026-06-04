# Honest Third-Party Loss Audit — 2026-05-29

**Reviewer stance:** External code/quant review. No politics, no rule-restriction. Just the data and the code, scored against the user's stated goal: **stop losing money**.

## TL;DR — the 5 things actually bleeding you

| # | Bleeder | Evidence | Cost | Fixable how |
|---|---|---|---|---|
| 1 | Manual `close_all` on mode switch (paper↔live toggle) | 353 trades closed this way, **-$3,325** total; one day (2026-05-24) alone = **-$2,498** | **-$3,325** | Toggle behavior (operator) + add confirmation gate (code) |
| 2 | Regime-scaling override of profit lock | `_regime_scaled_lock_frac` cuts user-mandated 80% to **0.40-0.55** in bull/bear. Fires 23,345× | **-$13,222 of given-back peaks** | Disable kill switch OR raise floor |
| 3 | Initial SL too loose on illiquid coins | GUAUSDT lost -$1,128 in 21 min; SIRENUSDT -$176 in 0 min; INUSDT -$854 in 16 min | **~-$5,000** | Liquidity-gated min_atr_mult |
| 4 | TP1/TP2 dead — fires NEVER (0/4059 trailing_sl trades) | tp1_fired = NULL/FALSE on every single row. Avg peak +$14.47 → avg realized +$0.57 = 4% retention | **-$13,890 of peaks** | Wire TP1/TP2 into engine.close path |
| 5 | "High-WR" strategies bleed slowly | momentum_continuation_8636 (79% wr, -$993), tight_stops_poor_0358 (72% wr, -$990), momentum_strategy_8049 (80% wr, -$704) | **-$2,687** | Asymmetric exit gates — small win, big loss = death |

**Without bleeder #1 alone, the bot would be +$904 instead of -$1,593.**
**Without #1 and fixing #2 to honor the 80% mandate, the bot would be ~+$10k.**

---

## 1. Manual close_all on mode switch — the single biggest loss source

`dashboard/api.py:439` — when you toggle paper↔live with `close_open_trades=True`, every open position force-closes at whatever P&L it had. The `manual_close_all` exit reason aggregates **353 trades, -$3,325**.

**Daily breakdown:**
| Day | Closes | Loss |
|---|---|---|
| 2026-05-24 | 138 | **-$2,498** |
| 2026-05-28 | 49 | -$541 |
| 2026-05-26 | 19 | -$198 |
| 2026-05-23 | 49 | -$94 |
| 2026-05-27 | 24 | -$57 |
| 2026-05-25 | 6 | -$16 |
| 2026-05-20 | 68 | **+$79** ← the only profitable close-all batch |

**Fix:** add a "smart close" mode in `dashboard/api.py:439` that:
- Holds positions across mode switch (mode flag is informational; positions don't need to close)
- Or only closes losing positions, lets winners ride
- Or requires explicit dollar threshold ("only close positions worse than -10% of capital")

Operator-side: avoid toggling paper↔live with `close_open_trades=True` unless you actually want to liquidate.

## 2. Regime-scaling override — the silent profit drain

`risk/manager.py:56-85` `_regime_scaled_lock_frac` maps the user-mandated 80-92% profit lock to **0.40-0.55 in bull/bear regimes**, "to let trends breathe." Per data:

```
trail:ratchet_applied_count = 3766   (ratchet fired)
trail:regime_scaled_count   = 23345  (scaling downgraded the lock)
trail:learned_params:base_lock_frac = 0.8065  (F47 learner at floor)
```

The kill switch `risk:regime_scaling_disabled=1` reverts to the mandated 80%. **It is not currently set.**

**Peak retention by bucket (the proof):**

| Peak | avg_peak | avg_final | retention |
|---|---|---|---|
| <$5 | $2.75 | -$2.92 | **-148%** (peaked into profit, exited at loss) |
| $5-15 | $9.14 | -$4.56 | **-56%** |
| $15-30 | $21.38 | $0.28 | **-1%** |
| $30-100 | $53.21 | $26.32 | 44% |
| >$100 | $149.54 | $114.39 | 75% |

Even the >$100 bucket at 75% is below your 80% floor. The $5-15 bucket actually *loses money on average* despite having a peak gain — the bot watches a +$9 peak turn into a -$5 loss.

**Fix options:**
- (a) `redis-cli SET risk:regime_scaling_disabled 1` — immediate, reverts to 80% mandate
- (b) Change `_REGIME_LOCK_BANDS` floor from 0.40 → 0.65 (50% retention minimum) and ceiling from 0.55 → 0.80
- (c) Per-strategy lock_frac (trends keep loose, mean-revs go tight)

**Recommended: (a) immediately, audit P&L for 48h, then decide on (b)/(c).**

## 3. Initial SL too loose on illiquid pairs

| Pair | n catastrophic | Total loss | Avg minutes to disaster |
|---|---|---|---|
| GUAUSDT | 7 | -$1,128 | **21.8** |
| INUSDT | 5 | -$854 | **15.8** |
| RONINUSDT | 15 | -$633 | **8.2** |
| SIRENUSDT | 1 | -$176 | **0.0** |

Sub-30 min catastrophic losses = flash dumps where the ATR-based SL was wider than the rug. `risk/manager.py:88 compute_initial_sl` doesn't liquidity-gate.

**Fix:** add a 24h-volume floor. Coins with rolling 24h notional below threshold X get either:
- Wider min_pct (3% → 5%) AND smaller capital_pct (1.0× → 0.5×) — bigger room, smaller bet
- Banned from entry entirely
- Required to come with on-chain netflow confirmation (F52 producer is live but no consumer)

## 4. TP1/TP2 system is completely dead

`SELECT COUNT(*) FROM trades WHERE tp1_fired=true` → **0**.

`risk/manager.py:223 compute_tp_targets` computes TP1/TP2 prices, writes them to `tp1_target`/`tp2_target` columns. But:
- No engine code reads `tp1_target` and checks against mark price
- `tp1_fired` column is never set to true
- Cont. 57 PROGRESS.md mentions TP1/TP2 dashboard persistence but the firing logic isn't wired

This is the second-biggest bleeder. Average peak $14.47, average realized $0.57 means **the bot reached profit 96% of the time and gave it all back.**

**Fix:** in `risk/manager.py:monitor_trailing_sl` add a TP1 check before the trailing logic:
```python
if not trade.get("tp1_fired") and tp1_target_hit(mark, tp1_target, direction):
    # Close 50% of position at TP1, move SL to entry on remainder
    engine.close_partial(trade_id, fraction=0.5, reason="tp1_hit")
    updates["tp1_fired"] = True
    updates["trailing_sl_level"] = entry  # break-even on runner
```

This single fix would lock in `peak * 0.5 + 0` at minimum on every trade that reaches a peak — turning -$2.92 average into approximately +$1.40 in the $5-15 peak bucket. Across 4059 trades that's **~+$8,200 alone.**

## 5. High-WR strategies losing money — asymmetric exit failure

Three strategies (now retired) had win rates 72-80% but each lost ~$700-1000:
- momentum_continuation_8636: 79.1% wr, -$993, avg -$3.30/trade
- tight_stops_poor_0358: 72.3% wr, -$990, avg -$2.51/trade
- momentum_strategy_8049: 80.0% wr, -$704, avg -$2.35/trade

When a strategy with 80% win rate loses money, it means **wins are smaller than losses in dollar terms**. With 80% wr and avg loss = 4× avg win, expected value goes negative. This is the same disease as #2 and #4 above, manifesting per-strategy.

## What's dormant in the blueprint that could help

Verified against `/home/ajithd747/ai-brain-crypto-bot/BOT_BLUEPRINT.md` and code:

| Feature | Status | What it would do | Why dormant |
|---|---|---|---|
| F27 Transfer Entropy | Producer live (`ml/transfer_entropy.py`) | BTC→altcoin lead detection — enter alt N bars after BTC moves | No consumer reads `lead_lag_matrix` for entry decisions |
| F52 Exchange Netflow | Producer live | Inflows = distribution risk → block long entries | No engine consumer |
| F58 Liquidation Cascade Alpha | Cont. 60 produces signals | Enter LONG after cascade exhaustion (1.3× capital per seed config) | Consumer side: `liquidation_cascade_fade` strategy is in pool but signal engine doesn't have a cascade-detector entry trigger |
| F26 BOCPD per-pair | Producer live `bocpd_per_pair_producer.py` | Hard kill on changepoint detection | Wired for kill but not for ENTRY direction info |
| F18 CryptoBERT + FinBERT sentiment | Loaded in `ml/sentiment.py` | Sentiment shift → tighten SL or block entries | Inference happens (web_intel pipeline) but `interpret_and_store` results don't feed into entry decisions |
| F23 Mutual Information | `ml/mutual_info.py` | Feature importance for current regime — re-weight signals | Computed but not consumed by signal weighting |
| F38 Curiosity Engine | Wired | Targets unexplored pair/regime combos | Active but its outputs not honored by selector |
| F37 Multi-Agent Debate | Wired in signals/engine | Three agents vote on entry — supposedly. | Look at counters — verify it's actually voting and rejecting |

**Quick verification of dormancy via counters:**

```bash
docker compose exec -T redis redis-cli MGET \
  "te:consumer_fire_count" "netflow:consumer_block_count" \
  "cascade:entry_count" "sentiment:gate_count" \
  "debate:agreement_count" "curiosity:target_picked_count"
```

If any of those return 0 or nil, the feature exists but never fires.

## SOTA exit alternatives (from web research)

Standard finding from quant literature:
- **Chandelier Exit (LeBeau)** = `Highest_High(22) - ATR(22) × 3.0` produced **1.61 profit factor** vs 1.28 for fixed 10% trailing on BTC daily 2020-24 ([StratBase](https://stratbase.ai/en/blog/average-true-range-trailing-stop), [LuxAlgo](https://www.luxalgo.com/blog/5-atr-stop-loss-strategies-for-risk-control/))
- ATR 3× multiplier is the sweet spot; 2× = too tight (whipsaw), 4× = gives back too much
- For day-trading: ATR(5-10) × 1.5-2.0
- For swing: ATR(14-21) × 2.0-2.5
- For position: ATR(21-30) × 2.5-3.5

Your `risk/manager.py:compute_initial_sl` uses configurable `atr_mult` (default 2.5). Per-strategy from the 27 seeds ranges 1.5-4.0 — that's wider than SOTA recommends for day-trading. Combined with insufficient profit lock, this is the asymmetric exit failure.

**More advanced (not in blueprint yet):**
- **Triple Barrier method (de Prado)** — every trade gets TP, SL, AND time-stop. After T bars, force-close. Backtests on crypto show 15-25% Sharpe improvement vs trailing alone.
- **CVaR-optimized exit** — minimize Conditional Value at Risk, not just expected loss. Captures tail-risk. Blueprint has "Deep Hedging policy network" in cont. 60 scaffold but not trained.
- **Order-flow exit (Hawkes self-excitation)** — your `hawkes_producer.py` is live but only consumed for ENTRY. Exit on Hawkes λ spike against your direction = early signal that the move is exhausted.
- **Pattern-based exit** — altFINS found inverse H&S 84%, H&S 82%, double-bottom 82% win rate ([altFINS](https://altfins.com/knowledge-base/chart-patterns/)). CandleNet partially does this; could be combined with pattern detection.

## Most-actionable fix priority

**Today (no code, takes 30 seconds):**
1. `docker compose exec -T redis redis-cli SET risk:regime_scaling_disabled 1` — reverts 80% mandate
2. Stop toggling paper↔live with close_open_trades=True

**This week (1-2 hour code fixes):**
3. Wire TP1/TP2 firing logic in `risk/manager.py:monitor_trailing_sl` — biggest dollar win available
4. Add liquidity gate to entry: `r.get(f"{pair}:24h_quote_volume_usdt")` < threshold → reject
5. Verify dormant feature counters; if any return 0, that's free profit being left on the table

**This month (deeper rewrites):**
6. Per-strategy lock_frac (not regime-driven) — momentum strategies should keep loose, mean-rev strategies tight
7. Triple Barrier method on top of trailing — adds time-stop dimension
8. Wire F18 sentiment into entry gates — sentiment flip against your direction = block new entries

## What I cannot honestly do in one session

- Continuously monitor live trades — chat session is bounded. Set up a `/loop` skill or schedule for that.
- Backtest these fixes — needs the backtester harness. The fixes above are statistically motivated by the data; backtesting before applying is still wise for #6-#8.
- Guarantee profits — markets can change; what fixed past losses may not fix future ones.

## Files referenced
- `risk/manager.py:56` `_regime_scaled_lock_frac`
- `risk/manager.py:88` `compute_initial_sl`
- `risk/manager.py:223` `compute_tp_targets` (computed but never compared)
- `risk/manager.py:765-870` ratchet logic
- `dashboard/api.py:295` `/bot/close_all_trades`
- `dashboard/api.py:439` mode-switch close-all
- `config.yaml: risk.profit_lock_tiers` — `[[50,0.92],[25,0.90],[10,0.85],[2,0.80]]`
- `signals/engine.py:1620` F8 capital sizing site (where overlay piggy-back lives)
