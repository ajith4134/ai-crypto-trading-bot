# Categorised 100-Pair Scanner (10 per category × 10 categories)

Owner ask (cont. 62b+, 2026-05-29 session):

> Scan ALL ~400 Binance USDT-M perps (not just 42). Categorise into the buckets
> we discussed (anchors, large-cap, mid-cap, small-cap, memes, L2, DeFi, AI,
> GameFi, LST, Oracle, Privacy, Storage/DePIN, RWA, NFT, stablecoin perps).
> Pick top 10 from each category → 100 active pairs. Refresh every
> 5 / 20 / 30 min. Trades drawn only from these 100. No hardcoded symbol lists.

---

## 1. Current state on disk (verified)

- `config.yaml: trading.max_active_pairs: 200` (cap), `scanner.rescan_interval_hours: 8`.
- Live `scanner:active_pairs` set: **42** symbols (well under the 200 cap).
- Live `scanner:anchor_pairs` set: **30** symbols (dynamic by mcap via CoinGecko,
  re-derived every scan).
- `scanner/main.py:run_scan()` scores ALL Binance USDT-M perps (~400) on 8 criteria
  (volume, volatility, spread, win-rate, PnL, candle-setup, ADX, OI), then
  filters down to the active list using anchors-only mode + movers + the
  cascade (listing-age / spread / mcap / quote-vol / funding / 24h change).
- `scanner/market_data.py`: CoinGecko mcap fetch (top 250) + Binance
  `onboardDate` listing-age fetch. No category-aware code anywhere.
- Categorisation today: **hardcoded** ad-hoc in 4 places — `_NATIVE_PAIRS = {BTC,ETH,SOL}`,
  `_MAJOR_PAIRS = {BTC,ETH,SOL}`, `_MAJORS = {BTC,ETH}`, and `_STABLECOIN_BASE_SYMBOLS`.

Blueprint §10.8 says scan every USDT-M futures pair, score 5 criteria with
Brain-learned weights, periodic rescan, target up to 60 active in paper phase.
**The blueprint is silent on categories.** A category-aware bucket policy is
additive to §10.8 — the existing composite score still ranks within each
bucket; the bucket only enforces breadth.

---

## 2. Online research

