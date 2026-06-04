"""
Shared model class definitions — used by BOTH pretrainer and inference.

Why this file exists:
- pretrainer/main.py originally defined model classes at module level and called
  torch.save(model, ...). This pickled the model as `__main__.TFTModel` etc.
- At inference time, __main__ is brain/main.py or celery/bin/celery, not the
  pretrainer. Pickle cannot find `__main__.TFTModel` and torch.load fails.

The fix:
- All 4 model classes live here.
- pretrainer imports them and saves state_dict only.
- Inference imports the classes, instantiates fresh, loads state_dict.
- For backward-compatibility with EXISTING .pth files (saved via torch.save(model)),
  inference also injects these classes into sys.modules['__main__'] before
  torch.load so the legacy pickle stream can resolve __main__.ClassName.

Blueprint reference: F19 (TFT), F20 (PatchTST), F24 (GNN), F34 (World Model).
These are Stage 1 placeholder architectures. Issue #2 in the audit covers
upgrading them to the real research-paper implementations (pytorch-forecasting,
torch-geometric, dreamerv3-torch).
"""
import torch
import torch.nn as nn


class GatedResidualNetwork(nn.Module):
    """TFT building block — Gated Residual Network (Lim et al. Section 4.1).

    GRN(x) = LayerNorm( ELU(W1·x) -> W2 -> Dropout + GLU(skip(x)) + skip(x) )

    The GLU lets the model gate (zero out) the transformation when it's not
    useful for the prediction. Residual + LayerNorm stabilize deep stacks.
    """
    def __init__(self, input_dim: int, hidden_dim: int, output_dim: int | None = None,
                 dropout: float = 0.1):
        super().__init__()
        output_dim = output_dim or input_dim
        self.fc1  = nn.Linear(input_dim, hidden_dim)
        self.fc2  = nn.Linear(hidden_dim, output_dim)
        self.gate = nn.Linear(input_dim, 2 * output_dim)  # GLU splits into a, b
        self.skip = nn.Linear(input_dim, output_dim) if input_dim != output_dim else nn.Identity()
        self.norm = nn.LayerNorm(output_dim)
        self.drop = nn.Dropout(dropout)

    def forward(self, x):
        import torch.nn.functional as F
        h = self.fc2(F.elu(self.fc1(x)))
        h = self.drop(h)
        gated = F.glu(self.gate(x), dim=-1)
        return self.norm(h + gated + self.skip(x))


