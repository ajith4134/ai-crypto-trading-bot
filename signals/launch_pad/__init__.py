"""Launch-Pad (cont. 70) — a 10-deep on-deck buffer of pre-qualified symbols
that is the sole funnel for opening trades.

Plan + locked decisions: /opt/trading-bot/next_impl/launch_pad_10.md

Submodules:
  store.py      — slot CRUD, Redis hot-mirror, history logging (shared by all phases)
  shadow.py     — P2: shadow MAE/MFE tracker (PnL / peak-profit / peak-loss from table-entry price)
  qualify.py    — P3: "open-green" gate + the 3 movement columns (added in P3)
  maintainer.py — P4: keep buffer full, displace weakest, flip rule, TTL (added in P4)

Everything is gated behind Redis `launchpad:enabled=0` (shadow-only) until reviewed.
"""
