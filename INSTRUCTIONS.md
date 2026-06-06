# AI Crypto Trading Bot — Setup & Deploy Instructions

Complete guide to set up the bot from scratch on a new VPS.

---

## 1. Server Requirements

- **OS**: Ubuntu 22.04 / Debian 12
- **RAM**: 16 GB minimum (32 GB recommended)
- **CPU**: 4+ cores
- **Disk**: 100 GB+ SSD
- **Open port**: 80 (dashboard via nginx)

---

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker --version   # verify
```

---

## 3. Clone the Code

```bash
sudo mkdir -p /opt/trading-bot
sudo chown $USER:$USER /opt/trading-bot
git clone https://github.com/ajith4134/ai-crypto-trading-bot.git /opt/trading-bot
cd /opt/trading-bot
```

---

## 4. Create the .env File

```bash
cp .env.example .env
nano .env
```

Fill in every value:

| Variable | Description |
|----------|-------------|
| `BINANCE_API_KEY` | Binance API key (testnet or live) |
| `BINANCE_API_SECRET` | Binance API secret |
| `BINANCE_TESTNET` | `true` = testnet, `false` = live Binance |
| `TRADING_MODE` | **Always start with `paper`** — change to `live` only when ready |
| `POSTGRES_PASSWORD` | Pick a strong password (e.g. `MySecurePass2026`) |
| `DB_CONNECTION_STRING` | `postgresql://botuser:SAME_PASSWORD@postgres:5432/trading_bot` |
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram (optional but recommended) |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID |
| `MAX_ACTIVE_PAIRS` | Start with `60`, max `200` |
| `OLLAMA_RESEARCH_MODEL` | `qwen2.5-coder:7b` |

> **CRITICAL**: Keep `TRADING_MODE=paper` until you have verified the bot is working correctly. Real money trades happen when `TRADING_MODE=live` AND `BINANCE_TESTNET=false`.

---

## 5. Create Required Directories

```bash
mkdir -p /opt/trading-bot/models
mkdir -p /opt/trading-bot/data/historical
mkdir -p /opt/trading-bot/logs
mkdir -p /opt/trading-bot/hf_cache
```

---

## 6. Build the Docker Image

> This takes 20–40 minutes the first time (downloads PyTorch + CUDA layers, ~34 GB image).

```bash
cd /opt/trading-bot
docker compose build
```

Check disk space first — you need at least 50 GB free:
```bash
df -h /
```

If disk is tight, clean Docker cache first:
```bash
docker builder prune -af
```

---

## 7. Start Infrastructure Services First

Start Postgres, Redis, and Ollama before the bot:

```bash
docker compose up -d postgres redis ollama
```

Wait 30 seconds, then verify they are healthy:
```bash
docker compose ps
```

---

## 8. Pull Ollama Models

These are the LLM models the bot uses locally (downloads ~15 GB total):

```bash
docker exec trading-bot-ollama-1 ollama pull qwen2.5-coder:7b
docker exec trading-bot-ollama-1 ollama pull nomic-embed-text:latest
docker exec trading-bot-ollama-1 ollama pull qwen2.5:3b
docker exec trading-bot-ollama-1 ollama pull llama3.2:3b
docker exec trading-bot-ollama-1 ollama pull deepseek-r1:8b
docker exec trading-bot-ollama-1 ollama pull mistral:7b
docker exec trading-bot-ollama-1 ollama pull phi3:mini
docker exec trading-bot-ollama-1 ollama pull tinyllama:latest
```

Verify models are ready:
```bash
docker exec trading-bot-ollama-1 ollama list
```

---

## 9. Run Database Migrations

```bash
docker compose run --rm brain python -m migrations.run 2>/dev/null || \
docker compose run --rm brain alembic upgrade head
```

---

## 10. Start All Services

```bash
docker compose up -d
```

Check all containers are running (none should be `Restarting` after 60 seconds):
```bash
docker compose ps
```