class TFTModel(nn.Module):
    """F19 — Temporal Fusion Transformer for single-feature price forecasting.

    Blueprint citation: Lim et al., "Temporal Fusion Transformers for
    Interpretable Multi-horizon Time Series Forecasting" (Google, 2021).

    Components present (per Lim et al. Sections 4.1–4.4):
      - Input embedding   : Linear projection of scalar close-price to hidden_dim
      - LSTM encoder      : local temporal dynamics (2-layer, dropout 0.1)
      - GRN(post-LSTM)    : gated residual processing of encoder output
      - Multi-Head Self-Attention with CAUSAL mask: long-range dependencies, no leakage
      - GRN(post-attention): gated residual processing of attention output, with skip
      - Quantile head     : Linear → 3 outputs (q10, q50, q90)

    Components OMITTED vs full Lim et al. (and documented per Rule 4 honesty):
      - Variable Selection Network — we have 1 input variable (close price), VSN is trivial
      - Static + temporal covariate split — no static (e.g., pair-id) or known-future inputs
      - Multi-horizon output — current consumer (signals/engine.py) only reads 1-step ahead;
        could extend to multi-horizon by widening the quantile head and unflattening
      - Cross-attention between encoder and decoder — replaced with simpler self-attention
        since we're not doing multi-step decoder rollout

    Input shape:  [B, T, 1]   — batch, time, 1 close price feature
    Output shape: [B, 3]      — q10, q50, q90 of next price (relative to input[:, 0, 0])

    NORMALIZATION CONTRACT: caller MUST divide input by input[:, 0, :] before passing,
    and multiply output by input[:, 0, :] to get back to absolute prices. See
    ml/tft.py:get_price_forecast for the inference-side normalization, and
    pretrainer/main.py:train_tft for the training-side normalization. Both MUST match.
    This is what makes the model scale-invariant across pairs (BTC at $50k and PEPE
    at $0.00001 produce identical relative-price inputs).
    """
    HIDDEN_DIM    = 64
    LSTM_LAYERS   = 2
    LSTM_DROPOUT  = 0.1
    N_ATTN_HEADS  = 4
    N_QUANTILES   = 3   # q10, q50, q90

    def __init__(self):
        super().__init__()
        self.input_proj      = nn.Linear(1, self.HIDDEN_DIM)
        self.lstm            = nn.LSTM(
            input_size=self.HIDDEN_DIM,
            hidden_size=self.HIDDEN_DIM,
            num_layers=self.LSTM_LAYERS,
            batch_first=True,
            dropout=self.LSTM_DROPOUT,
        )
        self.post_lstm_grn   = GatedResidualNetwork(self.HIDDEN_DIM, self.HIDDEN_DIM)
        self.attention       = nn.MultiheadAttention(
            embed_dim=self.HIDDEN_DIM,
            num_heads=self.N_ATTN_HEADS,
            batch_first=True,
            dropout=0.1,
        )
        self.post_attn_grn   = GatedResidualNetwork(self.HIDDEN_DIM, self.HIDDEN_DIM)
        self.quantile_head   = nn.Linear(self.HIDDEN_DIM, self.N_QUANTILES)

    def forward(self, x):
        # x: [B, T, 1] — normalized relative prices
        h = self.input_proj(x)                  # [B, T, H]
        lstm_out, _ = self.lstm(h)              # [B, T, H]
        h = self.post_lstm_grn(lstm_out)        # [B, T, H]

        # Causal self-attention (prevents leakage from future timesteps)
        T = h.shape[1]
        causal = torch.triu(torch.ones(T, T, dtype=torch.bool, device=h.device), diagonal=1)
        attn_out, _ = self.attention(h, h, h, attn_mask=causal, need_weights=False)
        h = self.post_attn_grn(attn_out + h)    # [B, T, H] — residual + GRN

        # Quantile prediction from the final timestep
        return self.quantile_head(h[:, -1, :])  # [B, 3] — q10, q50, q90 (relative)


def tft_quantile_loss(pred, target, quantiles=(0.1, 0.5, 0.9)):
    """Pinball/quantile loss for training TFTModel.

    pred:    [B, len(quantiles)]  — model output (relative-price quantile predictions)
    target:  [B, 1]               — actual next relative price
    Returns scalar mean loss across batch and quantiles.

    Pinball loss for quantile q: max(q*(y-ŷ), (q-1)*(y-ŷ))
    Sum over quantiles, mean over batch.
    """
    import torch as _torch
    losses = []
    for i, q in enumerate(quantiles):
        e = target.squeeze(-1) - pred[:, i]
        losses.append(_torch.maximum(q * e, (q - 1) * e))
    return _torch.stack(losses).mean()


