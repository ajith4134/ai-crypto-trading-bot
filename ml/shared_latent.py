"""SciBrain Phase-7e — the shared multimodal self-supervised LATENT CHALLENGER (design §3.2 / §9 step 5).

"Turn raw market streams into useful representations before asking for a trade label." This is the
sensory-cortex/association challenger: a single bounded shared latent assembled from the EXISTING expert
encoders' outputs (the CandleNet-MAE forecasts + the 14-module PhD bank + regime + belief state — the
modalities §3.2 lists), with the self-supervised objective HEADS in place:

  • a masked-reconstruction decoder      (L_mask)
  • a JEPA-style future-latent predictor  (L_latent)

This module is TASK-1 of Phase 7e: the ARCHITECTURE + the feature assembly + a working forward pass that
produces a real shared latent from real inputs. It is UNTRAINED (random init) — training the objectives
(masked / contrastive / cross-modal / future-latent) and proving downstream utility / no leakage is the
separate step-2 task. It is a SHADOW CHALLENGER: it carries NO action authority and is never on the live
trade path; representation promotion is judged later by downstream forecast/control utility, not
reconstruction beauty. Information-bottleneck: the latent is deliberately low-dim.
"""
from __future__ import annotations

import numpy as np

try:
    import torch
    import torch.nn as nn
    _HAS_TORCH = True
except Exception:  # torch absent (e.g. a lint host) — assembly still works; the nn.Module is skipped
    _HAS_TORCH = False
    nn = object  # type: ignore

# ── stable modality layout (so the input is fixed-dim regardless of which modules fired) ──
CANON_ROLES = ("direction", "context", "gate", "risk", "allocator", "exit", "meta", "evidence",
               "execution", "selection")
CANON_REGIMES = ("trending", "mean_revert", "turbulent", "neutral")
_ROLE_STATS = 4                                  # [signed_vote_sum, conviction_mean, count_norm, dispersion]
MODULES_DIM = len(CANON_ROLES) * _ROLE_STATS     # 40
REGIME_DIM = len(CANON_REGIMES)                  # 4
BELIEF_DIM = 5                                   # [disagreement, changepoint, dispersion, mean_conv, n_conv_norm]
INPUT_DIM = MODULES_DIM + REGIME_DIM + BELIEF_DIM   # 49
LATENT_DIM = 24
_HID = 16


def assemble_modalities(decision: dict, belief: dict | None = None) -> dict:
    """Build the fixed-dim multimodal input from a live Decision (+ optional BeliefState). Pure numpy.
    Returns {'modules': (40,), 'regime': (4,), 'belief': (5,)} float32 arrays — the existing encoders'
    outputs re-expressed as a stable multimodal feature set."""
    mods = (decision or {}).get("modules") or []
    # per-role aggregates
    by_role: dict[str, list] = {r: [] for r in CANON_ROLES}
    for m in mods:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "direction")
        if role not in by_role:
            role = "meta"
        try:
            d = float(m.get("direction", 0.0) or 0.0)
            conv = float(m.get("conviction", 0.0) or 0.0)
        except (TypeError, ValueError):
            d, conv = 0.0, 0.0
        if m.get("ok", True):
            by_role[role].append((d, conv))
    mod_vec = []
    for r in CANON_ROLES:
        votes = by_role[r]
        if votes:
            signed = [d * c for d, c in votes]
            convs = [c for _, c in votes]
            mod_vec += [float(np.sum(signed)), float(np.mean(convs)),
                        float(min(1.0, len(votes) / 8.0)),
                        float(np.std(signed)) if len(signed) >= 2 else 0.0]
        else:
            mod_vec += [0.0, 0.0, 0.0, 0.0]

    # regime one-hot from the decision's current regime
    reg = str((decision or {}).get("regime", "neutral"))
    reg_vec = [1.0 if reg == rr else 0.0 for rr in CANON_REGIMES]
    if sum(reg_vec) == 0:
        reg_vec = [0.0, 0.0, 0.0, 1.0]               # default neutral

    b = belief or {}
    ud = b.get("uncertainty_decomposition") or {}
    convicted = [float(m.get("conviction", 0.0) or 0.0) for m in mods
                 if isinstance(m, dict) and m.get("ok", True) and float(m.get("conviction", 0.0) or 0.0) > 0]
    belief_vec = [
        float(b.get("disagreement", 0.0) or 0.0),
        float(b.get("changepoint_posterior", 0.0) or 0.0),
        float(b.get("latent_dispersion", 0.0) or 0.0),
        float(np.mean(convicted)) if convicted else 0.0,
        float(min(1.0, len(convicted) / 14.0)),
    ]
    return {
        "modules": np.asarray(mod_vec, dtype=np.float32),
        "regime": np.asarray(reg_vec, dtype=np.float32),
        "belief": np.asarray(belief_vec, dtype=np.float32),
    }


