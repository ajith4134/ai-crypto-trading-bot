"""Predict-All-Before-Open module — cont. 63 (2026-05-29).

Implements Phase B of next_impl/predict_all_before_open.md:
pre-warmed XGBoost predictions of (direction, entry, SL, TP, hold, confidence,
RR distribution) for the top-N scanner-ranked symbols every 30 s, written to
predictions:{symbol} Redis hash and the predictions DB table.

Cold-start posture: when no trained model file exists at the path
`models/predict_all_xgb.pkl`, predict() returns None and the signal-side gate
(signals/engine.py) treats the signal as "no prediction available" — the
legacy accept_or_reject path takes over. This makes Phase B safe to ship
before a model is trained.
"""
