"""
Section J: Historical Data Download & Pre-Training — J-01 to J-07.
Run as one-shot Docker Compose service:
  docker compose --profile pretrainer up pretrainer
"""
import sys
import csv
import os
import time
from pathlib import Path
import structlog
import torch
import torch.nn as nn

# cont. 49 — use all 4 container cores for CPU training (PyTorch default
# is 2). Set BEFORE any model is built so the thread pool sizes correctly.
# MKL_NUM_THREADS also honoured by Intel MKL-DNN kernels.
_TORCH_THREADS = int(os.environ.get("PRETRAINER_TORCH_THREADS", "4"))
try:
    torch.set_num_threads(_TORCH_THREADS)
    torch.set_num_interop_threads(max(1, _TORCH_THREADS // 2))
    os.environ.setdefault("OMP_NUM_THREADS", str(_TORCH_THREADS))
    os.environ.setdefault("MKL_NUM_THREADS", str(_TORCH_THREADS))
except Exception:
    pass

# Shared model classes — see ml/architectures.py for rationale
sys.path.insert(0, "/app")
from ml.architectures import TFTModel, PatchTSTModel, GNNModel, WorldModelBundle

log = structlog.get_logger()
log.info("pretrainer_torch_config",
         num_threads=torch.get_num_threads(),
         interop_threads=torch.get_num_interop_threads(),
         mkldnn=torch.backends.mkldnn.is_available())

MODELS_DIR = Path("models")
DATA_DIR = Path("data/historical")
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
LOOKBACK_DAYS = 730          # 2 years
DOWNLOAD_PAIRS = 350         # Download historical data for 350 pairs
                             # Bot actively monitors top 200 — more training data = better models


# ── Legacy model class definitions kept below for reference only ───────────────
# These classes are now defined in ml/architectures.py and imported above.
# DO NOT define them at module level here — that's what caused the __main__
# pickle reference issue. Future pretraining MUST save state_dict only.

# (Model classes now imported from ml/architectures.py — see top of file)


# ── J-01: OHLCV Downloader ────────────────────────────────────────────────────

def download_ohlcv(client, symbols: list[str]) -> None:
    """Download 2+ years OHLCV for all pairs at all 6 timeframes."""
    from datetime import datetime, timedelta, timezone
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    total = len(symbols) * len(INTERVALS)
    done = 0

    for symbol in symbols:
        for interval in INTERVALS:
            out_dir = DATA_DIR / symbol
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / f"{interval}.csv"

            if out_file.exists():
                done += 1
                continue

            try:
                klines = client.get_historical_klines(symbol, interval, start_ms, end_ms)
                if klines:
                    with open(out_file, "w", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                        for k in klines:
                            writer.writerow([k[0], k[1], k[2], k[3], k[4], k[5]])
                done += 1
                if done % 60 == 0:
                    log.info("ohlcv_progress", done=done, total=total, pct=round(done/total*100, 1))
                time.sleep(0.08)
            except Exception as exc:
                log.error("ohlcv_download_failed", symbol=symbol, interval=interval, error=str(exc))
                done += 1

    pairs_done = len(list(DATA_DIR.glob("*")))
    log.info("ohlcv_complete", pairs=pairs_done)


# ── J-02: HMM Regime Model ───────────────────────────────────────────────────

def train_hmm(symbols: list[str]) -> None:
    import numpy as np
    import pickle
    try:
        from hmmlearn import hmm

        all_returns = []
        for symbol in symbols[:30]:   # use top 30 for HMM stability
            csv_file = DATA_DIR / symbol / "1d.csv"
            if not csv_file.exists():
                continue
            closes = [float(row["close"]) for row in csv.DictReader(open(csv_file))]
            returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
            all_returns.extend(returns)

        if len(all_returns) < 100:
            log.warning("hmm_insufficient_data", count=len(all_returns))
            return

        X = np.array(all_returns).reshape(-1, 1)
        best_bic, best_model = float("inf"), None
        for k in range(2, 5):
            model = hmm.GaussianHMM(n_components=k, n_iter=200, random_state=42)
            model.fit(X)
            bic = -2 * model.score(X) + k * np.log(len(X))
            if bic < best_bic:
                best_bic, best_model = bic, model

        MODELS_DIR.mkdir(exist_ok=True)
        with open(MODELS_DIR / "hmm_regime.pkl", "wb") as f:
            pickle.dump(best_model, f)
        log.info("hmm_trained", n_states=best_model.n_components,
                 regimes=["bull", "bear", "turbulent"][:best_model.n_components])
    except Exception as exc:
        log.error("hmm_training_failed", error=str(exc))


# ── J-03 to J-06: ML Model Initialization ────────────────────────────────────

def train_tft(symbols: list[str]) -> None:
    """F19 — Real TFT pretraining on historical 1h OHLCV (per blueprint line 449).

    Loads 1h close-price sequences from /app/data/historical/{symbol}/1h.csv across
    the top N symbols, samples 100-step windows, normalizes each window by its first
    close price (scale invariance across pairs), trains with pinball quantile loss
    for ~5 epochs. The normalization contract matches ml/tft.py inference.

    Per Rule 4 honesty: this is the SIMPLIFIED TFT (no VSN, no multi-horizon, no
    static covariates) — see ml/architectures.TFTModel docstring. But the model
    IS now actually trained, not just saved with random weights as before.
    """
    import numpy as np
    from ml.architectures import TFTModel, tft_quantile_loss

    MODELS_DIR.mkdir(exist_ok=True)
    SEQ_LEN        = 100
    BATCH_SIZE     = 64
    N_EPOCHS       = 5
    LR             = 1e-3
    MAX_PAIRS      = 50
    MAX_WINDOWS    = 30_000  # cap total training samples — keeps wall-clock under a few minutes

    # Collect (sequence, next-price) pairs from 1h CSVs
    windows: list[tuple[list[float], float]] = []
    pairs_used = 0
    for symbol in symbols[:MAX_PAIRS]:
        csv_file = DATA_DIR / symbol / "1h.csv"
        if not csv_file.exists():
            continue
        try:
            closes = [float(row["close"]) for row in csv.DictReader(open(csv_file)) if row.get("close")]
        except Exception:
            continue
        if len(closes) <= SEQ_LEN:
            continue
        # Slide window with stride 1; cap per-pair to keep balance
        per_pair_cap = MAX_WINDOWS // MAX_PAIRS
        sampled = 0
        for i in range(0, len(closes) - SEQ_LEN - 1):
            if sampled >= per_pair_cap:
                break
            seq = closes[i:i+SEQ_LEN]
            nxt = closes[i+SEQ_LEN]
            anchor = seq[0]
            if anchor <= 0:
                continue
            seq_norm = [c / anchor for c in seq]
            nxt_norm = nxt / anchor
            windows.append((seq_norm, nxt_norm))
            sampled += 1
        pairs_used += 1

    if len(windows) < 100:
        log.warning("tft_insufficient_training_data", windows=len(windows), pairs=pairs_used)
        # Save untrained model so downstream loaders don't fail
        model = TFTModel()
        torch.save(model.state_dict(), MODELS_DIR / "tft.pth")
        log.info("tft_saved_untrained", params=sum(p.numel() for p in model.parameters()))
        return

    log.info("tft_training_start",
             pairs_used=pairs_used, windows=len(windows),
             seq_len=SEQ_LEN, batch_size=BATCH_SIZE, n_epochs=N_EPOCHS)

    # Build tensors once
    X = torch.tensor([w[0] for w in windows], dtype=torch.float32).unsqueeze(-1)  # [N, T, 1]
    y = torch.tensor([w[1] for w in windows], dtype=torch.float32).unsqueeze(-1)  # [N, 1]

    model = TFTModel()
    opt = torch.optim.Adam(model.parameters(), lr=LR)
    model.train()

    n = X.shape[0]
    for epoch in range(N_EPOCHS):
        perm = torch.randperm(n)
        epoch_loss = 0.0
        n_batches = 0
        for start in range(0, n, BATCH_SIZE):
            idx = perm[start:start+BATCH_SIZE]
            xb, yb = X[idx], y[idx]
            opt.zero_grad()
            pred = model(xb)
            loss = tft_quantile_loss(pred, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            opt.step()
            epoch_loss += float(loss.item())
            n_batches += 1
        log.info("tft_epoch_done", epoch=epoch+1, avg_loss=round(epoch_loss / max(n_batches, 1), 6))

    model.eval()
    # Atomic save: temp file + rename. Also handle the case where the existing tft.pth
    # is owned by another user (e.g. from a previous pretrainer run as a different uid)
    # by unlinking first — chmod 777 on the dir lets us create a new file, but doesn't
    # let us overwrite a file we don't own.
    tft_path = MODELS_DIR / "tft.pth"
    tmp_path = tft_path.with_suffix(".pth.tmp")
    torch.save(model.state_dict(), tmp_path)
    if tft_path.exists():
        try:
            tft_path.unlink()
        except PermissionError:
            log.warning("tft_save_unlink_failed_trying_overwrite", path=str(tft_path))
    tmp_path.replace(tft_path)
    # Sanity sample on first window
    with torch.no_grad():
        sample_pred = model(X[:1])
        log.info("tft_trained_saved",
                 params=sum(p.numel() for p in model.parameters()),
                 sample_q10=round(float(sample_pred[0, 0]), 4),
                 sample_q50=round(float(sample_pred[0, 1]), 4),
                 sample_q90=round(float(sample_pred[0, 2]), 4),
                 sample_target=round(float(y[0]), 4))


def train_patchtst(symbols: list[str]) -> None:
    """F20 — Real PatchTST pretraining on historical 1h OHLCV.

    Architecture: HF transformers.PatchTSTForPrediction (real Nie et al. impl).
    Training: 256-step context → 16-step prediction. Per-sequence normalization
    by first close (matches inference path in ml/patchtst.py). MSE loss, 2 epochs
    (smaller than TFT because the model is bigger and we just need a reasonable
    starting point — the long-horizon prediction task is noisy anyway).

    Per Rule 4 honesty: a single-step price target is highly noisy for crypto.
    Treat the trained weights as a sensible-init prior, not a deployed forecaster.
    Real production-grade would: longer training, validation split, RevIN scaling,
    and ensembling. Out of scope for this PR.
    """
    import numpy as np
    from ml.architectures import PatchTSTModel

    MODELS_DIR.mkdir(exist_ok=True)
    CONTEXT_LEN     = 256
    PRED_LEN        = 16
    BATCH_SIZE      = 32
    N_EPOCHS        = 2
    LR              = 1e-4
    MAX_PAIRS       = 30
    MAX_WINDOWS     = 10_000   # half what TFT uses — model is bigger, batches are slower

    # Collect (256-step context, 16-step target) windows from 1h CSVs
    windows: list[tuple[list[float], list[float]]] = []
    pairs_used = 0
    for symbol in symbols[:MAX_PAIRS]:
        csv_file = DATA_DIR / symbol / "1h.csv"
        if not csv_file.exists():
            continue
        try:
            closes = [float(row["close"]) for row in csv.DictReader(open(csv_file)) if row.get("close")]
        except Exception:
            continue
        if len(closes) <= CONTEXT_LEN + PRED_LEN:
            continue
        per_pair_cap = MAX_WINDOWS // MAX_PAIRS
        sampled = 0
        # Stride 8 to get more samples per pair (overlapping windows)
        for i in range(0, len(closes) - CONTEXT_LEN - PRED_LEN, 8):
            if sampled >= per_pair_cap:
                break
            ctx = closes[i:i+CONTEXT_LEN]
            tgt = closes[i+CONTEXT_LEN:i+CONTEXT_LEN+PRED_LEN]
            anchor = ctx[0]
            if anchor <= 0:
                continue
            ctx_norm = [c / anchor for c in ctx]
            tgt_norm = [c / anchor for c in tgt]
            windows.append((ctx_norm, tgt_norm))
            sampled += 1
        pairs_used += 1

    if len(windows) < 100:
        log.warning("patchtst_insufficient_training_data", windows=len(windows), pairs=pairs_used)
        model = PatchTSTModel()
        # Atomic save
        ptst_path = MODELS_DIR / "patchtst.pth"
        tmp_path = ptst_path.with_suffix(".pth.tmp")
        torch.save(model.state_dict(), tmp_path)
        if ptst_path.exists():
            try:
                ptst_path.unlink()
            except PermissionError:
                pass
        tmp_path.replace(ptst_path)
        log.info("patchtst_saved_untrained", params=sum(p.numel() for p in model.parameters()))
        return

    log.info("patchtst_training_start",
             pairs_used=pairs_used, windows=len(windows),
             context_len=CONTEXT_LEN, pred_len=PRED_LEN,
             batch_size=BATCH_SIZE, n_epochs=N_EPOCHS)

    X = torch.tensor([w[0] for w in windows], dtype=torch.float32).unsqueeze(-1)  # [N, 256, 1]
    y = torch.tensor([w[1] for w in windows], dtype=torch.float32)                # [N, 16]

    model = PatchTSTModel()
    opt = torch.optim.Adam(model.parameters(), lr=LR)
    model.train()

    n = X.shape[0]
    for epoch in range(N_EPOCHS):
        perm = torch.randperm(n)
        epoch_loss = 0.0
        n_batches = 0
        for start in range(0, n, BATCH_SIZE):
            idx = perm[start:start+BATCH_SIZE]
            xb, yb = X[idx], y[idx]
            opt.zero_grad()
            pred = model(xb)             # [B, 16]
            loss = torch.nn.functional.mse_loss(pred, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            opt.step()
            epoch_loss += float(loss.item())
            n_batches += 1
        log.info("patchtst_epoch_done", epoch=epoch+1, avg_loss=round(epoch_loss / max(n_batches, 1), 6))

    model.eval()
    # Atomic save with stale-file unlink
    ptst_path = MODELS_DIR / "patchtst.pth"
    tmp_path = ptst_path.with_suffix(".pth.tmp")
    torch.save(model.state_dict(), tmp_path)
    if ptst_path.exists():
        try:
            ptst_path.unlink()
        except PermissionError:
            log.warning("patchtst_save_unlink_failed", path=str(ptst_path))
    tmp_path.replace(ptst_path)
    # Sanity sample
    with torch.no_grad():
        sample_pred = model(X[:1])
        log.info("patchtst_trained_saved",
                 params=sum(p.numel() for p in model.parameters()),
                 sample_final_pred=round(float(sample_pred[0, -1]), 4),
                 sample_target_final=round(float(y[0, -1]), 4))


def train_gnn(symbols: list[str]) -> None:
    """F24 — initialise GAT-based GNN and save state_dict.

    The graph itself (correlations + edges + leader-follower) is computed at
    INFERENCE time from live CANDLES — see ml/gnn.get_interasset_signals.
    Pretraining only needs to produce a checkpoint with the right architecture
    (Xavier-init weights); the correlation-driven outputs are data-driven and
    don't depend on trained GAT weights for correctness.

    A proper graph-autoencoder pretraining loop could be added later as a
    self-supervised objective (mask node features, predict from neighbors), but
    blueprint Feature 24 emphasises the dynamic graph + leader-follower outputs
    over learned attention weights.
    """
    import numpy as np
    MODELS_DIR.mkdir(exist_ok=True)

    # Sanity log — average correlation across top-N pairs (informational only)
    closes_by_symbol = {}
    for symbol in symbols[:20]:
        csv_file = DATA_DIR / symbol / "1d.csv"
        if csv_file.exists():
            try:
                closes = [float(row["close"]) for row in csv.DictReader(open(csv_file)) if row.get("close")]
                if closes:
                    closes_by_symbol[symbol] = closes[-365:]
            except Exception:
                pass

    if len(closes_by_symbol) >= 2:
        min_len = min(len(v) for v in closes_by_symbol.values())
        if min_len > 1:
            try:
                matrix = np.array([v[:min_len] for v in closes_by_symbol.values()])
                corr = np.corrcoef(matrix)
                log.info("gnn_corr_sanity", pairs=len(closes_by_symbol),
                         avg_abs_corr=round(float(np.mean(np.abs(corr))), 3))
            except Exception:
                pass

    model = GNNModel()
    # Xavier init for Linear sub-modules in GATConv (GATConv internally creates Linear)
    for mod in model.modules():
        if isinstance(mod, torch.nn.Linear):
            torch.nn.init.xavier_uniform_(mod.weight)
            if mod.bias is not None:
                torch.nn.init.zeros_(mod.bias)

    # Atomic save with stale-file unlink (handles ownership conflict)
    gnn_path = MODELS_DIR / "gnn.pth"
    tmp_path = gnn_path.with_suffix(".pth.tmp")
    torch.save(model.state_dict(), tmp_path)
    if gnn_path.exists():
        try:
            gnn_path.unlink()
        except PermissionError:
            log.warning("gnn_save_unlink_failed_trying_overwrite", path=str(gnn_path))
    tmp_path.replace(gnn_path)
    log.info("gnn_saved", params=sum(p.numel() for p in model.parameters()))


def train_world_model(symbols: list[str]) -> None:
    MODELS_DIR.mkdir(exist_ok=True)
    model = WorldModelBundle()
    torch.save(model.state_dict(), MODELS_DIR / "world_model.pth")
    log.info("world_model_saved", params=sum(p.numel() for p in model.parameters()))


# ── J-08: CandleNet 1m + 5m (F46) ────────────────────────────────────────────

def train_candlenet_1m(symbols: list[str]) -> None:
    """Blueprint F46 — CandleNet 1-minute model pre-training."""
    from ml.candlenet import train as _cn_train
    log.info("candlenet_1m_training_start",
             note="CNN-GRU on 1min OHLCV+HA+patterns, ~282K samples")
    result = _cn_train("1m", data_dir=DATA_DIR, models_dir=MODELS_DIR)
    log.info("candlenet_1m_training_done", **result)


def train_candlenet_5m(symbols: list[str]) -> None:
    """Blueprint F48 — CandleNet 5-minute model pre-training."""
    from ml.candlenet import train as _cn_train
    log.info("candlenet_5m_training_start",
             note="CNN+GRU+TCN+GAF on 5min OHLCV+HA+patterns, ~282K samples")
    result = _cn_train("5m", data_dir=DATA_DIR, models_dir=MODELS_DIR)
    log.info("candlenet_5m_training_done", **result)


def train_mae_pretrain_step(tf: str) -> None:
    """cont. 54 — P5: MAE pretraining warm-up for CandleNet (per-TF).
    Saves models/candlenet_mae_{tf}_encoder.pth. Failure is non-fatal —
    supervised CandleNet still works without the warm-start."""
    try:
        from ml.candlenet_mae import pretrain as _mae_pretrain
        log.info("mae_pretrain_start", tf=tf)
        result = _mae_pretrain(tf, DATA_DIR, n_epochs=6, batch_size=128)
        log.info("mae_pretrain_done", tf=tf, **(result or {}))
    except Exception as exc:
        log.warning("mae_pretrain_failed", tf=tf, error=str(exc)[:200])


def train_mamba_step(tf: str) -> None:
    """cont. 54 — P1: Mamba SSM forecaster (per-TF). Saves models/mamba_{tf}.pth.
    Failure non-fatal — signals/engine.py reads forecasts via mamba_forecaster
    which returns None when the model file is missing."""
    try:
        from ml.mamba_forecaster import train as _mamba_train
        log.info("mamba_train_start", tf=tf)
        result = _mamba_train(tf, DATA_DIR, n_epochs=12, batch_size=64)
        log.info("mamba_train_done", tf=tf, **(result or {}))
    except Exception as exc:
        log.warning("mamba_train_failed", tf=tf, error=str(exc)[:200])


def download_chronos_step() -> None:
    """cont. 54 — P2: download the Chronos-Bolt foundation model weights
    from Hugging Face. Failure non-fatal — foundation_forecast.predict()
    returns None when the model dir is missing."""
    try:
        from ml.foundation_forecast import download_weights as _dl_chronos
        log.info("chronos_download_start")
        result = _dl_chronos()
        log.info("chronos_download_done", **(result or {}))
    except Exception as exc:
        log.warning("chronos_download_failed", error=str(exc)[:200])


def pull_ollama_dsl_models_step() -> None:
    """cont. 55 — F54 LLM-DSL miner needs qwen2.5-coder:7b (proposer) and
    deepseek-r1:8b (validator) loaded into Ollama. Pulls via the Ollama
    HTTP API; skips any model already present per `/api/tags`. Writes the
    marker file on success. Failure is non-fatal — F54 logs a clear error
    and falls back to no-op when it tries to call a missing model."""
    import os, requests
    host = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
    needed = [
        os.environ.get("DSL_PROPOSER_MODEL",  "qwen2.5-coder:7b"),
        os.environ.get("DSL_VALIDATOR_MODEL", "deepseek-r1:8b"),
    ]
    # Discover what's already installed.
    have: set[str] = set()
    try:
        resp = requests.get(f"{host}/api/tags", timeout=15)
        if resp.status_code == 200:
            for m in resp.json().get("models", []) or []:
                if "name" in m:
                    have.add(m["name"])
    except Exception as exc:
        log.warning("ollama_tags_unreachable",
                    host=host, error=str(exc)[:200])
    log.info("ollama_pull_dsl_models_start", needed=needed, have=list(have))
    pulled = 0
    failed = 0
    for model in needed:
        if model in have:
            log.info("ollama_pull_skip_present", model=model)
            continue
        try:
            # /api/pull streams; we just wait for the connection to close.
            with requests.post(
                f"{host}/api/pull",
                json={"name": model, "stream": False},
                timeout=2700,  # 45min for 7B-8B Q4 GGUF over a slow link
            ) as resp:
                if resp.status_code == 200:
                    log.info("ollama_pull_ok", model=model)
                    pulled += 1
                else:
                    log.warning("ollama_pull_http_error", model=model,
                                code=resp.status_code, body=resp.text[:200])
                    failed += 1
        except Exception as exc:
            log.warning("ollama_pull_exception", model=model,
                        error=str(exc)[:200])
            failed += 1
    # Marker only on full success — if any model failed, retry next boot.
    if failed == 0:
        try:
            (MODELS_DIR / "ollama_models_pulled.marker").write_text(
                "\n".join(needed))
        except Exception:
            pass
    log.info("ollama_pull_dsl_models_done",
             pulled=pulled, skipped=len(have & set(needed)), failed=failed)


def train_candlenet_15m(symbols: list[str]) -> None:
    """F48 §Idea D — CandleNet 15-minute model pre-training (4-TF hierarchy)."""
    from ml.candlenet import train as _cn_train
    log.info("candlenet_15m_training_start",
             note="CNN+GRU+TCN+GAF on 15min OHLCV+HA+patterns")
    result = _cn_train("15m", data_dir=DATA_DIR, models_dir=MODELS_DIR)
    log.info("candlenet_15m_training_done", **result)


def train_entry_timing_agent_step() -> None:
    """F48 §Idea C — Pre-training step for the PPO entry-timing agent.

    The agent requires logged signal events (≥ 200) to train; the bot
    accumulates these only during live signal generation. At pretrainer time
    there is no history yet, so this step intentionally NO-OPs (logs only).
    The first real training happens via the weekly Celery beat
    `train_entry_timing_task` once the signal-event JSONL has been populated.
    """
    from ml.entry_timing_agent import train_entry_timing
    log.info("entry_timing_agent_step_start")
    result = train_entry_timing()
    log.info("entry_timing_agent_step_done", **result)


# ── J-11: Verification ───────────────────────────────────────────────────────

def verify_all_checkpoints() -> None:
    required = [
        MODELS_DIR / "hmm_regime.pkl",
        MODELS_DIR / "tft.pth",
        MODELS_DIR / "patchtst.pth",
        MODELS_DIR / "gnn.pth",
        MODELS_DIR / "world_model.pth",
        MODELS_DIR / "candlenet_1m.pth",
        MODELS_DIR / "candlenet_5m.pth",
        MODELS_DIR / "candlenet_15m.pth",
    ]
    # Llama 70B no longer required locally — runtime uses cloud failover chain
    # (Groq/Cerebras/SambaNova). See llm/researcher.py.
    missing = [str(p) for p in required if not p.exists()]

    if missing:
        sys.exit("ABORT — missing files:\n" + "\n".join(f"  {m}" for m in missing))

    log.info("all_checkpoints_verified", count=len(required))
    for p in required:
        log.info("checkpoint_ok", file=p.name, size_mb=round(p.stat().st_size / 1e6, 1))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("pretrainer_started", download_pairs=DOWNLOAD_PAIRS, active_trading_pairs=200)

    import db
    db.init_pool()

    from exchange.client import BinanceClient
    client = BinanceClient()

    all_symbols = client.get_all_usdt_futures_symbols()
    log.info("total_usdt_m_pairs_available", count=len(all_symbols))

    # Download data for DOWNLOAD_PAIRS (150+) — more training data = better models
    # Bot will only actively trade top 60 selected by scanner
    symbols = all_symbols[:DOWNLOAD_PAIRS]
    log.info("pretraining_on_pairs", count=len(symbols))

    # cont. 51 — skip-if-complete guards. The user requested a ~1h
    # pretrainer run. Steps 1-6 already have their artifacts on disk from
    # earlier sessions (hmm_regime.pkl / tft.pth / patchtst.pth / gnn.pth /
    # world_model.pth). Re-running them costs ~30-45min for zero benefit.
    # Steps 7-9 (CandleNet 1m/5m/15m) and step 10 (entry timing) are the
    # only ones that NEED to run — those `.pth` files don't exist yet.
    # Override: env var `PRETRAINER_FORCE_REDO=1` re-runs everything from
    # scratch.
    import os as _os
    _force = _os.environ.get("PRETRAINER_FORCE_REDO") == "1"

    def _skip_or_run(step_label: str, checkpoint_path, run_fn, *args):
        if not _force and checkpoint_path is not None and checkpoint_path.exists():
            log.info(f"{step_label}_skipped",
                     reason="checkpoint_already_exists",
                     file=str(checkpoint_path.name),
                     size_mb=round(checkpoint_path.stat().st_size / 1e6, 1))
            return
        log.info(step_label)
        run_fn(*args)

    # Step 1 — OHLCV download. Skip if historical CSV directory already has
    # the expected file count (DOWNLOAD_PAIRS × len(INTERVALS) timeframes).
    # CSVs live at data/historical/<PAIR>/<INTERVAL>.csv so we recurse.
    _csv_dir = DATA_DIR
    _expected_files = DOWNLOAD_PAIRS * len(INTERVALS)
    try:
        _existing = sum(1 for _p in _csv_dir.rglob("*.csv")) if _csv_dir.exists() else 0
    except Exception:
        _existing = 0
    if not _force and _existing >= _expected_files * 0.9:
        log.info("step_1_downloading_ohlcv_skipped",
                 reason=f"{_existing}/{_expected_files} CSV files already present")
    else:
        log.info("step_1_downloading_ohlcv",
                 note=f"~{DOWNLOAD_PAIRS * len(INTERVALS)} files across {len(INTERVALS)} timeframes")
        download_ohlcv(client, symbols)

    _skip_or_run("step_2_training_hmm",
                 MODELS_DIR / "hmm_regime.pkl", train_hmm, symbols)
    _skip_or_run("step_3_saving_tft_architecture",
                 MODELS_DIR / "tft.pth", train_tft, symbols)
    _skip_or_run("step_4_saving_patchtst_architecture",
                 MODELS_DIR / "patchtst.pth", train_patchtst, symbols)
    _skip_or_run("step_5_training_gnn_with_correlation",
                 MODELS_DIR / "gnn.pth", train_gnn, symbols)
    _skip_or_run("step_6_saving_world_model_architecture",
                 MODELS_DIR / "world_model.pth", train_world_model, symbols)

    _skip_or_run("step_7_training_candlenet_1m",
                 MODELS_DIR / "candlenet_1m.pth", train_candlenet_1m, symbols)
    _skip_or_run("step_8_training_candlenet_5m",
                 MODELS_DIR / "candlenet_5m.pth", train_candlenet_5m, symbols)
    _skip_or_run("step_9_training_candlenet_15m",
                 MODELS_DIR / "candlenet_15m.pth", train_candlenet_15m, symbols)

    # Step 10 entry-timing agent is already a no-op when there's no
    # signal-event history; safe to keep unconditional.
    log.info("step_10_training_entry_timing_agent")
    train_entry_timing_agent_step()

    # cont. 54 — P1/P2/P5 training steps. P6 (Evolving Multiscale GNN) is
    # inference-only; no pretraining step needed.

    # Step 11 — P5: MAE pretraining warm-up for CandleNet (per-TF encoders).
    # These are OPTIONAL warm-starts; supervised CandleNet still runs even if
    # MAE encoders don't exist. Skip if encoder already saved.
    for _tf in ("1m", "5m", "15m"):
        _enc_path = MODELS_DIR / f"candlenet_mae_{_tf}_encoder.pth"
        _skip_or_run(f"step_11_mae_pretrain_{_tf}", _enc_path,
                     train_mae_pretrain_step, _tf)

    # Step 12 — P1: Mamba SSM forecaster per-TF training. Same skip-if-exists
    # pattern as the supervised CandleNet steps.
    for _tf in ("1m", "5m", "15m", "1h"):
        _mamba_path = MODELS_DIR / f"mamba_{_tf}.pth"
        _skip_or_run(f"step_12_train_mamba_{_tf}", _mamba_path,
                     train_mamba_step, _tf)

    # Step 13 — P2: Chronos-2 foundation model download (Hugging Face).
    # No supervised training needed — zero-shot model. ~1 GB on disk.
    _chronos_marker = MODELS_DIR / "chronos_bolt_base" / "config.json"
    _skip_or_run("step_13_download_chronos", _chronos_marker,
                 download_chronos_step)

    # Step 14 — F54 (cont. 55): pull Ollama models for the LLM-DSL alpha
    # miner. qwen2.5-coder:7b proposer + deepseek-r1:8b validator. Skip if
    # already present — Ollama tracks model state itself; we read the
    # /api/tags response and only pull what's missing.
    _ollama_marker = MODELS_DIR / "ollama_models_pulled.marker"
    _skip_or_run("step_14_pull_ollama_dsl_models", _ollama_marker,
                 pull_ollama_dsl_models_step)

    log.info("step_99_verifying_checkpoints")
    verify_all_checkpoints()

    log.info("pretrainer_complete",
             message="All models ready. Bot can now start paper trading.",
             next_step="docker compose up -d")


if __name__ == "__main__":
    main()