if _HAS_TORCH:
    class SharedLatentChallenger(nn.Module):
        """Multimodal self-supervised encoder over the existing experts' outputs. Per-modality input
        projections → a fused, bottlenecked shared latent; plus a masked-reconstruction decoder and a
        JEPA future-latent predictor head (the SSL objectives' heads). UNTRAINED until step-2; SHADOW
        only — no action authority. forward() returns the latent + the head outputs.

        `modality_dims` is dim-parametric so the SAME architecture serves the LIVE role-aggregated input
        (default: modules40/regime4/belief5) AND the HISTORICAL module_vec corpus (e.g. modules18/regime4/
        flow5) used by ml/train_shared_latent.py. Modalities are concatenated in a FIXED key order."""

        def __init__(self, modality_dims: dict | None = None, latent_dim: int = LATENT_DIM,
                     hidden: int = _HID):
            super().__init__()
            self.modality_dims = dict(modality_dims or
                                      {"modules": MODULES_DIM, "regime": REGIME_DIM, "belief": BELIEF_DIM})
            self.keys = list(self.modality_dims.keys())          # FIXED concat order
            self.latent_dim = latent_dim
            self.input_dim = int(sum(self.modality_dims.values()))
            # prefix keys: 'modules' etc. would collide with nn.Module's reserved attrs in a ModuleDict.
            self.encoders = nn.ModuleDict(
                {f"enc_{k}": nn.Sequential(nn.Linear(d, hidden), nn.GELU())
                 for k, d in self.modality_dims.items()})
            self.fuse = nn.Sequential(nn.Linear(len(self.keys) * hidden, latent_dim), nn.LayerNorm(latent_dim))
            self.mask_decoder = nn.Linear(latent_dim, self.input_dim)          # masked reconstruction
            self.jepa_predictor = nn.Sequential(nn.Linear(latent_dim, latent_dim), nn.GELU(),
                                                nn.Linear(latent_dim, latent_dim))  # future-latent prediction

        @staticmethod
        def _t(x):
            t = x if torch.is_tensor(x) else torch.as_tensor(np.asarray(x, dtype=np.float32))
            return t.unsqueeze(0) if t.dim() == 1 else t

        def encode(self, modalities: dict) -> "torch.Tensor":
            """Shared latent only (the representation). modalities = dict of tensors or arrays."""
            zs = [self.encoders[f"enc_{k}"](self._t(modalities[k])) for k in self.keys]
            return self.fuse(torch.cat(zs, dim=-1))

        def forward(self, modalities: dict, mask: "torch.Tensor | None" = None) -> dict:
            """Full SSL forward: latent + masked reconstruction + JEPA predicted next-latent. `mask` (a
            0/1 vector over input_dim) zeroes inputs before encoding so the decoder learns to fill them."""
            raw = torch.cat([self._t(modalities[k]) for k in self.keys], dim=-1)
            x = raw if mask is None else raw * self._t(mask)
            parts = torch.split(x, [self.modality_dims[k] for k in self.keys], dim=-1)
            zs = [self.encoders[f"enc_{k}"](p) for k, p in zip(self.keys, parts)]
            z = self.fuse(torch.cat(zs, dim=-1))
            return {"latent": z, "recon": self.mask_decoder(z), "jepa_pred": self.jepa_predictor(z),
                    "target_input": raw}

        def n_params(self) -> int:
            return sum(p.numel() for p in self.parameters())


def build_latent(decision: dict, belief: dict | None = None, model: "SharedLatentChallenger | None" = None):
    """Convenience: assemble modalities from a Decision and run encode() → a shared latent (numpy).
    Read-only; returns {'latent': list, 'dim', 'n_params', 'input_dim'} or {'error'} if torch absent."""
    if not _HAS_TORCH:
        return {"error": "torch unavailable", "available": False}
    mods = assemble_modalities(decision, belief)
    m = model or SharedLatentChallenger()
    m.eval()
    with torch.no_grad():
        z = m.encode(mods).squeeze(0).cpu().numpy()
    return {"available": True, "latent": [round(float(v), 5) for v in z], "dim": int(z.shape[0]),
            "n_params": m.n_params(), "input_dim": INPUT_DIM,
            "modalities": {k: int(v.shape[0]) for k, v in mods.items()}}