class PatchTSTModel(nn.Module):
    """F20 — Real PatchTST (Nie et al. ICLR 2023) via HuggingFace transformers.

    Blueprint citation: "A Time Series is Worth 64 Words" (Nie et al., ICLR 2023).
    Key innovations vs prior approaches:
      - PATCHING: split time series into non-overlapping patches; treat each as a token.
        Reduces sequence length by patch_length, dropping memory by 8-16×.
      - CHANNEL INDEPENDENCE: each channel (here: close price) is modeled separately.
        Avoids spurious cross-feature correlations.
      - REVERSIBLE INSTANCE NORMALIZATION (RevIN): applied internally by HF impl.

    Architecture (smaller-than-default HF config to keep training feasible):
      - context_length     : 256  (about 10 days of 1h candles — long-horizon view)
      - patch_length       : 16   (16 patches per sequence)
      - num_input_channels : 1    (close price only — single-channel model per blueprint)
      - prediction_length  : 16   (forecast next 16 hours)
      - d_model            : 64
      - num_attention_heads: 4
      - num_hidden_layers  : 3

    Why both TFT (F19) and PatchTST (F20):
      - TFT: 100-step lookback, 1-step quantile forecast — short horizon, with uncertainty
      - PatchTST: 256-step lookback, 16-step horizon — long-range pattern detection
    They feed independent signals into signals/engine.py direction_conf computation.

    Forward signature: forward(x) where x has shape [B, T, 1] with T=256.
    Returns:           [B, prediction_length] of predicted next-N relative prices.
    """
    CONTEXT_LENGTH    = 256
    PATCH_LENGTH      = 16
    PATCH_STRIDE      = 16
    NUM_CHANNELS      = 1
    PREDICTION_LENGTH = 16
    D_MODEL           = 64
    N_HEADS           = 4
    N_LAYERS          = 3

    def __init__(self):
        super().__init__()
        from transformers import PatchTSTConfig, PatchTSTForPrediction
        cfg = PatchTSTConfig(
            num_input_channels=self.NUM_CHANNELS,
            context_length=self.CONTEXT_LENGTH,
            patch_length=self.PATCH_LENGTH,
            patch_stride=self.PATCH_STRIDE,
            prediction_length=self.PREDICTION_LENGTH,
            d_model=self.D_MODEL,
            num_attention_heads=self.N_HEADS,
            num_hidden_layers=self.N_LAYERS,
            ffn_dim=128,
            scaling="std",          # internal standardization (RevIN-like)
            loss="mse",
        )
        self.model = PatchTSTForPrediction(cfg)

    def forward(self, x):
        """
        x: [B, T, NUM_CHANNELS=1] — normalized relative-price sequence of length 256.
        Returns: [B, prediction_length] — predicted relative prices for next 16 hours.
        Strips the channel dim from output for downstream simplicity.
        """
        out = self.model(past_values=x)
        # PatchTSTForPrediction output: prediction_outputs with shape [B, pred_len, channels]
        return out.prediction_outputs.squeeze(-1)  # [B, 16]


class GNNModel(nn.Module):
    """F24 — Graph Attention Network for inter-asset correlation.

    Blueprint citation: Feature 24 specifies message passing with edge weights
    (formula `h_v^(l+1) = σ(W·Σ h_u/√(deg(v)·deg(u)))`) — current architecture
    must be a real graph network, not a Linear projection.

    Architecture: 2-layer Graph Attention Network using torch_geometric's GATConv.
      - Layer 1 : GATConv(IN_FEATURES → HIDDEN_DIM × N_HEADS) with concat=True
                  → produces HIDDEN_DIM*N_HEADS-dim node embedding
      - ReLU
      - Layer 2 : GATConv(HIDDEN_DIM*N_HEADS → OUT_DIM) with heads=1, concat=False
                  → produces OUT_DIM-dim final node embedding

    Input format per torch_geometric convention:
      - x          : [N_NODES, IN_FEATURES] node feature matrix
      - edge_index : [2, N_EDGES] long tensor of source/target node indices
      - edge_attr  : (optional) [N_EDGES] edge weights — used as attention bias

    Node features (4-dim):
      0. price_change_5m   — short-term momentum
      1. price_change_1h   — medium-term momentum
      2. volume_24h_log    — log-scaled liquidity
      3. realized_vol      — recent realized volatility

    Edges:
      Built dynamically per call from price-history correlation. Pairs with |corr| > 0.5
      become connected. Edge weights = |correlation|. This is the "Evolving Multiscale
      GNN" property — graph structure is recomputed each tick.

    Component used vs blueprint:
      - GATConv implements the attention-weighted message passing per the formula
      - Multi-head attention with concat in layer 1 = "multiscale" feature extraction
      - Dynamic graph construction in caller satisfies "evolving" requirement
      - Leader-follower extraction in caller satisfies "lead-lag" interpretation
    """
    IN_FEATURES   = 4
    HIDDEN_DIM    = 32
    OUT_DIM       = 16
    N_HEADS       = 4
    DROPOUT       = 0.1

    def __init__(self):
        super().__init__()
        from torch_geometric.nn import GATConv
        self.conv1 = GATConv(
            self.IN_FEATURES, self.HIDDEN_DIM,
            heads=self.N_HEADS, concat=True, dropout=self.DROPOUT,
        )
        self.conv2 = GATConv(
            self.HIDDEN_DIM * self.N_HEADS, self.OUT_DIM,
            heads=1, concat=False, dropout=self.DROPOUT,
        )

    def forward(self, x, edge_index, edge_attr=None):
        # edge_attr is accepted in the signature but not used by GATConv directly —
        # attention coefficients are learned from node features. We use edge_attr
        # only at graph-construction time (to decide which edges to include).
        h = self.conv1(x, edge_index)
        h = torch.relu(h)
        h = self.conv2(h, edge_index)
        return h


