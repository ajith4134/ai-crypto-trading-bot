# Advanced Candle / Time-Series ML — Survey of Unimplemented SOTA

Topic slug: `advanced_candle_features`
Created: 2026-05-27 (cont. 53 follow-up)
Trigger: User — "is there any other ultra advanced feature or ultra advanced AI or ML or deep learning or neural networks on candle chart pattern 1m/5m/15m/1hr excluding what we already implemented on the disk search every online source"

---

## A. What's already on disk (Rule 2 verified — confirmed via grep / blueprint)

| ID | Feature | Status |
|---|---|---|
| F14 | HMM regime detector | shipped |
| F19 | TFT (Temporal Fusion Transformer, 1 h horizon) | shipped, pre-trained |
| F20 | PatchTST (16 h horizon) | shipped, pre-trained |
| F24 | **GNN (Graph Neural Network, cross-pair)** | shipped per blueprint |
| F27 | Transfer Entropy | shipped |
| F35 | MemRL — memory embedding for trade retrieval | shipped (RAG-style for trades) |
| F47 | Brain-learned trailing-ratchet parameters | shipped |
| F48 | **CandleNet** 1m/5m/15m — CNN + GRU + TCN + GAF + Heikin-Ashi + regime-conditioned | shipped (training in flight per cont. 49) |
| F48 §C | RL Entry Timing Agent (PPO, 22-dim state, 3 actions) | shipped, inactive (paper_closed < 1000) |
| F49 | Autonomous Self-Training (Drift Detector, Perf Monitor, Auto HPO, Active Learning, Model Versions, Online Learner) | shipped |
| F50a | Scanner candle-setup score | shipped |
| F50b | Multi-TF hierarchical direction cascade (1h/15m/5m/1m + 4h veto) | shipped |
| F51d | Chandelier Exit (ATR-anchored watermark) | shipped |
| F51e | Regime-Adaptive Profit-Lock + MTF Reversal Guards (Paths D/E/F + P4) | **shipped this session (cont. 53)** |

So our stack already covers: candle CNN-Transformer hybrids, GAF vision encoding, GNN cross-pair, RL entry, RL trail params, regime-conditioning via concat, MTF cascade voting, autonomous retraining, drift detection, online learning.

---

## B. SOTA features NOT yet on disk — gap analysis

