"""
Feature Governance Bootstrap — registers every feature in the system.

Blueprint Feature 30 — Universal Future-Proof Rule:
"Every feature ever added to this system — now or in the future — is automatically
subject to Feature Governance. No feature can be marked as exempt."

This module is called once at brain startup. It populates the in-memory
_REGISTRY in feature_governance.registry so update_contribution() and
is_active() work for every feature.

To add a new feature: append a tuple to FEATURES below.
"""
import structlog
from feature_governance.registry import register

log = structlog.get_logger()


# (feature_id, human_name, activation_phase)
# Phase: 0=Day 0, 1=50-100 trades, 2=100 trades, 3=300 trades, 4=500-800 trades
FEATURES = [
    # === Trading mechanics — Day 0 ===
    ("F2",   "Trade Memory Pattern Mining",     0),
    ("F4",   "Dynamic Trailing SL",             0),
    ("F5",   "DCA Loss Recovery",               0),
    ("F9",   "Rejected Signal Scanner",         0),
    ("F10",  "Pair Scanner",                    0),
    ("F13",  "Direction Prediction Model",      0),
    # === Microstructure / regime — Day 0 ===
    ("F14",  "HMM Regime Detection",            0),
    ("F15",  "Market Microstructure VPIN/OFI",  0),
    ("F26",  "BOCPD Changepoint Detection",     0),
    ("F27",  "Transfer Entropy Lead-Lag",       0),
    ("F28",  "Turbulence Index",                0),
    # === ML forecasts — Day 0 pre-trained ===
    ("F19",  "TFT Forecaster",                  0),
    ("F20",  "PatchTST Forecaster",             0),
    ("F23",  "Mutual Information Ranking",      0),
    ("F24",  "GNN Inter-Asset Correlation",     0),
    ("F34",  "Market World Model",              0),
    # === Sentiment — Day 0 ===
    ("F18",  "Fear & Greed Sentiment",          0),
    ("F29",  "Web Intelligence",                0),
    # === Memory ===
    ("F35",  "MemRL Quality-Weighted Memory",   0),
    # === Phase 1 (50-100 trades) ===
    ("F16",  "Fractional Kelly",                1),
    ("F25",  "Genetic Algorithm",               1),
    # === Phase 2 (100 trades) ===
    ("F36",  "Strategy Research Engine",        2),
    ("F43",  "Metacognitive Monitor",           2),
    # === Phase 3 (300 trades) ===
    ("F17",  "Continual Learning EWC",          3),
    ("F21",  "Hierarchical MARL",               3),
    ("F37",  "Multi-Agent Debate Council",      3),
    # === Phase 4 (500-800 trades) ===
    ("F22",  "Meta-RL MAML",                    4),
    # === Infrastructure — Day 0 (not opt-out-able) ===
    ("F31",  "Performance Analytics",           0),
    ("F32",  "Account Risk Monitor",            0),
    ("F33",  "Self-Healing Watchdog",           0),
    # === Self-improvement — Day 0 ===
    ("F38",  "Curiosity Engine",                0),
    ("F39A", "OPRO Prompt Optimization",        0),
    ("F40",  "Local LLM Layer",                 0),
    ("F41",  "Self-Play vs MarS",               0),
    ("F42",  "SOAR Cognitive Loop",             0),
    # === Hedge (Stage 3+) ===
    ("F44",  "Directional Hedge",               3),
    # === Cross-Sectional Momentum (Liu-Tsyvinski 2022, Stage 2+) ===
    ("F45",  "Cross-Sectional Momentum",        2),
    # === F9/F12 Decoder Writeback (Stage 2+, cont. 27) ===
    # Closes Blueprint §10.7 "Filter Improvement Loop" — postmortem LLM
    # output now writes back to filter/scorer overrides via bounded actuator.
    # F30 governance can deactivate to freeze drift if outcomes regress.
    ("F46",  "Decoder Writeback Actuator",      2),
    # === F47 — Learned Trailing-SL Ratchet (Stage 1+, cont. 30) ===
    # Blueprint §10.4: "Brain adjusts trailing distance in real time ...
    # locking in gains progressively." Replaces hand-picked 0.40 lock_frac
    # floor with constant-α MC + exploration learner (risk/trail_params.py).
    ("F47",  "Learned Trailing Ratchet",        1),
    # === F48 — CandleNet 1min/5min Next-Candle Prediction (Day 0 pre-trained) ===
    # Blueprint Feature 48. Two separate governance IDs so each timeframe model
    # can be deactivated independently if one underperforms.
    ("F48_1m",  "CandleNet 1min Model",           0),
    ("F48_5m",  "CandleNet 5min Model",           0),
    # F48 §Idea D (cont. 46) — 4-TF hierarchy. Third CandleNet timeframe
    # used in signals/engine.py for the +25 confluence bonus when all three
    # TFs agree.
    ("F48_15m", "CandleNet 15min Model",          0),
    # cont. 65d — 30m closes the 15m→1h gap in the cascade hierarchy.
    # Same inference + retrain machinery as the other TFs.
    ("F48_30m", "CandleNet 30min Model",          0),
    # cont. 65 — 1h CandleNet completes the cascade's 1h candle vote
    # (previously only TFT 1h bias). Inference task includes 1h in the
    # per-tick loop; model file optional (silent cold-start fallback).
    ("F48_1h",  "CandleNet 1h Model",             0),
    # F49 (cont. 47, 2026-05-25) — Autonomous Self-Training Orchestrator.
    # Closed-loop drift+perf+orchestrator system. Day 0 / infrastructure-tier
    # but can be deactivated to fall back to weekly-cron-only retrains.
    ("F49",    "Autonomous Self-Training",        0),
    # F51 bundle (cont. 51, 2026-05-27) — Research-driven improvements from
    # the arXiv/GitHub audit. Each sub-feature can be deactivated independently.
    ("F16",     "Fractional Kelly Position Sizing", 1),   # Phase 1 per blueprint
    ("F51a",    "HMM-Adaptive ATR Multiplier",     0),
    ("F51b",    "Funding Rate Extremes Gate",      0),
    ("F51c",    "ADX Pair-Ranker (1h)",            0),
    ("F51d",    "Chandelier Exit Trailing SL",     0),
    # F52, F53, F54 (cont. 55, 2026-05-28) — Tier S features from the
    # direction-prediction + strategy-creator SOTA survey.
    # See next_impl/{f52_exchange_netflow,f53_qlib_alpha_pool,f54_llm_dsl_alpha_miner}.md
    ("F52",     "Exchange Net-Flow Directional Gate", 0),
    ("F53",     "Qlib Alpha-158 Formulaic Pool",      0),
    ("F54",     "LLM-DSL Crypto Alpha Miner",         0),
    # F56, F58, F60 (cont. 56, 2026-05-28) — Tier A features. F55 (Kronos),
    # F57 (TLOB), F59 (RD-Agent), F61 (AlphaGen-RL) remain deferred — too
    # large for a single session.
    ("F56",     "Conformal Prediction Wrapper",       0),
    ("F58",     "Liquidation Cascade Alpha",          0),
    ("F60",     "AlphaAgent Novelty Regularisers",    0),
]


def bootstrap_all_features() -> None:
    """Register every feature in the registry. Safe to call multiple times — idempotent."""
    registered = 0
    failed = 0
    for feature_id, name, phase in FEATURES:
        try:
            register(feature_id, name, activation_phase=phase)
            registered += 1
        except Exception as exc:
            log.warning("feature_register_failed", feature_id=feature_id, error=str(exc))
            failed += 1
    log.info("feature_governance_bootstrap_complete",
             total=len(FEATURES), registered=registered, failed=failed)


if __name__ == "__main__":
    # Allow running manually: python -m feature_governance.bootstrap
    import sys
    sys.path.insert(0, "/app")
    from db import init_pool
    init_pool()
    bootstrap_all_features()
