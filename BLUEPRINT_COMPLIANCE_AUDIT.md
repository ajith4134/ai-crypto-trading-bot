# Blueprint Compliance Audit — 2026-05-17

**Audit method:** Each feature in `BOT_BLUEPRINT.md` (44 features total) was compared against the actual code on disk by reading every source file in `/opt/trading-bot/`. No assumptions — only direct file reads.

**Severity:**
- **P0** — Feature broken or producing fake output that looks real
- **P1** — Feature implemented but never called from the live system
- **P2** — Feature simplified vs blueprint requirements but still functional

---

## 🔴 P0 — CRITICAL: PRE-TRAINED MODELS UNLOADABLE

### Issue #1 — `torch.save(model)` instead of `torch.save(state_dict)` breaks every deep ML feature

**Affected blueprint features:** F19 (TFT), F20 (PatchTST), F24 (GNN), F34 (World Model)

**File:** `pretrainer/main.py` lines 169, 176, 204, 211
**File:** `ml/tft.py`, `ml/patchtst.py`, `ml/gnn.py`, `world_model/model.py`

**Root cause — exact mechanic:**

The pretrainer at `pretrainer/main.py:24-92` defines four model classes (`TFTModel`, `PatchTSTModel`, `GNNModel`, `WorldModelBundle`) at module level. When the pretrainer runs as `python pretrainer/main.py`, Python sets `__name__ = "__main__"`. Then at lines 169/176/204/211:

```python
torch.save(model, MODELS_DIR / "tft.pth")          # saves WHOLE model + class ref
torch.save(model, MODELS_DIR / "patchtst.pth")     # WRONG — pickles class path
torch.save(model, MODELS_DIR / "gnn.pth")          # WRONG — pickles class path
torch.save(model, MODELS_DIR / "world_model.pth")  # WRONG — pickles class path
```

Because `torch.save(model)` pickles the entire object graph including the class reference, the saved file embeds the qualified path `__main__.TFTModel`, `__main__.GNNModel`, `__main__.WorldModelBundle`.

At inference time the loaders (`ml/tft.py:17`, `ml/gnn.py:17`, `ml/patchtst.py:17`, `world_model/model.py:20`) do:

```python
_model = torch.load(_MODEL_PATH, map_location="cpu", weights_only=False)
```

But inference runs from `python main.py` (brain) or `celery -A celery_app` (worker), so `__main__` is now `brain.main` or `celery.bin.celery` — `__main__.TFTModel` does not exist there. Pickle raises `Can't get attribute 'TFTModel' on <module '__main__'>`.

**Current behavior observed in logs:**
- `tft_forecast_failed error="Weights only load failed... Unsupported global: GLOBAL __main__.TFTModel"`
- `world_model_load_failed_using_fallback`
- `gnn_signals_failed`

**Consequence — fallback heuristics are not real predictions:**
- TFT/PatchTST: `get_price_forecast()` returns `{}` → `signals/engine.py:14` `tft_bias` stays 0 → no forecast bias
- GNN: `get_interasset_signals()` returns `{}` → inter-asset correlations are unused
- World Model: `encode()` falls back to MD5-hash-of-observation-dict (not a real latent space). `predict_reward()` returns hardcoded `{open_long:0.1, open_short:0.1, hold:0.0, close:-0.05}` — every action gets the same bias regardless of market. `imagine_trajectory()` returns deterministic same-value rewards because the fallback chain produces stable outputs.
- Self-Play F41 (`self_play/mars.py:84` `mcts_plan`) calls `imagine_trajectory` which uses the hash fallback. Result: every action gets reward 0.1, every self-play episode produces $0.00 PnL.

**How to fix:**

**Step 1:** Create a new shared module `models/architectures.py` containing all four model classes:

```python
# /opt/trading-bot/models/architectures.py
import torch
import torch.nn as nn

class TFTModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.lstm = nn.LSTM(1, 64, 2, batch_first=True)
        self.head = nn.Linear(64, 3)
    def forward(self, x):
        out, _ = self.lstm(x)
        return self.head(out[:, -1, :])

class PatchTSTModel(nn.Module):
    def __init__(self, patch_size=16, seq_len=256):
        super().__init__()
        self.patch_size = patch_size
        self.embed = nn.Linear(patch_size, 64)
        self.encoder = nn.TransformerEncoderLayer(64, 4, batch_first=True)
        self.head = nn.Linear(64, 1)
    def forward(self, x):
        B, T, _ = x.shape
        patches = x.reshape(B, T // self.patch_size, self.patch_size)
        emb = self.embed(patches)
        enc = self.encoder(emb)
        return self.head(enc).squeeze(-1)

class GNNModel(nn.Module):
    def __init__(self, in_features=2, out_features=16):
        super().__init__()
        self.fc = nn.Linear(in_features, out_features)
    def forward(self, x):
        return torch.relu(self.fc(x))

class WorldModelBundle(nn.Module):
    def __init__(self, obs_dim=64, latent_dim=32, action_dim=4):
        super().__init__()
        self.encoder = nn.Sequential(nn.Linear(obs_dim, 128), nn.ReLU(), nn.Linear(128, latent_dim))
        self.transition = nn.Sequential(nn.Linear(latent_dim + action_dim, 64), nn.ReLU(), nn.Linear(64, latent_dim))
        self.reward = nn.Sequential(nn.Linear(latent_dim, 32), nn.ReLU(), nn.Linear(32, 1))
    def forward(self, x):
        return self.encoder(x)
```

**Step 2:** Update `pretrainer/main.py`:
- Delete lines 24-92 (the class definitions)
- Add at top: `from models.architectures import TFTModel, PatchTSTModel, GNNModel, WorldModelBundle`
- Change lines 169/176/204/211 from `torch.save(model, ...)` to `torch.save(model.state_dict(), ...)`

**Step 3:** Update each inference module's `_load()`:
- `ml/tft.py:13-19` — import `TFTModel` from `models.architectures`, instantiate, load state_dict
- `ml/patchtst.py:13-19` — same with `PatchTSTModel`
- `ml/gnn.py:13-19` — same with `GNNModel`
- `world_model/model.py:16-27` — same with `WorldModelBundle`, then attach `.encoder`, `.transition`, `.reward` as separate sub-modules

**Step 4:** Re-run pretrainer to regenerate `.pth` files with state_dict format:
```
docker compose --profile pretrainer up pretrainer
```

**Step 5:** Rebuild brain + celery_worker + data_feed containers.

**Verification:**
- Brain startup logs should show `world_model_loaded` instead of `world_model_load_failed_using_fallback`
- `redis-cli get "{pair}:tft_forecast_1h"` should return JSON with non-zero quantiles
- Self-play episode log should show non-zero `total_pnl` (not always 0.0)

---

## 🔴 P0 — CRITICAL: BLUEPRINT MODEL ARCHITECTURES NOT IMPLEMENTED

### Issue #2 — TFT/PatchTST/GNN/World Model are toy implementations, not the cited research papers

**File:** `pretrainer/main.py:26-92`

**Blueprint vs reality:**

| Feature | Blueprint Citation | Actual Implementation |
|---------|-------------------|----------------------|
| F19 TFT | Lim et al. (Google, 2021) — Variable Selection Networks + Gated Residual Networks + Multi-head attention + Quantile loss | `nn.LSTM(1, 64, 2) + nn.Linear(64, 3)` (50-line stub) |
| F20 PatchTST | Nie et al. (ICLR 2023) — Channel independence, patch reverse instance normalization | `nn.Linear(16, 64) + 1 TransformerEncoderLayer + Linear(64,1)` |
| F24 GNN | Graph attention or message passing with edge weights | `nn.Linear(2, 16) + ReLU` — not even a graph! |
| F34 World Model | DreamerV3 (Nature 2025) — Recurrent State-Space Model + symlog reward + KL balancing | `Sequential(Linear, ReLU, Linear)` for each of encoder/transition/reward |