| Question | Finding |
|---|---|
| Free data source for crypto categories | CoinGecko Demo (free) plan: 100 calls/min, 10 k/month, `/coins/categories/list` and `/coins/markets?category=<id>` endpoints accessible. 500+ categories. |
| Are the categories I listed (memes, L2, AI, etc.) all real CoinGecko IDs? | Yes — `meme-token`, `layer-1`, `layer-2`, `decentralized-finance-defi`, `artificial-intelligence`, `gaming`, `oracle`, `privacy-coins`, `real-world-assets-rwa`, `storage`, `liquid-staking-tokens`, `non-fungible-tokens-nft` are standard category slugs. |
| Per-category vs per-coin fetch | Per-category is the right unit: one `/coins/markets?category=<id>&per_page=100` call returns the top-100 coins in that category with mcap/volume/price. 16 categories × 1 call = 16/refresh. |
| Multi-category coins | CoinGecko coins carry MULTIPLE category tags (e.g. SOL is "layer-1" AND "smart-contract-platform" AND "solana-ecosystem"). Industry practice: assign each coin to ONE PRIMARY category (its highest-mcap-rank one) when bucketing, but track full list. |
| Cadence | Category MEMBERSHIP changes slowly (a token doesn't switch from "AI" to "DeFi" overnight) → cache 24 h. Per-bucket RANKING is by composite score — already realtime in Redis — can re-rank cheaply (no API call). |

Sources:
- CoinGecko API pricing — https://www.coingecko.com/en/api/pricing
- CoinGecko categories list (5 + endpoints) — https://docs.coingecko.com/reference/categories-list

---

## 3. Proposed architecture

### 3.1 Category set (10 buckets — owner asked for "top 10 in all categories" totaling 100)

| Bucket | CoinGecko `category` slug | Why kept | Why this size |
|---|---|---|---|
| 1. Anchors / L1 majors | `layer-1` (filtered to top-10 by mcap) | The market benchmarks. | 10 |
| 2. Layer-2 / scaling | `layer-2` | Distinct alpha from L1s. | 10 |
| 3. DeFi | `decentralized-finance-defi` | Largest theme by tokens listed. | 10 |
| 4. AI | `artificial-intelligence` | Highest narrative beta in 2025-26. | 10 |
| 5. Memecoins | `meme-token` | Volatility playground; pure direction. | 10 |
| 6. GameFi / metaverse | `gaming` (or `metaverse`) | Discrete narrative cycles. | 10 |
| 7. LST / Restaking | `liquid-staking-tokens` | Yield-narrative cohort. | 10 |
| 8. Real-World Assets | `real-world-assets-rwa` | Institutional flow. | 10 |
| 9. DePIN / Storage | `depin` (preferred) or `storage` | Hardware-narrative cohort. | 10 |
| 10. Oracle + Privacy + NFT residual | union of `oracle`, `privacy-coins`, `non-fungible-tokens-nft` | Small individually; pooled to hit 10. | 10 |

Total: **100 pairs.** Categories list is **runtime-overridable** via Redis
key `scanner:category_slugs` (JSON list) — owner can swap a bucket without
redeploy.

**Stablecoin perps** are explicitly EXCLUDED (current behaviour kept).

### 3.2 Multi-category handling

A coin belongs to many CoinGecko categories. Bucket assignment rule:

1. Walk the bucket list in the ORDER listed above (Anchors first, residual last).
2. Assign each coin to the FIRST bucket whose CoinGecko slug appears in the
   coin's `categories` field.
3. A coin is counted in exactly ONE bucket — no double-counting toward the 100.

This deterministically prevents BTC from filling both "Anchors" and "DeFi"
slots. The anchors bucket gets first pick, so the canonical majors land
there even when CoinGecko also tags them under L2 / RWA / etc.

### 3.3 Per-bucket ranking

Inside each bucket, sort by the existing 8-criteria composite score
(volume, volatility, spread, win-rate, PnL, candle-setup, ADX, OI) using
the Brain-learned weights. Take top 10. This means the per-bucket *quality*
filter is exactly what §10.8 calls for; the bucket only ensures *breadth*.

If a bucket has < 10 categorised pairs available on Binance USDT-M perps
(possible for niche buckets like LST), fill the deficit from the
**residual bucket #10 by mcap**. Final list is always 100 or as close as
possible; counter `scanner:bucket_underfilled_count` logs the shortfall.

### 3.4 Refresh cadence

- **Category MEMBERSHIP** (CoinGecko calls): every **24 h** (slow-changing,
  cache TTL). 16 calls/day × 30 days ≈ 480 calls/month. Free-tier safe.
- **Per-bucket RANKING** (composite re-score + top-10 pick): every
  **N minutes**, configurable in Redis `scanner:rerank_interval_minutes`.
  No API calls — pure in-memory re-sort over existing Redis ticker /
  win-rate / PnL data.
- The 8-criteria *score computation* itself (currently 2-3 min for ADX 50×
  klines + OI 60× API) runs each rerank — still bounded since ADX/OI only
  touch the post-pass-1 top 50/60.

Owner choice for the rerank interval is the user-facing knob. Recommended
**20 min** (sub-cache-window, doesn't burn cache; bounded ADX/OI work).
5 min is feasible but compute-heavy. 30 min is comfortable margin.

### 3.5 No hardcoded symbol lists

- `_NATIVE_PAIRS`, `_MAJOR_PAIRS`, `_MAJORS` constants are **deleted**.
- Replacement: `risk/pair_classes.py` (new module) exposes
  `classify(pair) -> str` reading the live bucket assignment from Redis
  `scanner:pair_bucket:{pair}` (written by the scanner).
- `q_learning.py` / `memrl.py` / `consolidation.py` / `onchain_netflow.py`
  switch from local hardcoded sets to `from risk.pair_classes import classify`.
- The "majors"/"alts" 2-class split (used by Q-learning bucket key) is
  preserved as a coarsening: `"anchor" → "majors"`, all else → `"alts"`.
  This keeps existing Q-tables interpretable.

### 3.6 Files touched

- `scanner/categories.py` — NEW. `fetch_category_universe(slugs, binance_set)`
  → `{slug: [sym, mcap]...}`. CoinGecko Demo calls, 24 h Redis cache.
- `scanner/main.py` — NEW step inside `run_scan` between composite calc
  and `update_active_pairs`: bucket the composite by category, top-10 per
  bucket, write `scanner:pair_bucket:*` keys. Pass the bucketed union to
  `update_active_pairs` as the candidate set.
- `scanner/main.py::scanner_loop` — adjust sleep to read
  `scanner:rerank_interval_minutes` (default 20 min); on first boot or
  when `scanner:category_cache_stale=1`, re-fetch categories.
- `risk/pair_classes.py` — NEW. Single source of truth for pair → class.
- `memory/cognitive/q_learning.py`, `memory/cognitive/memrl.py`,
  `memory/cognitive/consolidation.py`, `data/onchain_netflow.py` — replace
  local `_MAJOR_PAIRS` / `_NATIVE_PAIRS` constants with
  `from risk.pair_classes import classify`. Map "anchor" bucket → "major";
  everything else → "alt" for the 2-class consumers.
- `redis_keys.py` — new constants for the bucket and category-list keys.
- `config.yaml` — document the new defaults.

### 3.7 Honest Rule-4 production-grade checklist

| Check | Status |
|---|---|
| Coverage of owner's ask | ✅ all 400 pairs scanned, 10 categories × 10, refresh cadence configurable, no hardcoded symbols |
| Consume side | ✅ `signals/engine.py` already reads `scanner:active_pairs` set — switching that set's source is transparent to consumers |
| Producer side | ✅ scanner now writes bucket → set and per-pair bucket Redis keys |
| Silent-rejection | ✅ underfilled-bucket counter; CoinGecko fetch failure counter; explicit log of the post-bucketing union size |
| Blueprint stance | ✅ additive to §10.8 (breadth filter on top of existing composite quality filter) — no amendment needed |
| Rate-limit safety | ✅ 16 calls/day to CoinGecko (24 h membership cache) — 480/month vs 10 k free-tier cap |
| Brain override | ✅ Brain can write `scanner:pair_bucket_override:{pair}` to force-assign |
| Backout | ✅ Redis flag `scanner:categorised_mode_disabled=1` reverts to current composite-only universe |

---

## 4. Owner decisions (CONFIRMED 2026-05-29)

- [x] **D-1**: Use proposed 10 buckets (Anchors L1 / L2 / DeFi / AI / Memes /
      GameFi / LST / RWA / DePIN / Oracle+Privacy+NFT residual).
- [x] **D-2**: 20-min rerank cadence.
- [x] **D-3**: Fill deficit from residual by mcap.
- [x] **D-4**: Keep 200 cap; bucketed 100 = PRIORITY CORE, remaining 100 slots
      filled by highest composite irrespective of bucket.
- [x] **D-5**: CoinGecko Demo (free, 100 calls/min, 10 k/month).

---

## 5. Session handoff

Pending owner confirmation of D-1…D-5.

Linked memories: [[feedback_blueprint_first]], [[feedback_blueprint_grade_check]],
[[feedback_silent_rejection]], [[feedback_progress_tracking]],
[[feedback_verify_before_fix]], [[feedback_next_implementation]].
