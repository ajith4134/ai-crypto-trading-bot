# Phase A — Code Surface Map (Migration 027 companion)

Total tp1/tp2 references across the codebase: **73** in 7 files.

## File-by-file surface

| File | LOC w/ tp1/tp2 | Kind of change |
|---|---|---|
| `risk/manager.py` | 48 | Profit-lock ratchet, TP firing logic, exit-band logic. Heart of the change. |
| `risk/frontier/llm_council.py` | 6 | LLM exit-decision inputs. Replace tp1 input with tp. |
| `signals/engine.py` | 5 | INSERTs into trades on signal-acceptance. Mostly setting tp1=value. |
| `memory/write.py` | 4 | Trade memory writes — same pattern as signals/engine.py. |
| `dashboard/api.py` | 4 | Read tp1/tp2 for dashboard display. Show single `tp` instead. |
| `execution/paper.py` | 3 | Paper-mode order placement. Replace tp1 SELECT with tp. |
| `execution/live.py` | 3 | Live-mode order placement. Replace tp1 SELECT with tp. |

## Transition strategy (write-through, then cut)

To keep the live bot safe during the migration window:

**Window 1 — write-through (after Migration 027 applied):**
- Every site that writes `tp1=X` also writes `tp=X` in the same statement.
- Every site that reads `tp1` reads `COALESCE(tp, tp1)` so new trades work even if a code path missed write-through.
- No site touches tp2 (the runner role transfers to the existing profit-lock ratchet).

**Window 2 — cut (after 1 release of stable write-through):**
- Migration 028: `ALTER TABLE trades DROP COLUMN tp1, tp1_fired, tp1_target, tp2, tp2_target;`
- Remove all tp1/tp2 references from code.

This is safer than a big-bang rename. Lets rollback by simply reverting to reading tp1.

## NEW files to create (Phase A)

| File | LOC est. | Purpose |
|---|---|---|
| `pattern/__init__.py` | ~5 | Module marker |
| `pattern/clusterer.py` | ~150 | HDBSCAN on CandleNet embeddings. `fit_clusters(embeddings)` for batch training + `predict_cluster(embedding) -> int` for online lookup. Persist cluster centroids in `models/pattern_clusters.pkl`. |
| `pattern/registry.py` | ~120 | Read/write `pattern_effectiveness_registry`. `update_on_close(trade_id)` joins predictions ↔ trades, updates the row. `lookup(cluster, regime, direction) -> {win_rate, avg_rr, …}` for signal-side gates. |
| `pretrainer/pattern_cluster_train.py` | ~80 | Offline batch trainer: load all closed-trade CandleNet embeddings → HDBSCAN → persist clusters → backfill `trades.pattern_cluster_id` for historical data. |

Total NEW: **~355 LOC across 4 files.**

## Phase A code patches (LOC by file)

| File | Patch LOC | Description |
|---|---|---|
| `risk/manager.py` | ~60 | Write-through: every UPDATE/INSERT touching tp1 also touches tp. Every read becomes `COALESCE(tp, tp1)`. The 48 references collapse to ~25 distinct logical sites. |
| `risk/frontier/llm_council.py` | ~10 | 6 references → 6 line replacements. |
| `signals/engine.py` | ~15 | INSERT statements gain tp column. |
| `memory/write.py` | ~10 | Same pattern. |
| `dashboard/api.py` | ~10 | Dashboard shows `tp` column instead of `tp1`/`tp2` fan-out. UI change minor. |
| `execution/paper.py` | ~10 | Order placement. |
| `execution/live.py` | ~10 | Same. |
| `celery_app.py` | ~30 | New beat task `update_pattern_registry` (runs every 5 min), new beat task `cleanup_stale_predictions` (runs hourly). |

Total CODE patches: **~155 LOC across 8 files.**

## Grand total for Phase A
- Migration: 1 SQL file (~120 lines, drafted at `step2a_027_predict_all_schema.sql.draft`)
- New files: 4 (~355 LOC)
- Code patches: 8 files (~155 LOC)
- **= ~510 LOC + 1 migration. Realistic: 3 dev-days for production-grade with tests.**

## Sequencing within Phase A (recommended order)

1. **A.0 — Migration 027 applied.** Schema in place. Zero behavior change.
2. **A.1 — Write-through patches** (`risk/manager.py`, `signals/engine.py`, `memory/write.py`, `execution/{paper,live}.py`). Reads use `COALESCE(tp, tp1)`. Writes hit both columns. Bot still uses tp1 logic functionally. New `tp` data starts accumulating.
3. **A.2 — NEW `pattern/clusterer.py`** + offline batch train. Backfill `trades.pattern_cluster_id` for closed-trade history.
4. **A.3 — NEW `pattern/registry.py`** + new beat task `update_pattern_registry`. Registry starts populating.
5. **A.4 — `dashboard/api.py` UI update** to show `tp` + `pattern_cluster_id` + win-rate from registry.
6. **A.5 — Verify** 100+ closed trades show up in registry, win-rate stats look sane. Reservoir for Phase B/C training.

Phase B can start once A.5's data is flowing.

## What's intentionally NOT in Phase A
- The XGBoost predictor itself (Phase B).
- Signal-side prediction-gate enforcement (Phase C).
- Conformal calibration drift monitor (Phase D).
- Transformer model (Phase E).
- Walk-forward backtest harness (Phase F).
- Single-TP COLUMN DROP (Migration 028 — separate release).

## Open verification items (resolve at apply-time)

1. **Pattern cluster persistence path** — `models/pattern_clusters.pkl` vs. Postgres? Pickle is simplest; Postgres pattern_clusters table is more queryable. Recommend pickle for v1, table for v2.
2. **HDBSCAN min_cluster_size** — 20? 50? Tradeoff: low = many tiny clusters (overfitting); high = few coarse clusters (under-fits). Default to `min_cluster_size = max(50, n_samples // 500)` adaptively.
3. **CandleNet embedding dimension** — need to confirm what ml/candlenet.py emits before sizing the clusterer.