### B1. Mamba State-Space Models (Linear-time long-context)
**What it is**: Selective State-Space Model. Replaces self-attention with a parameter-efficient gated SSM. Linear time in sequence length vs Transformer's quadratic — handles 60-2000 candle context cheaply.
**Crypto-specific repo (production-ready)**: [CryptoMamba](https://github.com/MShahabSepehri/CryptoMamba) — "first framework to leverage SSMs for Bitcoin price prediction", paper [arXiv:2501.01010](https://arxiv.org/abs/2501.01010). Beats LSTM + Transformer baselines.
**General**: [Mamba4Cast](https://github.com/automl/Mamba4Cast) zero-shot forecaster, [MambaTS](https://github.com/XiudingCai/MambaTS-pytorch) long-term TS, [state-spaces/mamba](https://github.com/state-spaces/mamba) base architecture.
**Why it matters for us**: F19 TFT and F20 PatchTST are Transformer-based. Replacing or augmenting with Mamba gives us:
  - Longer effective context (e.g. 512 1m candles ≈ 8 h history at inference)
  - Faster inference per pair (matters at 60 active pairs × 4 TFs)
  - Better long-range dependency capture (overnight / session-to-session)
**Blueprint context**: this is **F50c** (already designed but NOT YET BUILT). Decision Mamba (F50d), continuous live training (F50e), RAG candle memory (F50f) are downstream.
**Risk / cost**: requires causal-conv1D + selective_scan_interface CUDA kernels — heavier compile. On CPU there's a fallback `mamba-ssm` mode but ~3-5× slower. Add `mamba-ssm` + `causal-conv1d` to requirements. Pretraining cost similar to F48.
**Fit**: high.

### B2. Time-Series Foundation Models (Zero-Shot)
**What they are**: pre-trained on billions of TS points, used zero-shot or fine-tuned. The post-2025 wave.
**Candidates ranked by community / docs / production-readiness**:
| Model | Vendor | Params | Context | Strengths |
|---|---|---|---|---|
| [Chronos-2](https://github.com/amazon-science/chronos-forecasting) | Amazon | 8M–710M | 2 K | best docs, 300+ fc/s/GPU, Hugging Face native |
| [TimesFM-2.5](https://huggingface.co/google/timesfm-2.5-200m-pytorch) | Google | 200 M | 16 K | longest context, decoder-only |
| [Moirai-2](https://huggingface.co/Salesforce/moirai-2.0-R-small) | Salesforce | 14 M–311 M | flexible | true multivariate, any-frequency |
| Kronos | THUML | 100 M+ | financial-pretrained | finance-domain pretraining |
| [Lag-LLama](https://github.com/time-series-foundation-models/lag-llama) | ServiceNow | 200 M | 1 K | probabilistic forecasts |
**Why it matters**: cold-start. Right now F19 TFT / F20 PatchTST need 2 years of historical OHLCV pretraining (≈5 h pretrainer run per cont. 49). A foundation model could produce a usable baseline forecast **on day 0 with zero training**. We then fine-tune over the next month as live data accumulates.
**Fit for us**: high for the bootstrap window. As a permanent replacement: maybe — needs A/B vs custom CandleNet. Reference [arXiv:2511.18578](https://arxiv.org/html/2511.18578v1) — "ReVisiting TS Foundation Models in Finance" 2026.
**Risk**: license sometimes restrictive (Chronos = Apache 2.0 ✅, Moirai = CC-BY 4.0 ✅, Lag-LLama = Apache 2.0 ✅, TimesFM = Apache 2.0 ✅). Model size: TimesFM 200M weights ≈ 800 MB on disk. Inference latency on CPU: 100–500 ms/forecast depending on context.

### B3. Diffusion Models for Probabilistic Forecasts
**What they are**: denoising-diffusion in the time-series latent space. Outputs a **distribution** over future values, not a point estimate.
**SOTA paper (2026)**: [Diffolio — Diffusion Model for Multivariate Probabilistic Financial Time-Series Forecasting and Portfolio Construction](https://arxiv.org/abs/2511.07014). Hierarchical attention (asset-level + market-level) + correlation-guided regulariser.
**Other**: [StochDiff](https://arxiv.org/pdf/2406.02827) stochastic latent diffusion, [TimeGrad](https://arxiv.org/pdf/2101.12072) autoregressive denoising.
**Why it matters**: enables **uncertainty-aware sizing**. Today our position sizing assumes a point forecast. With a diffusion-predicted full distribution, we can:
  - Compute VaR / CVaR at entry time → size each position by tail risk, not just mean
  - Reject signals where forecast variance is too wide (low-conviction signal)
  - Build a probabilistic stop placement (SL at 5 % CVaR boundary instead of fixed ATR multiple)
**Fit**: medium-high. Big architectural fit with the existing `risk/manager.py` sizing path. Adds ~50-100 ms inference latency per pair.
**Risk**: training cost. Diffusion models need ~10-50× the compute of equivalent CNN-GRU. Would either run nightly on Diffolio-checkpoint or use frozen Diffolio + a small adapter.

### B4. Mixture-of-Experts (Sparse, Per-Regime Routing)
**What it is**: a gating network routes each input to a small subset (top-k) of "expert" sub-networks. Each expert specialises in a regime / pair-cluster / volatility band.
**Time-series papers**:
  - [Adaptive Market Intelligence: MoE Framework](https://arxiv.org/pdf/2508.02686) (2025)
  - [Task-Aware MoE for Time Series Analysis](https://arxiv.org/pdf/2509.22279) (2025)
  - [LLM-Based Routing in MoE for Trading](https://arxiv.org/pdf/2501.09636) (2025)
**Where we are now**: F48 CandleNet has **regime-conditioned heads** (HMM one-hot concat at the dense layer — arXiv:2603.19136). That's *implicit* expert routing — but it's dense (all heads always active) and the regime is hand-coded HMM.
**What true MoE adds**:
  - Sparse activation → 3-4× inference speedup per pair for the same total params
  - Learned router (no HMM dependency for the routing decision)
  - Expert specialisation can find sub-regimes HMM misses (e.g. "bull-but-funding-spike", "bear-with-volume-collapse")
**Fit**: medium. Could be a future evolution of F48 — replace the regime-concat with a learned 4-8 expert MoE layer.
**Risk**: expert collapse (router always picks one expert) — needs load-balancing loss. Adds router params + expert variance.

### B5. Self-Supervised Pretraining via Masked Autoencoder (MAE)
**What it is**: pretrain the candle encoder by randomly masking 75 % of the input candles and asking the model to reconstruct them. No label needed — uses all historical OHLCV.
**Status**: MAE exists for images / video / point clouds; the search found **no published crypto candlestick MAE** as of 2026. This is **net-new research surface** — opportunity for first-mover signal but no off-the-shelf code.
**Why it matters**: F48 pretrains supervised on direction/magnitude labels. MAE pretraining first → then supervised fine-tune is the modern recipe (BERT → MLM-then-fine-tune). Typically +3-7 % AUC at the same supervised data budget.
**Fit**: medium. Modest expected gain. Would extend the cont. 49 pretrainer pipeline with an MAE warm-up phase.
**Risk**: research-grade — we'd be implementing without a known crypto baseline. Time investment 2-4 weeks.

### B6. GNN beyond F24 — Evolving Multiscale GNN for Volatility
**What it is**: time-evolving graph where edges adapt window-by-window. Specifically for volatility / contagion forecasting.
**Paper**: [Forecasting cryptocurrency volatility: a novel framework based on the evolving multiscale graph neural network](https://link.springer.com/article/10.1186/s40854-025-00768-x) (2025).
**Vs our F24 GNN**: F24 (per blueprint) does static cross-pair correlation. The evolving multiscale version adapts the graph topology in real-time and learns multi-scale (1m/5m/15m/1h) graph features.
**Fit**: low-medium. F24 already exists; an upgrade is a refactor, not a new feature. Defer unless F24 metrics show plateau.

### B7. Multimodal Vision-LLMs reading the actual chart image
**What it is**: feed a screenshot of the chart to GPT-4o / Gemini / Claude vision and let it call out patterns in natural language.
**Verdict (2026)**: **DON'T** — multiple audits ([gist 2026](https://gist.github.com/roman-rr/c1cd675f7c35b68ae5ac281c30080166), [ChartSnipe](https://chartsnipe.com/blog/claude-vs-chatgpt-vs-chartsnipe-chart-analysis)) show frontier vision LLMs at 51 % direction accuracy (coin-flip), 1 / 215 patterns correctly identified, and severe long-bias on Gemini. Useful for narrative/explanation, not for P&L decisions. **Skip.**

---

## C. Ranked recommendations

| Rank | Feature | Lift est. | Build cost | Risk | Recommended order |
|---|---|---|---|---|---|
| 1 | **Mamba SSM (F50c)** | High — replaces TFT/PatchTST with linear-time long-context | 2–3 days | low (CryptoMamba GH ready) | ship first |
| 2 | **Chronos-2 foundation model warmup** | Medium — solves cold-start gap | 1–2 days | low (HF download + adapter) | ship second |
| 3 | **Diffolio diffusion sizing** | Medium-High — uncertainty-aware sizing closes loss-tail gap | 4–7 days | medium (training compute) | ship third |
| 4 | **MoE per-regime routing in F48** | Medium — speeds inference + finds sub-regimes | 5–7 days | medium (expert collapse) | defer |
| 5 | **MAE pretraining warm-up for CandleNet** | Low-medium — +3-7 % AUC on F48 | 1–2 weeks | medium (research-grade) | defer |
| 6 | **Evolving multiscale GNN upgrade to F24** | Low | 1–2 weeks | medium | defer |
| 7 | Vision LLM chart reading | ~zero / negative | irrelevant | high | **skip** |

---

## D. Confirmed vs Proposed

### Confirmed (already on disk — DO NOT re-spec)
- F24 (any GNN), F19 (Transformer 1h), F20 (Transformer 16h), F48 (CNN+TCN+GAF+regime-cond), F50a/b, F35 (RAG-style memory).
- The user-mandated 80 % floor in chop regimes (now regime-adaptive — cont. 53).

### Proposed (need user OK before code)
- **P5 — Mamba SSM (F50c)**: new `ml/mamba_forecaster.py`. Wraps CryptoMamba or vanilla state-spaces/mamba. Output: 1m/5m/15m/1h `dir1/dir3/dir5/mag1/mag3/mag5/trend` Redis keys parallel to F48. Composite score gets a new `mamba_bonus` slot. Activation gated by `paper_closed ≥ 500`. Fallback: degrades to F48 alone on load failure.
- **P6 — Chronos-2 zero-shot warmup**: new `ml/foundation_forecast.py`. Loads `amazon/chronos-bolt-base` from HuggingFace. Replaces the cold-start period (where F19/F20 are still pretraining) with zero-shot forecasts. Auto-disabled when `model:tft:trusted == "1"` (the existing trusted gate).
- **P7 — Diffolio probabilistic sizing**: new `ml/diffusion_forecaster.py` + risk-manager hook. Reads the diffusion forecast's 5%/95% quantile band and writes per-pair `:{pair}:tp_quantile_low`, `:{pair}:tp_quantile_high`. Then `risk/manager.py:size_position` reads these and divides the per-trade capital by `(q95 - q05) / atr` (wider band = smaller size).

### Deferred (lower priority or research-grade)
- **D3 — MoE F48 upgrade**: refactor of existing model; defer until F48 has its own trusted gate clear.
- **D4 — MAE candle pretraining**: research-grade; defer.
- **D5 — Evolving multiscale GNN**: incremental over F24; defer.

### Skipped
- **S1 — Vision LLM chart reading**: industry-confirmed unreliable for production.

---

## E. Open-source / paper references (saved per Rule 6)

- [MShahabSepehri/CryptoMamba](https://github.com/MShahabSepehri/CryptoMamba) — Bitcoin-specific SSM
- [automl/Mamba4Cast](https://github.com/automl/Mamba4Cast) — zero-shot forecasting
- [XiudingCai/MambaTS-pytorch](https://github.com/XiudingCai/MambaTS-pytorch) — long-term TS
- [state-spaces/mamba](https://github.com/state-spaces/mamba) — base architecture
- [arXiv:2501.01010 CryptoMamba paper](https://arxiv.org/abs/2501.01010)
- [Chronos-2 Amazon foundation model](https://github.com/amazon-science/chronos-forecasting)
- [TimesFM-2.5 Google](https://huggingface.co/google/timesfm-2.5-200m-pytorch)
- [Moirai-2 Salesforce](https://huggingface.co/Salesforce/moirai-2.0-R-small)
- [Lag-LLama ServiceNow](https://github.com/time-series-foundation-models/lag-llama)
- [arXiv:2511.18578 — ReVisiting TS Foundation Models in Finance 2026](https://arxiv.org/html/2511.18578v1)
- [Diffolio arXiv:2511.07014](https://arxiv.org/abs/2511.07014)
- [StochDiff arXiv:2406.02827](https://arxiv.org/pdf/2406.02827)
- [TimeGrad arXiv:2101.12072](https://arxiv.org/pdf/2101.12072)
- [Adaptive Market Intelligence MoE arXiv:2508.02686](https://arxiv.org/pdf/2508.02686)
- [Task-Aware MoE Time Series arXiv:2509.22279](https://arxiv.org/pdf/2509.22279)
- [LLM-Based Routing MoE Trading arXiv:2501.09636](https://arxiv.org/pdf/2501.09636)
- [Evolving Multiscale GNN Crypto Volatility](https://link.springer.com/article/10.1186/s40854-025-00768-x)
- [Visual Chart Representations arXiv:2605.00875](https://arxiv.org/abs/2605.00875) (already informs our F48 GAF)
- [Vision LLM trading audit gist](https://gist.github.com/roman-rr/c1cd675f7c35b68ae5ac281c30080166)
- [Computer Vision for Crypto Trading — Medium](https://medium.com/@gwrx2005/computer-vision-for-cryptocurrency-trading-chart-analysis-sentiment-and-anomaly-detection-870764fca604)

---

## F. Checklist (post user approval)

If user picks **P5 only** (most surgical):
- [ ] `requirements.txt` += `mamba-ssm>=2.2.0`, `causal-conv1d>=1.4.0`
- [ ] `ml/mamba_forecaster.py` — CryptoMamba wrapper, returns `dir/mag/trend` parallel to CandleNet keys
- [ ] `pretrainer/main.py` — add Mamba pretraining step (10–15 epochs, same data as CandleNet)
- [ ] `signals/engine.py` — read `mamba_bonus`, blend with `candlenet_bonus` (50/50 to start)
- [ ] `feature_governance/registry.py` — register F50c
- [ ] Dashboard panel for F50c health
- [ ] Blueprint: cont. 54 line for F50c
- [ ] Memory: no new mandate; F50c is additive

If user picks **P5 + P6** (cold-start solved):
- [ ] All of P5 above, plus:
- [ ] `requirements.txt` += `chronos-forecasting>=2.0`
- [ ] `ml/foundation_forecast.py` — Chronos wrapper, daily 1h forecast for top 30 pairs
- [ ] `risk/manager.py` — read Chronos confidence band when F19 untrusted
- [ ] Auto-disable on `model:tft:trusted == "1"`

If user picks **P5 + P6 + P7** (full upgrade):
- [ ] All of P5+P6, plus:
- [ ] `requirements.txt` += `diffolio` (or train from arXiv code if no PyPI)
- [ ] `ml/diffusion_forecaster.py` — Diffolio adapter writing `:{pair}:tp_quantile_low/high`
- [ ] `risk/manager.py:size_position` — divide capital by `(q95-q05)/atr` ratio
- [ ] Blueprint: cont. 54 covers F50c + F50g (foundation) + F50h (diffusion sizing)

---

## G. Decision points awaiting user

1. P5 alone, P5+P6, or P5+P6+P7?
2. Run Mamba on CPU (3-5× slower, no CUDA dependency) or require GPU (faster but new infra)?
3. Use CryptoMamba pretrained checkpoint (Bitcoin-only — needs fine-tune for alts) or train from scratch on our 60-pair corpus?

---

## H. SHIPPED — cont. 54 (2026-05-27)

User picked: **P1 (Mamba) + P2 (Chronos) + P5 (MAE) + P6 (Evolving Multiscale GNN)**. P3 (Diffolio diffusion) and P4 (MoE) deferred. P7 (Vision-LLM) skipped.

**Created**:
- ✅ `ml/mamba_forecaster.py` — F50c, pure-PyTorch selective scan, no CUDA dep
- ✅ `ml/foundation_forecast.py` — F50g, Chronos-Bolt wrapper, F19-trusted-deferred
- ✅ `ml/candlenet_mae.py` — F48§MAE, masked autoencoder pretraining warm-up
- ✅ `ml/gnn_multiscale.py` — F24M, multi-TF fused-correlation graph

**Wired**:
- ✅ signals/engine.py — three new additive bonuses (mamba ±10, foundation ±8, gnn_multiscale ±5) folded into direction_conf
- ✅ pretrainer/main.py — steps 11 (MAE), 12 (Mamba per-TF), 13 (Chronos download)
- ✅ requirements.txt — chronos-forecasting + huggingface_hub
- ✅ Blueprint cont. 54 changelog
- ✅ PROGRESS.md cont. 54 entry

**Pretrainer-pending**: model weights don't exist on disk yet. Until pretrainer runs steps 11/12/13:
- Mamba forecasts return None → mamba_bonus = 0 (cold-start safe)
- Chronos forecasts return None → foundation_bonus = 0 (cold-start safe)
- MAE warm-start unused by CandleNet → no change to existing CandleNet
- Multiscale GNN works (inference-only); doesn't depend on pretraining

**Activation order (after pretrainer)**:
1. Chronos useful immediately (zero-shot, just needs HF download).
2. Mamba useful after ~1.5 h pretrain per TF (6 h total for 4 TFs).
3. MAE warm-start requires follow-up wiring in `ml/candlenet.py:train` (1-2 h).
4. Multiscale GNN already live.

**File clears after pretrainer run + 24 h of telemetry confirming counters non-zero. Do not delete before then.**
