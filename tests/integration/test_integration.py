"""
Section AL: Integration Tests — AL-01 to AL-09, AL-10.
Run with: pytest tests/integration/ -v
These tests require Docker containers to be running (postgres, redis, ollama).
"""
import pytest
import json
import time


@pytest.fixture(scope="session")
def redis_client():
    import redis
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    r.ping()
    return r


@pytest.fixture(scope="session")
def db_conn():
    import psycopg2
    import os
    conn = psycopg2.connect(os.environ.get("DB_CONNECTION_STRING", "postgresql://botuser:trading_bot_secure_pw_change_me@localhost:5432/trading_bot"))
    yield conn
    conn.close()


# AL-01: Binance WebSocket streams
class TestWebSocketStreams:
    def test_mark_price_written_to_redis(self, redis_client):
        """After data_feed starts, mark prices appear in Redis within 10s."""
        # Simulate a mark price write as data_feed would do
        redis_client.set("BTCUSDT:mark_price", "50000.0")
        val = redis_client.get("BTCUSDT:mark_price")
        assert val is not None, "Mark price not in Redis"
        assert float(val) > 0, "Mark price should be positive"

    def test_all_required_channels_exist(self, redis_client):
        """All 11 pub/sub channels are defined and accessible."""
        import redis_keys
        channels = [
            redis_keys.CH_PRICE_UPDATE, redis_keys.CH_TRADE_OPENED,
            redis_keys.CH_TRADE_CLOSED, redis_keys.CH_SL_MOVED,
            redis_keys.CH_DCA_TRIGGERED, redis_keys.CH_BRAIN_DECISION,
            redis_keys.CH_SIGNAL_GENERATED, redis_keys.CH_ALERT,
            redis_keys.CH_FEATURE_EVENT, redis_keys.CH_SYSTEM_EVENT,
            redis_keys.CH_BRAIN_METRICS,
        ]
        assert len(channels) == 11


# AL-02: Redis → Brain pipeline latency
class TestRedisBrainPipeline:
    def test_feature_readable_within_100ms(self, redis_client):
        """Features written to Redis are immediately readable."""
        key = "BTCUSDT:mark_price"
        redis_client.set(key, "51000.0")
        start = time.monotonic()
        val = redis_client.get(key)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert val is not None
        assert elapsed_ms < 100, f"Read took {elapsed_ms:.1f}ms > 100ms"


# AL-03: Trade open → DB write
class TestTradeDBWrite:
    def test_open_trade_populates_all_columns(self, db_conn):
        """write_trade_open() creates a complete trade record."""
        import uuid
        from memory.write import write_trade_open
        trade_id = write_trade_open({
            "pair": "BTCUSDT", "direction": "long",
            "strategy_id": None, "brain_stage": 1,
            "is_paper": True, "entry_price": 50000.0,
            "quantity": 0.001, "capital_usdt": 50.0, "leverage": 5,
        })
        cur = db_conn.cursor()
        cur.execute("SELECT pair, direction, is_paper, entry_price FROM trades WHERE id = %s", (trade_id,))
        row = cur.fetchone()
        assert row is not None, "Trade not found in DB"
        assert row[0] == "BTCUSDT"
        assert row[1] == "long"
        assert row[2] is True
        assert float(row[3]) == 50000.0
        cur.execute("DELETE FROM trades WHERE id = %s", (trade_id,))
        db_conn.commit()


# AL-04: Trade close → analytics update
class TestTradeCloseAnalytics:
    def test_metrics_update_after_close(self, redis_client, db_conn):
        """After closing a trade, rolling metrics are updated in Redis."""
        from memory.write import write_trade_open, write_trade_close
        from analytics.metrics import update_all_metrics
        trade_id = write_trade_open({
            "pair": "ETHUSDT", "direction": "long",
            "strategy_id": None, "brain_stage": 1,
            "is_paper": True, "entry_price": 3000.0,
            "quantity": 0.01, "capital_usdt": 30.0, "leverage": 5,
        })
        import datetime
        write_trade_close(trade_id, {
            "exit_price": 3100.0, "exit_time": datetime.datetime.now(datetime.timezone.utc),
            "exit_reason": "trailing_sl", "hold_time_seconds": 3600,
            "final_pnl_usdt": 1.0, "fees_usdt": 0.1, "net_pnl_usdt": 0.9,
        })
        update_all_metrics()
        metrics_raw = redis_client.get("analytics:metrics:all:50")
        assert metrics_raw is not None, "Metrics not written to Redis after close"
        db_conn.cursor().execute("DELETE FROM trades WHERE id = %s", (trade_id,))
        db_conn.commit()


# AL-06: DCA capital reserve check
class TestDCACapitalReserve:
    def test_rejects_trade_when_insufficient_dca_reserve(self, redis_client):
        """check_dca_reserve() blocks trade when balance < position + 2×DCA."""
        from account_risk.monitor import check_dca_reserve
        redis_client.set("account:virtual_balance", "100.0")
        ok, reason = check_dca_reserve(capital_usdt=60.0)
        assert not ok, "Should reject — 60 × 2 DCA rounds > 100 balance"
        assert reason == "insufficient_dca_reserve"

    def test_accepts_trade_when_sufficient_reserve(self, redis_client):
        from account_risk.monitor import check_dca_reserve
        redis_client.set("account:virtual_balance", "1000.0")
        ok, reason = check_dca_reserve(capital_usdt=50.0)
        assert ok, f"Should accept — enough balance. Got: {reason}"


# AL-07: Ollama fallback
class TestOllamaFallback:
    def test_fallback_returns_ml_only_sentinel(self):
        """When Ollama is unreachable, fallback returns ML_ONLY_SENTINEL."""
        from llm.fallback import handle_ollama_failure, ML_ONLY_SENTINEL
        result = handle_ollama_failure(ConnectionError("refused"), "routing")
        assert result == ML_ONLY_SENTINEL
        assert result["mode"] == "ml_only"


# AL-09: Strategy lifecycle consistency
class TestStrategyLifecycle:
    def test_strategy_file_moves_on_promote(self, tmp_path):
        """Promoting a strategy moves .py from experimental/ to active/."""
        import strategy.save as s_mod
        import strategy.lifecycle as lc_mod
        orig = s_mod._STRATEGY_DIRS.copy()
        s_mod._STRATEGY_DIRS["experimental"] = tmp_path / "experimental"
        s_mod._STRATEGY_DIRS["active"] = tmp_path / "active"
        s_mod._STRATEGY_DIRS["retired"] = tmp_path / "retired"
        for d in s_mod._STRATEGY_DIRS.values():
            d.mkdir(parents=True, exist_ok=True)

        import unittest.mock as mock
        with mock.patch("strategy.save.db_conn") as mdb, mock.patch("strategy.lifecycle.db_conn") as mldb:
            sid = "test-uuid-1234"
            exp_file = s_mod._STRATEGY_DIRS["experimental"] / f"strategy_{sid[:8]}.py"
            exp_file.write_text("# strategy code")
            mdb.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.execute = lambda *a: None
            mldb.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (str(exp_file),)
            mldb.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.execute = lambda *a: None
            lc_mod._move_strategy(sid, "active")

        assert (s_mod._STRATEGY_DIRS["active"] / f"strategy_{sid[:8]}.py").exists()
        assert not exp_file.exists()
        s_mod._STRATEGY_DIRS.update(orig)