Expected services running:
- `brain` — main trading engine
- `celery_beat` — scheduled tasks
- `celery_worker` — task processor
- `celery_worker_candlenet` — ML training worker
- `celery_worker_cn_train` — candlenet trainer
- `dashboard` — web API
- `data_feed` — market data producer
- `kline_ws` — candlestick websocket
- `liq_ws` — liquidation websocket
- `micro_ws` — microstructure websocket
- `nginx` — reverse proxy (port 80)
- `ollama` — local LLM server
- `postgres` — database
- `redis` — cache & pub/sub
- `scanner` — pair scanner
- `watchdog` — health monitor
- `web_intel` — web intelligence

---

## 11. Verify the Bot is Working

```bash
# Check brain logs — should show "Starting SOAR loop" with no ERROR lines
docker compose logs brain --tail=50

# Check Redis is receiving market data
docker exec trading-bot-redis-1 redis-cli keys "kline:*" | head -5

# Check database has tables
docker exec trading-bot-postgres-1 psql -U postgres trading_bot -c "\dt" | head -20
```

---

## 12. Access the Dashboard

Open in your browser:
```
http://YOUR_VPS_IP
```

You should see the trading dashboard with pair scanner, open trades, and controls.

---

## 13. Configure Trading Mode

### Paper Trading (default — no real money)
```bash
# Already set in .env: TRADING_MODE=paper
# Confirm in Redis:
docker exec trading-bot-redis-1 redis-cli get bot:mode
```

### Switch to Live Trading (real money — do this carefully)
Only after paper trading works correctly:
1. Edit `.env` — set `TRADING_MODE=live` and `BINANCE_TESTNET=false`
2. Make sure your Binance API key has **Futures trading enabled**
3. Recreate the brain container (routing is set at startup):
```bash
docker compose up -d --no-deps --force-recreate brain
```
4. Verify in logs: `docker compose logs brain --tail=20 | grep -i "mode\|live"`

> **WARNING**: The dashboard "Mode" toggle is cosmetic only. Real routing is controlled by `TRADING_MODE` in `.env`. Always change `.env` and recreate the brain.

---

## 14. Key Redis Controls

Run these from the VPS to control the bot at runtime:

```bash
# Start/stop trading signals
docker exec trading-bot-redis-1 redis-cli set bot:running 1   # start
docker exec trading-bot-redis-1 redis-cli set bot:running 0   # stop

# Check current leverage (default 5x — do not increase without testing)
docker exec trading-bot-redis-1 redis-cli get risk:leverage

# Set leverage (recommended max: 5x)
docker exec trading-bot-redis-1 redis-cli set risk:leverage 5
```

---

## 15. Useful Commands

```bash
# View logs of any service
docker compose logs brain -f
docker compose logs celery_worker -f

# Restart a single service
docker compose restart brain

# Full restart
docker compose down && docker compose up -d

# Check disk usage
df -h /
docker system df

# Free up Docker build cache (if disk is low)
docker builder prune -af
```

---

## 16. Data That Does NOT Come from GitHub

These must be re-generated or transferred from the old VPS manually:

| Data | Location | How to restore |
|------|----------|----------------|
| Trade history | Postgres `trading_bot` DB | `pg_dump` → `psql restore` |
| Redis state | `trading-bot_redis_data` volume | Copy `dump.rdb` |
| Trained ML models | `/opt/trading-bot/models/` | `rsync` from old VPS |
| Kline corpus | `/opt/trading-bot/data/historical/` | `rsync` from old VPS |
| HuggingFace models | `/opt/trading-bot/hf_cache/` | Auto-downloaded on first use |

To rsync data from old VPS (run on OLD VPS):
```bash
rsync -avz --progress /opt/trading-bot/models/ root@NEW_VPS_IP:/opt/trading-bot/models/
rsync -avz --progress /opt/trading-bot/data/historical/ root@NEW_VPS_IP:/opt/trading-bot/data/historical/
```