class WorldModelBundle(nn.Module):
    """F34: DreamerV3-inspired Recurrent State-Space Model (RSSM).

    Blueprint citation: Hafner et al. "Mastering Diverse Domains through World Models"
    (Nature 2025 / arXiv:2301.04104). Audit Issue #2 called out that the previous
    implementation was 3-layer MLPs for each of encoder/transition/reward — not an
    RSSM. This is the proper architecture.

    State representation: (deter, stoch)
      - deter [DETER_DIM=64]: deterministic GRU hidden state (the "h")
      - stoch [STOCH_DIM=32]: stochastic latent sample (the "z"), Gaussian
      - full latent for downstream code = cat(deter, stoch) → LATENT_DIM=96

    Components:
      - Encoder            : obs[OBS_DIM=64] → embed[EMBED_DIM=64]
      - Action embedding   : action one-hot[4] → embedded[16]
      - Recurrent core     : GRUCell on (prev_stoch + action_embed) → new_deter
      - Prior network      : deter → Gaussian(mean, log_std) of stoch     (imagined step)
      - Posterior network  : (deter, embed) → Gaussian of stoch           (observed step)
      - Reward head        : (deter, stoch) → symlog-space scalar reward
      - Continue head      : (deter, stoch) → P(episode continues)        (used for early termination)

    Symlog reward transform (per DreamerV3): sign(x) * log(1 + |x|) stabilises learning
    across reward magnitudes. The reward head outputs in symlog space; symexp converts
    back to natural reward space.

    Online-learning scope (per memory/write.py): only the REWARD HEAD is trained from
    actual_pnl at trade close. Encoder/GRU/prior/posterior need either supervised
    next-state targets (we don't capture them yet) or full DreamerV3-style imagined
    rollout loss with KL balancing — explicit follow-up.
    """
    OBS_DIM          = 64
    EMBED_DIM        = 64
    DETER_DIM        = 64
    STOCH_DIM        = 32
    ACTION_DIM       = 4    # one-hot: open_long / open_short / hold / close
    ACTION_EMBED_DIM = 16
    LATENT_DIM       = DETER_DIM + STOCH_DIM   # 96 — what external callers see

    def __init__(self):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(self.OBS_DIM, 128),
            nn.ELU(),
            nn.Linear(128, self.EMBED_DIM),
        )
        self.action_embed = nn.Linear(self.ACTION_DIM, self.ACTION_EMBED_DIM)
        self.gru = nn.GRUCell(
            input_size=self.STOCH_DIM + self.ACTION_EMBED_DIM,
            hidden_size=self.DETER_DIM,
        )
        self.prior = nn.Sequential(
            nn.Linear(self.DETER_DIM, 64),
            nn.ELU(),
            nn.Linear(64, 2 * self.STOCH_DIM),  # mean + log_std
        )
        self.posterior = nn.Sequential(
            nn.Linear(self.DETER_DIM + self.EMBED_DIM, 64),
            nn.ELU(),
            nn.Linear(64, 2 * self.STOCH_DIM),
        )
        self.reward = nn.Sequential(
            nn.Linear(self.DETER_DIM + self.STOCH_DIM, 64),
            nn.ELU(),
            nn.Linear(64, 1),
        )
        self.continue_head = nn.Sequential(
            nn.Linear(self.DETER_DIM + self.STOCH_DIM, 32),
            nn.ELU(),
            nn.Linear(32, 1),
        )

    # ── Static helpers ───────────────────────────────────────────────────────
    @staticmethod
    def symlog(x):
        return torch.sign(x) * torch.log1p(torch.abs(x))

    @staticmethod
    def symexp(x):
        return torch.sign(x) * (torch.expm1(torch.abs(x)))

    # ── State helpers ────────────────────────────────────────────────────────
    def initial_state(self, batch: int = 1):
        """Zero-initialised (deter, stoch) for a fresh trajectory."""
        return (
            torch.zeros(batch, self.DETER_DIM),
            torch.zeros(batch, self.STOCH_DIM),
        )

    # ── Sub-step ops ─────────────────────────────────────────────────────────
    def imagine_step(self, prev_deter, prev_stoch, action_onehot):
        """One imagined step (no observation). Uses prior.
        Returns (new_deter, new_stoch_sample, prior_mean, prior_std).
        """
        ae = self.action_embed(action_onehot)
        new_deter = self.gru(torch.cat([prev_stoch, ae], dim=-1), prev_deter)
        prior_params = self.prior(new_deter)
        prior_mean, prior_log_std = prior_params.chunk(2, dim=-1)
        prior_std = torch.exp(prior_log_std.clamp(-5.0, 2.0))
        new_stoch = prior_mean + prior_std * torch.randn_like(prior_std)
        return new_deter, new_stoch, prior_mean, prior_std

    def observe_step(self, prev_deter, prev_stoch, action_onehot, embed):
        """One observed step. Uses posterior conditioned on encoder embedding.
        Returns (new_deter, new_stoch_sample, prior_m, prior_s, post_m, post_s).
        """
        ae = self.action_embed(action_onehot)
        new_deter = self.gru(torch.cat([prev_stoch, ae], dim=-1), prev_deter)
        prior_params = self.prior(new_deter)
        prior_mean, prior_log_std = prior_params.chunk(2, dim=-1)
        prior_std = torch.exp(prior_log_std.clamp(-5.0, 2.0))
        post_params = self.posterior(torch.cat([new_deter, embed], dim=-1))
        post_mean, post_log_std = post_params.chunk(2, dim=-1)
        post_std = torch.exp(post_log_std.clamp(-5.0, 2.0))
        new_stoch = post_mean + post_std * torch.randn_like(post_std)
        return new_deter, new_stoch, prior_mean, prior_std, post_mean, post_std

    def reward_from_state(self, deter, stoch):
        """Predict reward in symlog space, return natural-scale reward.
        Clamp symlog_r before symexp — random/large symlog values would overflow
        (symexp(20) ≈ 5e8, symexp(30) → inf for float32). Clamping to ±10 bounds
        the natural reward to ±22026 which is huge but finite, so downstream
        arithmetic (errors, means, std) stays numerically valid."""
        full = torch.cat([deter, stoch], dim=-1)
        symlog_r = self.reward(full).clamp(-10.0, 10.0)
        return self.symexp(symlog_r)

    def continue_from_state(self, deter, stoch):
        full = torch.cat([deter, stoch], dim=-1)
        return torch.sigmoid(self.continue_head(full))

    def forward(self, x):
        """Backward-compat: a plain forward through the encoder. Kept so the
        existing world_model.encode() path which used `_encoder(x)` still works
        if a caller treats the bundle itself as the encoder."""
        return self.encoder(x)


def inject_into_main():
    """
    Make these classes resolvable via __main__.ClassName so torch.load() can
    deserialize legacy .pth files saved by pretrainer running as __main__.

    Call this BEFORE torch.load() in any inference module that needs to load
    one of the legacy pre-trained model files.
    """
    import sys
    main_mod = sys.modules.get("__main__")
    if main_mod is None:
        return
    main_mod.TFTModel = TFTModel
    main_mod.PatchTSTModel = PatchTSTModel
    main_mod.GNNModel = GNNModel
    main_mod.WorldModelBundle = WorldModelBundle
