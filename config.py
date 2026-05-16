import os
from pathlib import Path
from dotenv import load_dotenv
import yaml

load_dotenv()

_REQUIRED_ENV = [
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "DB_CONNECTION_STRING",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

for _key in _REQUIRED_ENV:
    if not os.getenv(_key):
        raise EnvironmentError(f"Missing required environment variable: {_key}")

# --- Environment variables ---

BINANCE_API_KEY: str = os.environ["BINANCE_API_KEY"]
BINANCE_API_SECRET: str = os.environ["BINANCE_API_SECRET"]
BINANCE_TESTNET: bool = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")
BRAIN_STAGE: int = int(os.getenv("BRAIN_STAGE", "1"))
PAPER_TRADE_THRESHOLD: int = int(os.getenv("PAPER_TRADE_THRESHOLD", "2000"))
MAX_ACTIVE_PAIRS: int = int(os.getenv("MAX_ACTIVE_PAIRS", "60"))

REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))

DB_CONNECTION_STRING: str = os.environ["DB_CONNECTION_STRING"]

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID: str = os.environ["TELEGRAM_CHAT_ID"]

SERPAPI_KEY: str = os.getenv("SERPAPI_KEY", "")
REDDIT_CLIENT_ID: str = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET: str = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT: str = os.getenv("REDDIT_USER_AGENT", "")
ETHERSCAN_API_KEY: str = os.getenv("ETHERSCAN_API_KEY", "")
COINGECKO_API_KEY: str = os.getenv("COINGECKO_API_KEY", "")
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

# --- config.yaml ---

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
if not _CONFIG_PATH.exists():
    raise FileNotFoundError(f"config.yaml not found at {_CONFIG_PATH}")

with open(_CONFIG_PATH) as _f:
    _cfg = yaml.safe_load(_f)


def _get(d: dict, *keys):
    """Traverse nested dict; raise ValueError if any key is missing."""
    current = d
    path = []
    for k in keys:
        path.append(k)
        if not isinstance(current, dict) or k not in current:
            raise ValueError(f"Missing required config.yaml key: {'.'.join(path)}")
        current = current[k]
    return current


class trading:
    mode: str = _get(_cfg, "trading", "mode")
    paper_trade_threshold: int = _get(_cfg, "trading", "paper_trade_threshold")
    max_active_pairs: int = _get(_cfg, "trading", "max_active_pairs")
    min_open_trades: int = _get(_cfg, "trading", "min_open_trades")
    max_open_trades: int = _get(_cfg, "trading", "max_open_trades")
    max_total_capital_pct: int = _get(_cfg, "trading", "max_total_capital_pct")


class capital:
    per_trade_min_pct: int = _get(_cfg, "capital", "per_trade_min_pct")
    per_trade_max_pct: int = _get(_cfg, "capital", "per_trade_max_pct")
    leverage_min: int = _get(_cfg, "capital", "leverage_min")
    leverage_max: int = _get(_cfg, "capital", "leverage_max")
    dca_trigger_1_pct: int = _get(_cfg, "capital", "dca_trigger_1_pct")
    dca_trigger_2_pct: int = _get(_cfg, "capital", "dca_trigger_2_pct")
    dca_rounds_max: int = _get(_cfg, "capital", "dca_rounds_max")


class risk:
    daily_loss_limit_pct: int = _get(_cfg, "risk", "daily_loss_limit_pct")
    max_drawdown_pct: int = _get(_cfg, "risk", "max_drawdown_pct")
    circuit_breaker_live_only: bool = _get(_cfg, "risk", "circuit_breaker_live_only")


class brain:
    evolution_stage: int = _get(_cfg, "brain", "evolution_stage")
    stage_1_to_2_trades: int = _get(_cfg, "brain", "stage_1_to_2_trades")
    stage_2_to_3_trades: int = _get(_cfg, "brain", "stage_2_to_3_trades")
    stage_2_to_3_winrate: int = _get(_cfg, "brain", "stage_2_to_3_winrate")
    stage_3_to_4_trades: int = _get(_cfg, "brain", "stage_3_to_4_trades")
    stage_3_to_4_winrate: int = _get(_cfg, "brain", "stage_3_to_4_winrate")
    stage_3_to_4_sharpe: float = _get(_cfg, "brain", "stage_3_to_4_sharpe")
    live_unlock_trades: int = _get(_cfg, "brain", "live_unlock_trades")
    opro_window_size: int = _get(_cfg, "brain", "opro_window_size")
    code_rewrite_interval_trades: int = _get(_cfg, "brain", "code_rewrite_interval_trades")


class curiosity:
    exploration_budget_pct: int = _get(_cfg, "curiosity", "exploration_budget_pct")


class strategies:
    trial_min_trades: int = _get(_cfg, "strategies", "trial_min_trades")
    trial_min_days: int = _get(_cfg, "strategies", "trial_min_days")
    counterfactual_window_hours: int = _get(_cfg, "strategies", "counterfactual_window_hours")


class scanner:
    rescan_interval_hours: int = _get(_cfg, "scanner", "rescan_interval_hours")
    criteria_weights: dict = _get(_cfg, "scanner", "criteria_weights")


class llm:
    ollama_url: str = _get(_cfg, "llm", "ollama_url")
    router_model: str = _get(_cfg, "llm", "router_model")
    decision_model: str = _get(_cfg, "llm", "decision_model")
    airllm_model_path: str = _get(_cfg, "llm", "airllm_model_path")
    airllm_tasks: list = _get(_cfg, "llm", "airllm_tasks")
    fallback_to_ml_on_llm_failure: bool = _get(_cfg, "llm", "fallback_to_ml_on_llm_failure")
    no_reflection_enforced: bool = _get(_cfg, "llm", "no_reflection_enforced")


class memory:
    fast_memory_size: int = _get(_cfg, "memory", "fast_memory_size")
    sleep_consolidation_hour: int = _get(_cfg, "memory", "sleep_consolidation_hour")
    opro_evaluation_window: int = _get(_cfg, "memory", "opro_evaluation_window")


class web_intelligence:
    serpapi_searches_per_day: int = _get(_cfg, "web_intelligence", "serpapi_searches_per_day")
    reddit_fetch_interval_minutes: int = _get(_cfg, "web_intelligence", "reddit_fetch_interval_minutes")
    rss_fetch_interval_minutes: int = _get(_cfg, "web_intelligence", "rss_fetch_interval_minutes")
    github_fetch_interval_hours: int = _get(_cfg, "web_intelligence", "github_fetch_interval_hours")
    source_credibility_min_signals: int = _get(_cfg, "web_intelligence", "source_credibility_min_signals")


class notifications:
    critical_alert_immediate: bool = _get(_cfg, "notifications", "critical_alert_immediate")
    trade_alert_enabled: bool = _get(_cfg, "notifications", "trade_alert_enabled")
    daily_summary_hour_utc: int = _get(_cfg, "notifications", "daily_summary_hour_utc")


class logging_cfg:
    level: str = _get(_cfg, "logging", "level")
    log_to_file: bool = _get(_cfg, "logging", "log_to_file")
    log_dir: str = _get(_cfg, "logging", "log_dir")
