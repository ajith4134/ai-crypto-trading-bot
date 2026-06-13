"""SciBrain math/physics/quantum module bank.

Each module is one PhD-level concept that consumes a SensorFrame and emits a
ModuleOutput (the typed contract). Modules are pure compute (no Redis/IO) so they
are unit-testable in isolation and swappable. Register new modules in MODULES.

VS-1: KoopmanModule (physics — ergodic/DMD) + BOCPDModule.
"""
from __future__ import annotations

from .base import Module
from .bocpd import BOCPDModule
from .chaos import ChaosModule
from .ergodic_mixing import ErgodicMixingModule
from .evt_large_deviation_tail import EVTLargeDeviationTailModule
from .hmm_regime import HMMRegimeModule
from .multifractal_rg import MultifractalRGModule
from .info_theory import InfoTheoryModule
from .ising import MeanFieldIsingModule
from .kalman import KalmanModule
from .koopman import KoopmanModule
from .langevin_hawkes import LangevinHawkesModule
from .noiseharvest import NoiseHarvestModule
from .quantum import QuantumModule
from .rmt import RMTModule
from .rough_path_signature import RoughPathSignatureModule
from .statphys_soc import StatPhysSOCModule
from .tda import TDAModule
from .wavelet import WaveletSpectralModule

# The active bank, in deterministic order. Physics + quantum + stats, mixed disciplines.
MODULES: list[Module] = [
    KoopmanModule(),          # physics — Koopman/DMD (trend/regime)
    ChaosModule(),            # physics — nonlinear dynamics (Hurst: trend vs revert)
    StatPhysSOCModule(),      # physics — stat-mech / self-organized criticality (crash early-warning)
    MeanFieldIsingModule(),   # physics — Ising mean-field (herding/crowding order parameter)
    LangevinHawkesModule(),   # physics — Langevin drift/diffusion + Hawkes self-excitation
    RMTModule(),              # physics — Random Matrix Theory / SSA (signal-vs-noise denoise)
    NoiseHarvestModule(),     # stochastic — OU mean-reversion ("use noise to our advantage")
    KalmanModule(),           # estimation — fair-value + velocity (clean momentum)
    QuantumModule(),          # quantum — Von Neumann entropy + QFT cycle
    TDAModule(),              # topology — persistent homology (structural support/resistance)
    WaveletSpectralModule(),  # signal — multi-scale wavelet energy + noise floor
    InfoTheoryModule(),       # info-theory — permutation/Shannon entropy + volume->price TE
    HMMRegimeModule(),        # stats — Hidden Markov Model market-regime (reuses hmm_regime.pkl)
    BOCPDModule(),            # stats — change-point stability gate (non-directional)
    RoughPathSignatureModule(),  # rough-path — level-2 signature / Lévy area (event ORDER); SHADOW
    EVTLargeDeviationTailModule(),  # EVT — POT/GPD calibrated adverse-tail prob + risk envelope (RISK gate); SHADOW
    MultifractalRGModule(),     # multifractal — q-spectrum width + crossover + rough-vol (HORIZON gate); SHADOW
    ErgodicMixingModule(),      # ergodicity — mixing/half-life + permutation-entropy + EB (TRUST gate); SHADOW
]

__all__ = [
    "Module", "KoopmanModule", "ChaosModule", "StatPhysSOCModule",
    "MeanFieldIsingModule", "LangevinHawkesModule", "RMTModule",
    "NoiseHarvestModule", "KalmanModule", "QuantumModule", "TDAModule",
    "WaveletSpectralModule", "InfoTheoryModule", "HMMRegimeModule",
    "BOCPDModule", "RoughPathSignatureModule", "EVTLargeDeviationTailModule",
    "MultifractalRGModule", "ErgodicMixingModule", "MODULES",
]
