"""cont. 62c — Central pair → class lookup.

Single source of truth for "what bucket is this pair in?". Reads the
runtime bucket assignment written by scanner/main.py:update_active_pairs
(via scanner/categories.py:select_top_per_bucket) into Redis keys
`scanner:pair_bucket:{pair}`.

Use this in place of the deleted hardcoded sets:
  _MAJOR_PAIRS / _NATIVE_PAIRS / _MAJORS
in memory/cognitive/q_learning.py, memory/cognitive/memrl.py,
memory/cognitive/consolidation.py, data/onchain_netflow.py.

Returns:
  - `classify(pair)` → bucket name (str), or "alt" if unknown.
  - `class_2(pair)`  → "major" if in the anchors_l1 bucket, else "alt".
    This is the coarse 2-class split used by Q-learning state buckets;
    Q-tables remain interpretable across the migration.
"""
from __future__ import annotations

import redis_client

# Bucket name written by scanner/categories.py for the L1 anchors bucket.
_ANCHORS_BUCKET = "anchors_l1"

# Coarse "major" bucket also accepts the legacy alias used in old Q-table keys.
_MAJOR_BUCKETS = {"anchors_l1", "anchors"}


def classify(pair: str) -> str:
    """Return the bucket name for `pair`. Falls back to 'alt' when no
    assignment exists (pair outside the bucketed universe, or scanner
    hasn't built buckets yet on a fresh boot)."""
    if not pair:
        return "alt"
    try:
        val = redis_client.get().get(f"scanner:pair_bucket:{pair}")
    except Exception:
        return "alt"
    return val if isinstance(val, str) and val else "alt"


def class_2(pair: str) -> str:
    """Coarse 2-class split: 'major' for anchors_l1 bucket, 'alt' for
    everything else. Replaces hardcoded `if pair in {'BTCUSDT','ETHUSDT',
    'SOLUSDT'}`."""
    return "major" if classify(pair) in _MAJOR_BUCKETS else "alt"


# Note: data/onchain_netflow._NATIVE_PAIRS intentionally stays hardcoded
# to {BTC,ETH,SOL}. That set is a DATA-PROVIDER CAPABILITY marker
# (CoinMetrics free tier covers exactly those 3 chains), not a trading
# universe category — bucket assignment isn't the right abstraction
# there. Keep them separate.