**Consequence:** Even if model loading is fixed (Issue #1), the predictions are from minimal toy networks that don't have the predictive capacity the blueprint describes. The blueprint claims:
- TFT: "+19% improvement in price prediction accuracy"
- PatchTST: "achieves SOTA on long-sequence forecasting"
- World Model: "DreamerV3-style imagined planning"

A 2-layer LSTM cannot deliver any of this.

**How to fix:**

This is a deeper engineering effort. Options:

**Option A (recommended):** Use proper library implementations:
- TFT: `pytorch-forecasting.TemporalFusionTransformer` (already in `requirements.txt`)
- PatchTST: HuggingFace `transformers.PatchTSTModel`
- GNN: `torch-geometric.nn.GATConv` (already in `requirements.txt`)
- World Model: Use existing DreamerV3 implementation, e.g. https://github.com/danijar/dreamerv3-torch

**Option B (acceptable for Stage 1):** Acknowledge in PROGRESS.md that these are placeholder implementations. Replace each one progressively as the bot matures.

**Recommended timeline:**
1. Fix Issue #1 first (load mechanic) — gets the current toy networks producing outputs
2. Replace TFT and PatchTST with `pytorch-forecasting` versions at Phase 2 (100 trades)
3. Replace GNN with `torch-geometric.nn.GATConv` at Phase 3 (300 trades)
4. Replace World Model with `dreamerv3-torch` at Phase 3

---

## 🔴 P0 — CRITICAL: FEATURE GOVERNANCE NEVER ENFORCED

### Issue #3 — Registry exists, no module calls `register()`, no enforcement loop

**File:** `feature_governance/registry.py` (139 lines)
**Blueprint:** Section 15.13, Feature 30 — "Autonomous Feature Governance System"

**What's built:**
- `register()`, `update_contribution()`, `check_degradation()`, `deactivate_feature()`, `reactivate_on_probation()`, `is_active()` — all defined

**What's missing:**

1. **No module calls `register()` at module load.** Run `grep -rn "from feature_governance.registry import register" /opt/trading-bot/` — zero hits.
   - Result: `_REGISTRY` dict is always empty. `assert_registered()` would raise for every feature_id.

2. **`update_contribution()` is never called after trade close.** Should be called from `memory/write.py` `write_trade_close()` with the list of features that contributed to that trade.

3. **`check_degradation()` is never polled.** Should run as a periodic job every N trades.

4. **The 5 failure modes from blueprint are not all implemented:**
   - ✅ Degradation: `check_degradation()` exists
   - ❌ Contradiction: not implemented
   - ❌ Blocking: not implemented
   - ❌ Bad Trade Causation: not implemented
   - ❌ Redundancy/Noise: not implemented

5. **The 7-step decode-before-act process** described in blueprint Section 15.13.4 is missing entirely.

6. **No feature is actually checked via `is_active()`** before being used. Even if a feature were deactivated, the code calling it (e.g., `signals/engine.py` calling `ml.tft.get_price_forecast`) would still call it.

**How to fix:**

**Step 1:** Add a `register()` call at the top of every feature module. Create a single bootstrap function `feature_governance/bootstrap.py`:

```python
# feature_governance/bootstrap.py
from feature_governance.registry import register

def bootstrap_all_features():
    """Called once at brain startup to register all 44 features."""
    register("F14_HMM", "HMM Regime Detection", activation_phase=0)
    register("F15_OFI", "OFI + VPIN + Kyle + Amihud", activation_phase=0)
    register("F16_Kelly", "Fractional Kelly", activation_phase=1)
    register("F17_EWC", "Continual Learning", activation_phase=3)
    register("F18_Sentiment", "CryptoBERT + FinBERT", activation_phase=0)
    register("F19_TFT", "Temporal Fusion Transformer", activation_phase=0)
    register("F20_PatchTST", "PatchTST Long-Sequence", activation_phase=0)
    # ... all 44 features
```

Call this from `main.py:_startup_checks()` after redis init.

**Step 2:** Wire `update_contribution()` into `memory/write.py` `write_trade_close()`:

After the existing OPRO block (around line 132), add:
```python
# Feature Governance — update contribution scores for features that fired
try:
    from feature_governance.registry import update_contribution
    won = float(exit_data.get("net_pnl_usdt", 0)) > 0
    pair = ...   # need to query trade row
    regime = ... # need to query trade row
    for feature_id in ["F14_HMM", "F15_OFI", "F18_Sentiment", "F19_TFT", ...]:
        update_contribution(feature_id, won, pair, regime)
except Exception as exc:
    log.warning("governance_update_skipped", error=str(exc))
```

**Step 3:** Add a periodic check task to celery_app.py:
```python
@app.task(queue="default")
def feature_governance_check():
    """Every 50 trades: check each feature for degradation, deactivate if needed."""
    from feature_governance.registry import _REGISTRY, check_degradation, deactivate_feature
    for feature_id in _REGISTRY:
        if check_degradation(feature_id):
            deactivate_feature(feature_id, "degradation", f"10 consecutive negative trades")
```

Schedule it from `write_trade_close()` every 50 trades.

**Step 4:** Wire `is_active()` checks into the call sites. E.g., in `signals/engine.py`:
```python
from feature_governance.registry import is_active
if is_active("F19_TFT"):
    tft_bias = ...  # call TFT
else:
    tft_bias = 0
```

**Step 5:** Implement the 4 missing failure modes (Contradiction, Blocking, Bad Trade Causation, Redundancy/Noise) — design left to implementer per blueprint Section 15.13.

---

## 🟠 P1 — STRATEGY LIFECYCLE NEVER USED

### Issue #4 — Strategy creation/lifecycle exists but no code ever creates a new strategy

**File:** `strategy/lifecycle.py`, `strategy/save.py`
**Blueprint:** Feature 8 — Strategy Lifecycle System

**What's built:**
- `create_experimental()`, `promote_to_active()`, `retire_strategy()`, `check_trial_eligible()`, `auto_retire_if_underperforming()` — all working

**What's missing — wiring:**

1. **`create_experimental()` is only called from:**
   - `research/engine.py:89` `queue_for_paper_trial()` — but `queue_for_paper_trial()` itself is never called
   - `self_play/mars.py:113` `promote_from_self_play()` — but `promote_from_self_play()` is never called

2. **No code in the running brain creates new strategies.** The only 2 strategies in DB (`stage1_ofi_momentum`, `stage2_sentiment_ofi`) were manually seeded via SQL today.

3. **`auto_retire_if_underperforming()` is never called.** Strategies that underperform are never retired.

4. **`/opt/trading-bot/strategies/experimental/`, `/active/`, `/retired/` directories may not exist** — `strategy/save.py:11-15` references them but they're created on demand via `mkdir(parents=True, exist_ok=True)`. Verify they exist.

**How to fix:**

**Step 1:** Wire `auto_retire_if_underperforming()` into `memory/write.py` `_update_strategy_metrics()`. After updating metrics, call:
```python
from strategy.lifecycle import auto_retire_if_underperforming
auto_retire_if_underperforming(strategy_id, threshold_win_rate=40.0)
```

**Step 2:** Wire `promote_from_self_play()` into `celery_app.py` `run_self_play()`. After episode completes, if win rate over last 20 episodes > 0.55, promote.

**Step 3:** Wire `queue_for_paper_trial()` into the strategy research engine flow. After AirLLM returns a hypothesis with parseable code, call `queue_for_paper_trial()`.

**Step 4:** Verify directories exist:
```
mkdir -p /opt/trading-bot/strategies/{experimental,active,retired}
```

---

## 🟠 P1 — WEB INTELLIGENCE DEAD END

### Issue #5 — Content fetched, sent to AirLLM, but result never written back to DB

**File:** `web_intel/collector.py` lines 117-141

**Blueprint:** Feature 29 — Web Intelligence Module

**What happens currently:**

1. `web_intel_loop()` runs every N minutes (`config.web_intelligence.rss_fetch_interval_minutes`)
2. Calls `fetch_rss_feeds()`, `fetch_reddit()`, `fetch_fear_greed()` — these work ✓
3. Calls `interpret_content(all_items)` which submits each article to `research_strategy` Celery task
4. **THE BREAK:** `research_strategy` Celery task at `celery_app.py:36-40` returns a STRING (raw LLM output). Nothing parses that string back to JSON. Nothing calls `write_signal_to_db()` (defined at `web_intel/collector.py:147`).
5. Result: `web_intelligence` DB table has **0 rows** despite 2-hour container uptime.

**Other broken pieces:**

- `update_source_credibility()` at `web_intel/collector.py:166` is never called
- `serpapi_search()` is defined but never called from `web_intel_loop()`
- `fetch_etherscan()` is defined but never called from `web_intel_loop()` (also requires `ETHERSCAN_API_KEY` which is unset)

**How to fix:**

**Step 1:** Create a new Celery task in `celery_app.py` that wraps the LLM call AND writes the result back:

```python
@app.task(queue="default")
def interpret_and_store(prompt: str, source: str) -> None:
    """Send to AirLLM, parse JSON response, write to web_intelligence table."""
    import sys, json
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from llm.researcher import research
    from web_intel.collector import write_signal_to_db
    try:
        raw = research(prompt, max_new_tokens=512)
        # AE-11: Never eval()/exec() — strict JSON parse only
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end+1])
            parsed["source"] = source
            write_signal_to_db(parsed)
    except Exception:
        pass  # invalid LLM output discarded silently per blueprint
```

**Step 2:** Update `web_intel/collector.py:117` `interpret_content()` to call the new task:
```python
task = app.send_task("celery_app.interpret_and_store", args=[prompt, item.get("source","")], queue="default")
```

**Step 3:** Wire `serpapi_search()` and `fetch_etherscan()` into `web_intel_loop()`:
```python
async def web_intel_loop():
    while True:
        articles = await fetch_rss_feeds()
        reddit = await fetch_reddit()
        await fetch_fear_greed()
        serp = await serpapi_search("crypto market news")  # ADD THIS
        await fetch_etherscan()                            # ADD THIS
        all_items = articles + reddit + serp
        ...
```

**Step 4 (later):** Wire `update_source_credibility()` to a periodic task that runs 72h after each signal and compares predicted sentiment vs actual price direction.

---

## 🟠 P1 — DIRECTION PREDICTION MODEL DOESN'T EXIST (Feature 13)

### Issue #6 — `direction_confidence` is just OFI strength, not a learned model

**File:** `signals/engine.py:75-92`
**Blueprint:** Feature 13 — "Direction Prediction Model: dedicated model with inputs: EMA alignment, RSI/MACD/Stochastic, order book imbalance, funding rate, OI change, buy/sell volume ratio, sentiment, on-chain flow, historical directional accuracy on this pair"
**TASKS.md:** X-09 marked `[ ] NOT BUILT`

**Current implementation (`signals/engine.py:82-88`):**
```python
trade_potential = round(abs(sentiment - 0.5) * 2 * 100, 2)
direction_conf  = round(min(100, max(0, ofi_strength + regime_bonus + tft_bonus)), 2)
```

This is OFI magnitude + regime alignment bonus + TFT bonus (which is 0 because TFT can't load). Not a learned model. Not using EMA, RSI, MACD, funding rate, OI, on-chain flow, or per-pair historical accuracy.

**Consequence:**
74 of 75 closed-trade losses are direction failures. The brain has the wrong-direction signal because there's no real direction prediction model.

**How to fix:**

**Step 1:** Create `ml/direction_model.py`. Use the X-12 direction decoder records (already being written to `brain_actions` JSONB) as training data.

Inputs per blueprint:
- OFI, VPIN (from Redis, current code)
- Sentiment (from Redis, current code)
- Regime (from Redis, current code)
- EMA crossover state (need to compute from candles)
- RSI/MACD/Stochastic (need to compute from candles)
- Funding rate (already in Redis as `{pair}:funding_rate`)
- Historical directional accuracy on this pair (already in Redis as `brain:directional_accuracy:{pair}`)

Output: Direction Confidence Score 0–100.

Model: Start simple — logistic regression on these features trained on the direction-failure-decoded records. Replace with gradient-boosted trees (XGBoost) at 300 trades.

**Step 2:** Wire into `signals/engine.py` `generate_candidate_signals()`:
```python
if brain_state.get("stage", 1) >= 2 and self._paper_closed >= 100:
    from ml.direction_model import predict_direction_confidence
    direction_conf = predict_direction_confidence(pair, features)
else:
    direction_conf = ofi_strength + regime_bonus  # fallback
```

**Step 3:** Train on schedule. Celery task every 50 trades:
```python
@app.task(queue="default")
def retrain_direction_model():
    from ml.direction_model import train_from_decoder_records
    train_from_decoder_records()
```

---

## 🟠 P1 — DCA QUANTITY NOT UPDATED ON DCA FIRE (P&L MISCALCULATED)

### Issue #7 — `execution/paper.py` updates `average_entry` but not `quantity` on DCA

**File:** `execution/paper.py:120-151` `add_dca()`

**Blueprint:** Feature 5 — DCA Loss Recovery System

**Current code:**
```python
def add_dca(self, trade_id, round_number):
    ...
    dca_capital = float(trade["capital_usdt"]) * 0.5  # extra capital added
    ...
    new_avg = (orig_capital * orig_entry + dca_capital * mark_price) / (orig_capital + dca_capital)
    write_trade_update(trade_id, {
        dca_key: mark_price,
        "average_entry": round(new_avg, 8),  # ✓ updated
        "dca_status": json.dumps(dca_status),
        # ❌ quantity NOT updated
    })
```

**Why this matters:**
The PnL at close uses `qty * (mark_price - average_entry)`. After DCA, the average_entry is moved closer to the DCA price, but the quantity that was bought ADDITIONALLY (with the 0.5× capital at the lower price) is never added to the position size.

**Math:** Initial entry: $100 capital × 5× leverage at $1.00 = 500 units. DCA at $0.80: $50 × 5× = +312.5 units (now 812.5 units total). But our code keeps qty=500. At exit at $0.90, the actual gain should be 312.5 × ($0.90 − $0.80) + 500 × ($0.90 − $1.00) = +$31.25 − $50 = −$18.75. Our code computes 500 × ($0.90 − new_avg) where new_avg = $0.923 → 500 × −$0.023 = −$11.5. Wrong by $7.25 per trade.

**How to fix:**

In `execution/paper.py:135-145`, add quantity update:
```python
orig_capital = float(trade["capital_usdt"])
orig_entry = float(trade["average_entry"] or trade["entry_price"])
orig_qty = float(trade["quantity"])

# Additional units bought with DCA capital
dca_qty = (dca_capital * float(trade["leverage"])) / mark_price
new_qty = orig_qty + dca_qty

new_avg = (orig_capital * orig_entry + dca_capital * mark_price) / (orig_capital + dca_capital)

write_trade_update(trade_id, {
    dca_key: mark_price,
    "quantity": round(new_qty, 8),       # ADD THIS LINE
    "average_entry": round(new_avg, 8),
    "dca_status": json.dumps(dca_status),
})
```

---

## 🟠 P1 — MARL, MAML, CONTINUAL LEARNING ARE STUBS

### Issue #8 — Phase 3-4 ML modules have skeleton only

**Files:** `ml/marl.py` (64 lines), `ml/maml.py` (42 lines), `ml/continual_learning.py` (65 lines)
**Blueprint:** Features 17, 21, 22

**What's missing:**

| Feature | Blueprint Requirement | Actual Code |
|---------|----------------------|-------------|
| F21 MARL | 3 hierarchical agents (Day strategic / Hour tactical / Minute execution) with centralized training + decentralized execution, PPO, shared environment, reward decomposition | Loads PPO models from `.zip` (files don't exist), hardcoded action mappings, no Hour Agent, no training pipeline, no environment definition |
| F22 MAML | Inner/outer loop on θ' = θ - α∇L, task distribution from regime history, trigger on BOCPD changepoint | Has formula functions but no model, no task distribution, no training pipeline, no integration point |
| F17 EWC | Fisher information computed from old regime's data, penalty term added to new training, replay buffer w/ reservoir sampling | Fisher matrix initialised to all-zeros (no actual Fisher computation), no training loop, `_replay_buffer` is module-level list (won't persist across restarts) |

**How to fix:**

These activate at Phase 3 (300 trades) / Phase 4 (500-800 trades). We're at 147 trades. **You have time.**

Recommended order:
1. F17 EWC first (needed by F22 MAML for stability)
2. F22 MAML (needs F26 BOCPD output to trigger)
3. F21 MARL (most complex — full 3-tier hierarchy)

For each, the rewrite needs:
- Proper environment class (`gym.Env` subclass) wrapping trade data
- Real Fisher computation (gradient²-mean over replay batch)
- Persist Fisher to disk/Redis (not in-memory)
- Training scheduled as Celery beat task during low-activity hours

This is a multi-day engineering effort per feature. Recommend keeping as stubs until trade volume requires them.

---

## 🟡 P2 — TRADE MEMORY PATTERN MINING NOT IMPLEMENTED (Feature 2)

### Issue #9 — Pattern mining over closed trades doesn't exist

**Blueprint Feature 2:** "The AI mines this database to identify what winning trades have in common vs losing trades — finding patterns in timing, market structure, indicator combinations, pair behaviour, and more."

**What exists:**
- Trade data IS being stored fully ✓
- MemRL retrieves similar trades via pgvector cosine ✓
- Direction Decoder analyses individual direction failures ✓

**What doesn't exist:**
- No code that mines patterns ACROSS the trade corpus
- No clustering, no decision tree on outcome predictors, no association rule mining
- No "winners vs losers" summary

**How to fix:**

Create `memory/pattern_miner.py`:

```python
"""Mine patterns across closed trades — winners vs losers."""
from db import db_conn

def mine_patterns():
    """Run clustering + association rules over last 500 closed trades."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pair, direction, market_regime, timeframe, brain_stage,
                       net_pnl_usdt, failure_type, feature_vector
                FROM trades WHERE status = 'closed' AND is_paper = true
                ORDER BY exit_time DESC LIMIT 500
            """)
            cols = [d[0] for d in cur.description]
            trades = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Group by (regime, direction, pair) — compute win rate per bucket
    from collections import defaultdict
    buckets = defaultdict(lambda: {"win": 0, "loss": 0})
    for t in trades:
        key = (t["market_regime"], t["direction"])
        if (t.get("net_pnl_usdt") or 0) > 0:
            buckets[key]["win"] += 1
        else:
            buckets[key]["loss"] += 1
    return [
        {"context": k, "win_rate": v["win"] / max(1, v["win"] + v["loss"]),
         "n": v["win"] + v["loss"]}
        for k, v in buckets.items()
    ]
```

Schedule as Celery beat task every 6 hours. Write output to `brain:patterns_mined` Redis key for dashboard display.

---

## 🟡 P2 — OPRO SAVES PROMPT BUT NEVER REVERTS ON REGRESSION

### Issue #10 — Regression detection missing

**File:** `self_improve/opro.py`

**Blueprint AB-04:** "Regression detection: revert prompt when new prompt performs worse"

**What exists:**
- `increment_opro_counter()` ✓
- `compute_opro_score()` ✓
- `save_new_prompt()` writes new prompt to brain_state ✓
- `revert_prompt()` function exists ✓

**What's missing:**
- No code COMPARES the ROI score before vs after each prompt change
- No code calls `revert_prompt()` automatically
- The `current_executor_prompt` in brain_state is overwritten on every OPRO run with the new candidate, with no comparison to baseline

**Effect:** OPRO can make the brain WORSE and never roll back.

**How to fix:**

Modify `celery_app.py` `opro_optimize` task to:
1. Compute current window's ROI score
2. Fetch the previous window's ROI score from Redis `opro:last_score`
3. If current_score < previous_score by margin (e.g., -10): call `revert_prompt(previous_prompt)`
4. If current_score ≥ previous_score: keep the new prompt and update `opro:last_score`

```python
@app.task
def opro_optimize(prompt, window_scores):
    ...
    from self_improve.opro import revert_prompt, save_new_prompt, compute_opro_score
    new_score = compute_opro_score(window_scores)
    r = redis_client.get()
    prev_score = float(r.get("opro:last_score") or 50.0)
    prev_prompt = r.get("opro:last_prompt") or prompt

    # Run AirLLM, get candidate
    from llm.researcher import research
    candidate = research(...)

    if new_score < prev_score - 10:
        revert_prompt(prev_prompt)
        log.warning("opro_reverted", prev_score=prev_score, new_score=new_score)
    else:
        save_new_prompt(candidate)
        r.set("opro:last_score", new_score)
        r.set("opro:last_prompt", candidate)
```

---

## 🟡 P2 — COUNTERFACTUAL TRACKING NEVER FIRES (No Rejected Signals)

### Issue #11 — Counterfactual = 0 because all signals are accepted

**File:** `signals/engine.py:79-88` `accept_or_reject()`

**Current logic:**
```python
def accept_or_reject(signal, brain_state):
    strength = float(signal.get("signal_strength") or 0)
    min_strength = 0.1 if stage <= 1 else 30
    if strength < min_strength:
        return False, "signal_too_weak"
    if regime == "turbulent":
        if turbulence > 3.0:
            return False, "turbulence_too_high"
    return True, ""
```

**Problem:** With Stage 2 logic generating only high-confluence signals (sentiment + OFI agreement + regime), every signal passes the `> 30` threshold. Result: **0 rejected signals** → 0 counterfactual tracking → shadow win rate stuck at 0/0.

**Blueprint Feature 9** requires counterfactual tracking on rejected signals.

**How to fix:**

Generate MORE candidate signals at Stage 2 with lower bar, then reject many of them. Lower the Stage 2 sentiment+OFI thresholds in `generate_candidate_signals()`:

```python
# Stage 2: generate more candidates with looser filters
direction = "long" if (sentiment > 0.50 and ofi > 0) else \
            "short" if (sentiment < 0.50 and ofi < 0) else None
```

Then in `accept_or_reject()` keep the strong filter:
```python
if strength < 30 or regime_disagrees:
    return False, "weak_or_regime_disagree"
return True, ""
```

This will generate weak candidates that get rejected → counterfactuals track them → shadow win rate populates.

---

## 🟡 P2 — DASHBOARD MISSING ENDPOINTS (Feature 11 / Section 14.3)

### Issue #12 — 4 blueprint API endpoints don't exist

**File:** `dashboard/api.py`

**Missing endpoints from blueprint Section 14.3:**

| Endpoint | Used by | Status |
|----------|---------|--------|
| `GET /brain/decisions` | Brain Decision Feed panel | NOT BUILT |
| `GET /models` | ML & RL Models Panel (AI-05) | NOT BUILT — panel uses hardcoded data |
| `GET /account/positions` | Account Risk panel | NOT BUILT |
| `GET /web_intel/sources` | Web Intel credibility leaderboard | NOT BUILT |
| `GET /web_intel/strategies` | Strategy extraction queue | NOT BUILT |
| `GET /trades/open` ✓ | — | BUILT |
| `GET /trades/closed` ✓ | — | BUILT |
| `GET /signals/recent` ✓ | — | BUILT (added today) |
| ... | ... | ... |

**How to fix:**

Implement each missing endpoint as a thin DB query. For example:

```python
@app.get("/brain/decisions", dependencies=[Depends(_verify_token)])
async def brain_decisions(limit: int = 50):
    """Recent brain decision log from Redis pub/sub buffer."""
    import redis_client
    r = redis_client.get()
    raw = r.lrange("brain:decisions_log", 0, limit-1)
    return [json.loads(x) for x in raw]
```

Add corresponding pub-sub listener to populate `brain:decisions_log` from the `brain_decision` channel.

---

## 🟡 P2 — METACOGNITIVE MONITOR: ESCALATION NEVER FIRES

### Issue #13 — `evaluate_self_improvement_mechanisms` and `escalate_to_governance` defined but uncalled

**File:** `metacognition/monitor.py:79-101`

**Blueprint AC-04/AC-05:** "Track whether each mechanism is producing measurable improvement; if not, escalate to Feature Governance."

**What's missing:** Nothing calls `evaluate_self_improvement_mechanisms()`. Nothing calls `escalate_to_governance()`.

**How to fix:**

Schedule a Celery beat task once per day:

```python
@app.task(queue="default")
def metacog_daily_eval():
    """AC-04: Daily check whether OPRO/GA/MAML mechanisms are improving outcomes."""
    from metacognition.monitor import evaluate_self_improvement_mechanisms, escalate_to_governance
    import redis_client
    r = redis_client.get()
    trade_count = int(r.get("brain:paper_closed") or 0)
    if trade_count < 100:
        return  # not active yet
    results = evaluate_self_improvement_mechanisms(trade_count)
    for mechanism, success_rate in results.items():
        if success_rate < 30:  # less than 30% success
            escalate_to_governance(mechanism, f"7-day success rate {success_rate}%")
```

Add to beat schedule:
```python
"metacog-daily-eval": {
    "task": "celery_app.metacog_daily_eval",
    "schedule": crontab(hour=4, minute=0),
}
```

---

## 🟡 P2 — MEMRL RETRIEVAL CALLED BUT NEVER INFLUENCES DECISION

### Issue #14 — Retrieval logs the result, doesn't change behavior

**File:** `brain/soar.py:217-232` (recently added)

**Current code:**
```python
memories = retrieve_relevant_memories(context_trade, self._paper_closed, top_k=5)
if memories:
    wins = sum(1 for m in memories if (m.get("net_pnl_usdt") or 0) > 0)
    log.debug("memrl_context", similar_trades=len(memories),
              wins=wins, losses=len(memories)-wins)
```

**Problem:** The memory retrieval just logs the count. It doesn't:
- Adjust the signal_strength based on past similar-trade outcomes
- Reject the signal if 80%+ of similar past trades lost
- Boost confidence if 70%+ won

**Blueprint Feature 35:** "Retrieve top-10 memories most similar to current setup; these inform the decision."

**How to fix:**

Pass `memrl_context` into brain_state and consume it in `signals/engine.py`:

```python
# In brain/soar.py _act():
similar_wins, similar_total = compute_similar_outcomes(observation)
brain_state["memrl_win_rate"] = similar_wins / max(1, similar_total)

# In signals/engine.py generate_candidate_signals():
memrl_wr = brain_state.get("memrl_win_rate", 0.5)
if memrl_wr < 0.3:
    return []  # similar past setups mostly failed — skip
elif memrl_wr > 0.6:
    signal_strength *= 1.2  # boost
```

---

## ⚪ INFORMATIONAL — FEATURES THAT WORK CORRECTLY

These were verified against blueprint and confirmed compliant:

- **F4 Trailing SL** — `risk/manager.py` correctly ratchets only into profit, both long/short symmetric
- **F5 DCA Triggers** — `risk/manager.py` `check_dca_triggers()` correctly fires at -20% / -40% from average_entry
- **F14 HMM Regime** — `ml/hmm.py` calls Viterbi on real returns, regime is being updated to "bull"
- **F15 OFI/VPIN/Kyle's λ/Amihud** — All computed in `data/feed.py` every 10s
- **F26 BOCPD** — Now wired into data_feed loop, ruptures `Binseg` algorithm running
- **F28 Turbulence Index** — Computed correctly in `data/feed.py`
- **F31 Performance Analytics** — Updates after every close
- **F32 Account Risk Monitor** — Computing real margin/exposure
- **F33 Self-Healing Watchdog** — Heartbeat write in `watchdog/main.py`, restarts failing services
- **F40 Local LLM Layer** — Ollama Phi-3 / Mistral via aiohttp, AirLLM replaced with llama.cpp (justified in `llm/researcher.py:7`)
- **F42 SOAR Cognitive Loop** — Observe → Decide → Act, no Reflect step (compliant with F43B)
- **F44 Directional Hedge** — Blueprint Stage 3 feature, not built yet (correct, premature)

---

## SECTION 10.2 — BRAIN COMMAND AUTHORITIES AUDIT (added 2026-05-21 cont. 18)

The blueprint Section 10.2 lists 11 authorities the Master Brain is supposed
to have. Today's audit found 5 real gaps. Filing them here as Issues #15–#19
so future sessions can pick them up. Numbering continues from the original 14.

| # | Authority | Status | Evidence / Gap |
|---|---|---|---|
| 1 | Trade Execution (open/close/modify) | ✅ COMPLETE | `execution/{paper,live}.py` |
| 2 | Stop Loss (set/move/tighten/widen) | ✅ COMPLETE | `monitor_trailing_sl` + DCA breakeven |
| 3 | Take Profit override | ✅ N/A by design | Blueprint defers all exits to trailing SL |
| 4 | Strategies (create/modify/mutate/disable/delete) | ✅ COMPLETE | cont. 7-17 closed full lifecycle |
| 5 | **ML Sub-Agents** (spawn/define/retrain/retire) | ❌ **Issue #15** | Models defined in `ml/architectures.py`, trained via fixed loops. Brain has no spawn-new-model authority. **Multi-session scope.** |
| 6 | **RL Agents** (spawn/reward/evaluate/replace) | ❌ **Issue #16** | F21 MARL agents pre-defined; no spawn mechanism. **Multi-session scope.** |
| 7 | **Risk Parameters dynamic adjust** | ⚠️ **Issue #17** | Brain READS `bot:max_open_trades/leverage/etc.` but doesn't ADJUST them — user-set static values. Brain should be able to scale-down leverage in low-confidence regimes etc. |
| 8 | Pair Selection (add/remove) | ✅ COMPLETE | Scanner sadd ACTIVE_PAIRS + F10 ranking |
| 9 | **Criteria Weights Brain-learned** | ✅ **CLOSED cont. 18** | Was Issue #18: weights READ from Redis but config-static fallback was permanent. Cont. 18 added `ml/criteria_weights.py` correlation-based learner + daily celery task + `pair_selections` audit table. |
| 10 | **Experimentation (A/B tests)** | ❌ **Issue #19** | Blueprint Section 10.10: "A/B tested: new strategy vs current best, same market conditions." UCB1 selector samples ONE strategy per signal — no paired comparison. Need shadow-mode runner. |
| 11 | **Data Sources (add/remove feeds)** | ❌ **Issue #20** | No brain-driven enable/disable of data feeds based on measured predictive value. F30 governance handles per-FEATURE deactivation, but per-DATA-SOURCE valuation (e.g., is web_intel adding alpha?) is absent. |

**Status: 4 P1 issues opened (#15, #16, #17, #19, #20), 1 closed (#18 / cont. 18).**

---

## SUMMARY TABLE

| # | Feature | Severity | Status |
|---|---------|----------|--------|
| 1 | F19/F20/F24/F34 model loading | P0 | torch.save format broken |
| 2 | F19/F20/F24/F34 architectures | P0 | Toy networks vs paper specs |
| 3 | F30 Feature Governance | P0 | Registry never enforces |
| 4 | F8 Strategy Lifecycle | P1 | Functions exist, never called |
| 5 | F29 Web Intelligence | P1 | LLM output never parsed back |
| 6 | F13 Direction Model (X-09) | P1 | Not built — uses OFI proxy |
| 7 | F5 DCA quantity update | P1 | PnL miscalc on DCA'd trades |
| 8 | F17/F21/F22 (EWC/MARL/MAML) | P1 | Stubs |
| 9 | F2 Pattern Mining | P2 | Not built |
| 10 | F39A OPRO regression detection | P2 | No revert logic |
| 11 | F9 Counterfactual Tracking | P2 | No rejected signals to track |
| 12 | Dashboard endpoints | P2 | 5 endpoints missing |
| 13 | F43 Metacog escalation | P2 | Never called |
| 14 | F35 MemRL retrieval | P2 | Logged but doesn't change decisions |

**Total issues: 14**
- P0 (broken/fake output): 3
- P1 (unwired): 5
- P2 (simplified): 6

**Recommended fix order:**
1. **Issue #1 first** (model loading) — unlocks Issues #2 and many fallback paths
2. **Issue #7 second** (DCA quantity) — silent P&L corruption is dangerous
3. **Issue #3 third** (Feature Governance enforcement) — blueprint depends on it
4. **Issue #5 fourth** (Web Intelligence parse-back)
5. **Issue #6 fifth** (Direction Model — biggest single performance lever)
6. Remaining issues in any order

---

## VERIFICATION CHECKLIST (after each fix)

After fixing each issue, verify:

1. **Issue #1:** `docker logs trading-bot-brain-1 | grep world_model_loaded` should show success line
2. **Issue #7:** Open a paper trade, manually trigger DCA via Redis, check `quantity` column in DB increased
3. **Issue #3:** `docker exec trading-bot-redis-1 redis-cli get "feature:F14_HMM:contribution"` should return JSON history after first trade close
4. **Issue #5:** `docker exec trading-bot-postgres-1 psql -U botuser -d trading_bot -c "SELECT COUNT(*) FROM web_intelligence"` should be > 0 within 1 hour
5. **Issue #6:** New trade's `direction_confidence` column should differ across pairs (not just OFI strength)

---

*End of original 2026-05-17 audit. All findings verified by direct file read.*

---

## DEVIATIONS LOG

### D-01 (2026-05-20) — Llama 70B background runtime: local → cloud failover

**Blueprint Section 8.10 says:** "AirLLM: Llama 3.1 70B (30–120 sec) — background tasks only" and "All models run locally — zero external API calls".

**What was actually deployed (until 2026-05-20):**
1. AirLLM was abandoned during initial setup — the one-time HuggingFace → AirLLM weight conversion needs all 70B shards loaded simultaneously (~132 GB RAM). The target host has 16 GB.
2. Replacement: `llama-cpp-python[server]` serving the 42.5 GB Q4_K_M GGUF via mmap. The container's `restartCount` reached **1007** (verified via `docker inspect`) — every restart re-ran the multi-minute CPU tensor repack from block 0 and never finished a load. Background AirLLM tasks (research, OPRO, AI Scientist, DGM, sleep consolidation, web_intel interpretation) consequently produced zero successful inferences in production.

**Replacement (current):** 3-provider cloud failover chain in `llm/researcher.py`:

| Order | Provider | Model | Free-tier limits |
|---|---|---|---|
| Primary | Groq | `llama-3.3-70b-versatile` | 30 RPM, 6 K TPM, 1 K RPD |
| Fallback 1 | Cerebras | `llama-3.3-70b` | 30 RPM, 60-100 K TPM, 1 M tokens/day |
| Fallback 2 | SambaNova | `Meta-Llama-3.3-70B-Instruct` | 30 RPM persistent |

**What is preserved from the blueprint:**
- 70B parameter class (Llama 3.3 = Meta's current refresh of the Llama-3 line)
- Background-only call discipline (never reached from the SOAR trading loop)
- Fail-soft semantics (`handle_airllm_failure` already in place)
- No-Reflection rule (`assert_no_reflection` still called on every prompt)
- Same task assignment list (`config.yaml` `llamacpp_tasks` unchanged)

**What changes:**
- "All local" → cloud-hosted. Prompts (market snapshots, trade summaries, strategy code) leave the host. API keys live only in `.env` (gitignored). No customer-identifying data is sent.

**Why this is the right deviation:** the 70B parameter requirement is load-bearing for code generation (F36 strategy research, F39B DGM rewrites), hypothesis quality (F39C AI Scientist), and nuanced text interpretation (F18 web_intel). Downgrading to a smaller local model would compromise those features. Keeping the 70B *class* on a different runtime preserves blueprint behavior.

**Files touched:** `.env`, `config.py`, `config.yaml`, `llm/researcher.py` (rewrite), `docker-compose.yml` (removed `llama_cpp` service), `main.py` (startup check switched to API-key presence), `dashboard/api.py` (health probe), `pretrainer/main.py` (dropped GGUF file check), `celery_app.py` (comment), and `BOT_BLUEPRINT.md` Section 8.10 (inline deviation note).

---

### D-02 (2026-05-20) — Feature Governance peer-comparison guard

**Blueprint Section** (Feature Governance, W-03 Degradation): a feature is deactivated when its last N=10 contribution scores are all negative.

**Problem observed in production:** every closed trade gives every active feature a contribution of +1 (win) or -1 (loss). When the bot has a losing streak (≥10 consecutive losses, which is common with a sub-50% win rate), every active feature accumulates an identical all-negative history. Governance then deactivates **all 36 features simultaneously**, leaving the bot inert. Evidence: `brain:active_feature_flags` in Redis was found with all 36 features set to `false` despite no recorded deactivation trigger — every feature got deactivated equally in the same governance run.

**Fix:** added `_is_peer_outlier()` guard in `feature_governance/registry.py`. A feature is deactivated only when its mean contribution over the last 50 trades is meaningfully worse (`margin=0.2`) than the average of all currently-active peers. If every feature is bad equally, it's a bot-wide problem, not a feature-specific failure — governance now logs `governance_skip_blanket_deactivation` and takes no action.

**What is preserved:** original degradation/blocking/bad-trade-causation detection signals; structural-vs-temporary classification; periodic re-evaluation of dormant features. Only the deactivation step now requires being an outlier vs peers.

**Files touched:** `feature_governance/registry.py`.

---

### D-03 (2026-05-20) — Direction selection: regime-first, sentiment as confidence

**Original behaviour (`signals/engine.py`):**
```python
direction = "long" if (sentiment > 0.55 and ofi > 0) else \
            "short" if (sentiment < 0.45 and ofi < 0) else None
```
Sentiment was a hard direction gate.

**Problem observed in production:**
Sentiment source is the Fear & Greed Index proxy (blueprint F18 placeholder until CryptoBERT/FinBERT is wired). F&G stuck at 27 (extreme fear) for the production window. With `sentiment=0.27`, the `long` branch is mathematically unreachable — no long trade can ever fire regardless of OFI sign, regime, or ML forecast agreement. Over 500 closed trades: **500/500 short, 0/500 long, all in bull regime, 34% win rate, net -$1.12/trade**. OFI distribution across 112 active pairs at the time of investigation: 22 positive (19.6%), 21 negative (18.8%), 69 near-zero — i.e., the market was offering long opportunities the bot literally could not take.

**Fix:** regime drives direction; OFI confirms; sentiment only matters as a contrarian filter when the regime is wrong.

| Regime | Default direction | Contrarian condition |
|---|---|---|
| bull | long if OFI > 0.0001 | short only if OFI < -0.002 AND sentiment < 0.30 |
| bear | short if OFI < -0.0001 | long only if OFI > 0.002 AND sentiment > 0.70 |
| turbulent | OFI sign, requires \|OFI\| > 0.002 AND extreme sentiment | — |
| unknown | OFI sign, requires \|OFI\| > 0.0005 | — |

**What is preserved:** blueprint F14 ("every system conditions on detected regime") — regime is now the *primary* conditioning, not a secondary check applied after sentiment. F19 TFT and F20 PatchTST bonuses still adjust `direction_confidence` (not direction). F13 Direction Model still overrides confidence when trained.

**Reversion criteria:** if real CryptoBERT/FinBERT replaces the F&G proxy (blueprint F18 proper implementation) and the new sentiment shows reasonable variance across the trading day, the original sentiment-gated logic could be restored. Until then, the proxy is too coarse to use as a hard gate.

**Files touched:** `signals/engine.py`.

---

### D-04 (2026-05-20) — F13 Direction Model expanded from 3 → 7 inputs

**Blueprint Section** (Feature 13 — Direction Prediction Model): "Inputs: trend indicators, momentum signals, order book imbalance, funding rate, open interest changes, volume-weighted price direction, market sentiment, on-chain flow direction, historical directional accuracy on this pair."

**Original (audit Issue #6 — `ml/direction_model.py`):** 3 inputs (`ofi`, `vpin`, `sentiment`). `vpin` was identical to `ofi` in the data feed (`data/feed.py` sets both to `abs(returns)`), so the effective input set was 2 distinct signals + a globally-stuck sentiment proxy. Predictions varied across only 4 distinct values over 139 trades.

**Now:** 7 inputs from Redis sources that actually vary per pair:
- `ofi` — order book imbalance
- `sentiment` — F&G proxy (placeholder for F18 CryptoBERT)
- `funding_rate` — perp funding
- `change_24h` — 24h price move (trend / momentum proxy)
- `volume_24h` — 24h notional volume
- `amihud` — illiquidity / price impact
- `dir_acc_pair` — historical per-pair directional accuracy 0-100

**Model upgrade:** logistic regression below 300 samples; `GradientBoostingClassifier` at ≥300 (sklearn-bundled tree-boosting, same family as XGBoost the blueprint requests — avoids new dependency). Training logs now report feature importances per input.

**Still missing vs blueprint** (deferred to D-04a):
- `open_interest_change` — no OI data in the current data feed
- `on_chain_flow_direction` — no on-chain data wired (would need Etherscan/Glassnode integration)

**Backwards compatibility:** the old `feature_vector` (4 fields) is silently dropped from training. The model retrains every 50 closed trades; new-format trades will reach the 60-sample minimum within ~12-24 hours of trade flow.

**Files touched:** `signals/engine.py` (write expanded `feature_vector`), `ml/direction_model.py` (consume new features + tree-boost at 300+).

---

### D-05 (2026-05-20) — F15 microstructure features actually distinct

**Original (`data/feed.py:84-87`):**
```python
r.set(OFI,    abs(ret))
r.set(VPIN,   abs(ret))           # ← identical to OFI magnitude
r.set(AMIHUD, abs(ret) / vol)
r.set(KYLES_LAMBDA, abs(ret))     # ← also identical to VPIN
```
Three of the four supposedly-distinct microstructure measures were the same value. F13 direction model had `vpin` listed but it carried zero independent signal vs `ofi`.

**Fix:** compute each via its actual definition (or best proxy given available data):
- **OFI** = signed 1-tick return (positive = up-tick = buy pressure proxy)
- **VPIN** = rolling stdev of last 20 returns — proxies flow toxicity / informed-trading bursts in absence of tick-level signed volume
- **Amihud** = |return| / volume — illiquidity (unchanged, was correct)
- **Kyle's λ** = |return| / √volume — price impact per unit notional (dimensionally different from Amihud)

Each now varies independently across pairs. Verified before/after on `BTCUSDT`: previously OFI=VPIN=Kyle, now three distinct values.

**Limitations vs ideal blueprint:** without tick-level signed volume (would require Binance WebSocket aggregated-trades stream, not currently subscribed), proper VPIN (Easley/Lopez-de-Prado) cannot be computed. The rolling-vol proxy is a defensible approximation but not the canonical metric.

**Files touched:** `data/feed.py`.

---

### D-06 (2026-05-20) — F44 Directional Hedge implemented from scratch

**Blueprint Section** (Feature 44 — Directional Hedge on Confirmed Trend Continuation): "When a losing trade's DCA has fired and price continues falling, the Brain opens a counter-position on the same symbol… activates at Stage 3, 300+ trades, directional accuracy ≥ 55%, requires HMM regime trending, price -5% past DCA-1, free balance ≥ hedge_capital × 2. Sizing = abs(unrealised_loss) / (expected_continuation_pct × leverage), capped 50% of original capital."

**Before:** feature was registered in governance but had no implementation — `feature_health` reported `no_check` because the code didn't exist.

**What was built:**
- `migrations/014_trades_hedge_relation.sql` — adds `trades.hedge_of_trade_id` (FK) and `trades.is_hedge_active` (bool) so parent and hedge trades can be linked. Partial indexes for the per-tick lookup.
- `risk/hedge.py` — new module with two entry points:
  - `check_and_open_hedge(trade, engine)`: runs all 5 trigger gates per blueprint; opens an opposite-direction trade via the same `engine.open_trade(...)` API used elsewhere; links parent ↔ hedge in DB.
  - `maybe_lock_hedge_breakeven(trade, engine)`: when a hedge is +5% in profit, locks trailing SL to entry per blueprint exit rule.
- `risk/manager.monitor_trailing_sl` — calls both on every tick for each open trade; cheap because each call short-circuits early when gates fail.
- Redis evidence keys: `hedge:eval_count`, `hedge:open_count`, `hedge:last_open_ts`, `hedge:last_eval_reason` so `feature_health` can observe activity.

**What is preserved from blueprint:**
- All 5 trigger conditions (DCA fired, -5% past DCA, trending regime, dir_acc ≥ 55%, balance gate)
- Sizing formula and 50% cap
- Trailing SL exit, breakeven lock at +5%
- F30 governance gate respects deactivation
- Parent trade unaffected — hedge is a separate position

**Known limitations:**
- The `expected_continuation_pct` and other params are hardcoded constants. Blueprint says "Brain learns optimal parameters through OPRO/GA" — that integration is future work. For now the constants follow blueprint defaults.
- Triggers are evaluated on the standard 1s monitor tick; under heavy load could be debounced.

**Files touched:** `risk/hedge.py` (new, 220 lines), `risk/manager.py` (wire in monitor loop), `tools/feature_health.py` (check_F44_hedge replaces no_check stub), `migrations/014_trades_hedge_relation.sql` (new).


---

### D-07 (2026-05-20) — Feature Governance BadTradeCausation peer guard + frozen-snapshot

**Symptom (observed in production at 2026-05-20 10:45 UTC):**
36/36 features deactivated in a 32-second window. Every row showed identical `decode_reason` ("Mean contribution -0.368 over 57 samples"), identical `failure_mode` ("Degradation,BadTradeCausation"), classification "structural". Dashboard's "Feature Health (37)" panel listed every feature as `turned_off`. The bot was operating with zero blueprint features active.

**Root causes (two distinct bugs, both in `feature_governance/registry.py`):**

1. **`check_bad_trade_causation` had no peer guard.** The check returned True whenever `all(v < 0 for v in history[-8:])` — i.e., 8 consecutive losing trades. But `update_contribution` gives EVERY active feature `-1` on every loss (it does not discriminate per-feature contribution). So during any 8-trade losing streak, every active feature has 8 consecutive `-1`s and W-06 trips on all of them simultaneously. D-02 had patched the Degradation check (W-03) with a peer-comparison guard but never propagated the same pattern to W-06.

2. **`_is_peer_outlier` had a cascading-shrink race.** The peer mean was recomputed inside each per-feature check by reading the current `BRAIN_ACTIVE_FLAGS`. As `run_full_governance_check` deactivated features one by one, the peer set shrank. Once `peer_n` dropped below 5 (line 84 in the original), the fallback path used the absolute check `self_mean < -0.5` — which a losing streak satisfies for every feature. Result: D-02's peer guard caught the first N features, then collapsed and let the rest through.

**Fix:**
- New `check_bad_trade_causation(feature_id, precomputed_peer_loss_rate=...)` requires `self_loss_rate > peer_loss_rate + 0.15` before flagging. Mirrors D-02's `self_mean < peer_mean - 0.2` pattern but for the binary-loss W-06 view. Strict-fallback (`return True`) when `peer_n < 5` so a genuinely-broken solo feature still gets caught when most peers are off.
- New `_peer_loss_rate(n_recent)` helper — peers' average loss rate over their last N contribution samples.
- `run_full_governance_check` now snapshots `_peer_mean_contribution()` AND `_peer_loss_rate(8)` ONCE at the top of the run, before any deactivations. Both snapshots are passed into the per-feature checks via the new `precomputed_peer_loss_rate` and `_is_peer_outlier_frozen(feature_id, snapshot_peer_mean, snapshot_peer_n)` helpers. Cascading-shrink race eliminated by construction.
- `_is_peer_outlier_frozen` removes the absolute `self_mean < -0.5` fallback entirely — when `snapshot_peer_n < 5`, the check returns False (skip deactivation) instead. A genuinely-broken solo feature still gets caught via `check_bad_trade_causation`'s own strict fallback, which is the right granularity. The absolute fallback was the mechanism behind the 36/36 cascade.

**Immediate recovery applied:**
```sql
UPDATE feature_governance
SET status='active', current_weight=1.0,
    failure_mode=NULL, decode_reason=NULL, turned_off_at=NULL
WHERE status='turned_off';   -- 36 rows
```
Plus `DEL brain:active_feature_flags` so `is_active()` defaults to True for all features again.

**Why this matters beyond the immediate incident:**
With every feature deactivated, F18 sentiment scoring (just shipped 2026-05-20) was returning `{"status": "f18_inactive"}` on every Celery beat — the CryptoBERT/FinBERT models never even downloaded. Same blanket-off state would prevent any future feature from coming online. D-07 closes the structural issue that made D-02 incomplete.

**Files touched:** `feature_governance/registry.py`.

---

### D-08 (2026-05-20) — F44 Brain-learned hedge parameters

**Blueprint Section** (Feature 44): "Brain learns optimal parameters through OPRO/GA."

**Before:** the hedge module shipped in D-06 with five blueprint-default scalars hardcoded as module-level constants in `risk/hedge.py`: `_TRIGGER_PCT_BEYOND_DCA=0.05`, `_EXPECTED_CONTINUATION_PCT=0.05`, `_HEDGE_CAP_FRAC=0.50`, `_MIN_DIRECTIONAL_ACCURACY=55.0`, breakeven-lock threshold `0.05`. PROGRESS cont. 4 explicitly flagged this as Rule-4 simplified: "constants hardcoded… Brain learns optimal parameters through OPRO/GA — that integration is future work."

**Why constant-α MC + exploration was chosen over GA or OPRO:**
- GA needs hundreds of samples per generation; hedges fire ~1-2 per day in production, so GA convergence is impractical at our signal rate.
- OPRO optimises prompt text, not numeric scalars.
- The F35 Q-learning module solved an identical small-N regime via constant-α MC with exploration jitter. Same pattern works here: explore around the current best each open, attribute close outcomes to the value used, update via `Q ← Q + α · sign(r) · (value_used - Q)`.

**What was built:**
- **`risk/hedge_params.py`** (new module): per-parameter metadata (default, bounds, exploration sigma, description). `get_param(name, with_exploration=False)` reads Redis with hardcoded fallback when `n_samples < 10`. `sample_open_params()` returns a full param dict with exploration noise sampled per call. `record_outcome(params_used, net_pnl_usdt, capital_usdt)` does the MC update per param. `get_all_learned()` exposes state for the dashboard.
- **`risk/hedge.py`** rewired: the five hardcoded constants are gone; the module now calls `sample_open_params()` once per evaluation, uses those values throughout the trigger/sizing logic, and stamps the snapshot into the new hedge trade's `feature_vector` under key `hedge_params_used`. `maybe_lock_hedge_breakeven` reads the breakeven threshold from the snapshot stored on the trade row so the value used at open is honoured at close.
- **`memory/write.py:write_trade_close`** added a hedge close hook: if the closed trade has `hedge_of_trade_id` set, parse `feature_vector.hedge_params_used` and call `record_outcome(params, net_pnl_usdt, capital_usdt)`. F44 governance gate.
- **`dashboard/api.py:/hedge/learned_params`** endpoint returning per-parameter current value, default, n_samples, trusted flag, drift % from default, last update age.
- **`frontend/src/panels/HedgeLearnedParamsPanel.tsx`** — new dashboard panel showing the 5 params with color-coded drift, learned-vs-default state, and last-update age. Polls every 30s.

**Evidence keys (Redis):**
- `hedge:learned_params:{name}` — JSON with `value`, `n_samples`, `last_update_ts` per param.
- `hedge_params:updates_count`, `hedge_params:last_update_ts`, `hedge_params:last_reward` — cumulative learner state for `feature_health` and the dashboard summary header.

**Smoke test (synthetic close, real Redis):**
```
sampled: hedge_cap_frac=0.5368 (with exploration noise)
LOSS reward=-0.10 → hedge_cap_frac 0.5000 → 0.4996  (pushed AWAY from value_used)

sampled: expected_continuation_pct=0.0502
WIN  reward=+0.12 → expected_continuation_pct 0.0500 → 0.050024  (pulled TOWARD)
```
Both directions of the MC update verified. `n_samples=2 < 10 → trusted=False`, so `get_param()` still returns the blueprint default until the warm-up window closes — preserves D-06 behaviour during training.

**Rule 4 honesty notes:**
1. **Learning rate vs reward signal noise.** α=0.1 over the 5 hedge params with at most a few outcomes per day means convergence takes weeks even when the signal is consistent. Acceptable: the alternative (no learning) is worse, and the fallback-to-default gate prevents premature commitment to a noisy estimate.
2. **No per-pair / per-regime conditioning.** The current learned value is global across all pairs and regimes. Per-(pair, regime) breakdown would multiply state by ~24× (per the F35 q_learning bucketing); not worth it until base learning has accumulated samples globally first.
3. **Exploration noise is symmetric Gaussian.** A simple Thompson-sampling-style posterior would be more principled but adds complexity; the constant-σ noise gets the basic effect (sample alternatives, attribute outcomes) without the machinery.

**Files touched:** `risk/hedge_params.py` (new, ~210 lines), `risk/hedge.py` (constants replaced with `sample_open_params` + breakeven from snapshot), `memory/write.py` (close-time hook), `dashboard/api.py` (new endpoint), `frontend/src/api.ts` + `frontend/src/panels/HedgeLearnedParamsPanel.tsx` + `frontend/src/App.tsx` (dashboard panel), `tools/_hedge_params_smoke.py` (diagnostic).