---

## 17. Retraining ML Models From Scratch

If you cannot rsync models from the old VPS, follow these steps in order.
**The bot can trade without ML models** (rule-based signals still work), but retraining restores full prediction power.

### Step 1 — Download the Kline Corpus (historical candle data)

This downloads 1m/5m/15m/30m/1h candles for all active pairs from data.binance.vision (static CDN — no rate limits, no bans). Takes 30–90 minutes depending on VPS speed.

```bash
# Make sure the corpus directory is owned by uid 999 (container user)
sudo chown -R 999:999 /opt/trading-bot/data/historical/

# Run the deep backfill inside the cn_train container (has corpus mounts)
docker compose exec celery_worker_cn_train python -m ml.klines_corpus backfill --pairs active
```

Check progress — it will log each pair/timeframe as it downloads:
```bash
docker compose logs celery_worker_cn_train -f
```

### Step 2 — Retrain the XGBoost Kline Predictor

This is the main directional prediction model (predict-all). Trigger it manually:

```bash
docker compose exec celery_worker_cn_train python -c "
from celery_app import retrain_kline_predictor_task
retrain_kline_predictor_task()
print('Done')
"
```

Takes ~10–30 minutes. When complete you will see model files appear in `/opt/trading-bot/models/`.

### Step 3 — Retrain CandleNet Models (all timeframes)

CandleNet is a neural network trained per-timeframe. Trigger all 5 retrains:

```bash
docker compose exec celery_worker_candlenet python -c "
from celery_app import retrain_candlenet_1m, retrain_candlenet_5m, retrain_candlenet_15m, retrain_candlenet_30m, retrain_candlenet_1h
retrain_candlenet_1m()
retrain_candlenet_5m()
retrain_candlenet_15m()
retrain_candlenet_30m()
retrain_candlenet_1h()
print('Done')
"
```

Each takes 5–20 minutes. The weekly scheduler will also retrain automatically every Sunday.

### Step 4 — Retrain Other Models (HMM, MARL)

These run automatically on schedule, but can be triggered manually:

```bash
docker compose exec celery_worker python -c "
from celery_app import retrain_hmm, retrain_marl_day
retrain_hmm()
retrain_marl_day()
print('Done')
"
```

### Step 5 — Verify Models Loaded

```bash
# Check models directory has files
ls -lh /opt/trading-bot/models/

# Check Redis for prediction output (means model is running)
docker exec trading-bot-redis-1 redis-cli keys "prediction:*" | head -10

# Check brain logs for model load confirmation
docker compose logs brain --tail=30 | grep -i "model\|predict\|candlenet"
```

### Retraining Timeline Summary

| Model | Time | Command container |
|-------|------|-------------------|
| Kline corpus download | 30–90 min | `celery_worker_cn_train` |
| XGBoost kline predictor | 10–30 min | `celery_worker_cn_train` |
| CandleNet 1m | 5–20 min | `celery_worker_candlenet` |
| CandleNet 5m/15m/30m/1h | 5–20 min each | `celery_worker_candlenet` |
| HMM + MARL | 5–15 min | `celery_worker` |
| **Total from scratch** | **~3–5 hours** | |

> After retraining, the bot auto-uses new models. No restart needed.

---

## 18. Troubleshooting

| Problem | Fix |
|---------|-----|
| Container keeps restarting | `docker compose logs SERVICE_NAME --tail=50` |
| Dashboard blank / shows old data | Rebuild frontend: `docker compose build dashboard && docker compose up -d dashboard` |
| No trades opening | Check `bot:running` in Redis + check brain logs for gate rejections |
| Binance API errors | Verify API key has Futures enabled + correct testnet/live setting |
| Disk full during build | `docker builder prune -af` then rebuild |
| Port 80 not accessible | Check VPS firewall: `sudo ufw allow 80` |
