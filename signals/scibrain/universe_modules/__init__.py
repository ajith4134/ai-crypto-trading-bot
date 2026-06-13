"""SciBrain Universe-Core module bank — cross-market modules that consume the shared
read-only `UniverseFrame` (built once per cycle) instead of a per-symbol `SensorFrame`.

Design §4a: most of the 14 hot modules inspect ONE symbol at a time, so they can't see
market-wide factor flow, directed contagion, or crowding. A Universe module computes ONE
cross-market object from the `UniverseFrame`, then emits standard per-symbol `ModuleOutput`
objects — preserving the single interface contract (fusion never learns it's cross-market).

Admission (§6g + Rule 14): a brand-new directional component enters as `shadow_only=True`
(RECORDED + IC-evaluable, NEVER applied to a live vote) until it proves incremental
out-of-sample IC conditional on the existing bank. Promotion to a live vote is a separate
owner-approved step.

VS-9: SparseFactorResidual (robust low-rank+sparse market/sector/idiosyncratic decomposition).
"""
from __future__ import annotations

from .base import UniverseModule
from .causal_lead_lag import CausalLeadLagModule
from .optimal_transport_regime import OptimalTransportRegimeModule
from .sparse_factor_residual import SparseFactorResidualModule
from .spectral_graph_contagion import SpectralGraphContagionModule

# The active Universe-Core bank, in deterministic order.
UNIVERSE_MODULES: list[UniverseModule] = [
    SparseFactorResidualModule(),       # cross-asset — robust PCA residual reversal (idiosyncratic alpha)
    SpectralGraphContagionModule(),     # contagion — spectral graph: leader→follower shock diffusion
    CausalLeadLagModule(),              # causal_flow — sparse conditional Granger lag graph (stability-selected)
    OptimalTransportRegimeModule(),     # distribution_transport — sliced-Wasserstein regime + outcome winner/loser
]

__all__ = ["UniverseModule", "SparseFactorResidualModule",
           "SpectralGraphContagionModule", "CausalLeadLagModule",
           "OptimalTransportRegimeModule", "UNIVERSE_MODULES"]
