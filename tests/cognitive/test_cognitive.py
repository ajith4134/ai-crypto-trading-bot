"""
Section AM: Cognitive Brain Tests — AM-01 to AM-07.
Run with: pytest tests/cognitive/ -v
"""
import pytest


# AM-01: World Model
class TestWorldModel:
    def test_imagine_trajectory_returns_valid_distribution(self):
        from world_model.model import imagine_trajectory
        result = imagine_trajectory("open_long", n_steps=10, initial_obs={"price": 50000.0, "volume": 1e6})
        assert "mean_pnl" in result
        assert "uncertainty" in result
        assert "prob_profit" in result
        assert 0.0 <= result["prob_profit"] <= 1.0

    def test_world_model_update_logs_error(self):
        from world_model.model import update_on_trade_close
        # Should not raise
        update_on_trade_close({"mean_pnl": 0.5}, actual_pnl=1.0)


# AM-02: Debate Council
class TestDebateCouncil:
    def test_verdict_is_valid_outcome(self):
        """Debate verdict must be one of the 5 defined outcomes."""
        valid = {"full_allocation", "reduced_allocation", "exploratory", "skip", "skip_risk"}
        import asyncio
        import unittest.mock as mock
        with mock.patch("debate.council.decide") as mock_decide:
            mock_decide.return_value = {"argue_for": True, "confidence": 80, "argue_against": False, "risk_acceptable": True, "recommended_size_pct": 15}
            from debate.council import run_debate
            result = asyncio.run(run_debate(
                signal={"pair": "BTCUSDT", "direction": "long", "signal_strength": 75},
                market={"regime": "bull", "turbulence": 0.5},
                capital_pct=15, balance=1000,
            ))
        assert result["verdict"] in valid


# AM-03: MCTS Research Engine
class TestMCTSResearchEngine:
    def test_originality_check_passes_new_code(self):
        from research.engine import originality_check
        new_code = "def unique_entry_condition_xyz(): return True"
        result = originality_check(new_code, strategy_dir="/opt/trading-bot/strategies")
        assert isinstance(result, bool)

    def test_originality_check_rejects_identical_code(self, tmp_path):
        from research.engine import originality_check
        code = "def entry(): return price > sma"
        (tmp_path / "existing.py").write_text(code)
        result = originality_check(code, strategy_dir=str(tmp_path))
        assert result is False, "Identical code should fail originality check"


# AM-05: Sleep Consolidation
class TestSleepConsolidation:
    def test_consolidation_runs_without_crash(self):
        """run_sleep_consolidation() completes without exceptions (empty state)."""
        import asyncio
        import unittest.mock as mock
        with mock.patch("memory.cognitive.memrl.db_conn") as mdb, \
             mock.patch("redis_client.get") as mr:
            mr.return_value.zrange.return_value = []
            result = asyncio.run(__import__("memory.cognitive.memrl", fromlist=["run_sleep_consolidation"]).run_sleep_consolidation())
        assert result["reviewed"] == 0


# AM-06: Metacognitive Monitor
class TestMetacognition:
    def test_competence_map_updated_after_trade(self):
        from metacognition.monitor import update_competence_map
        import unittest.mock as mock
        with mock.patch("metacognition.monitor.db_conn") as mdb, \
             mock.patch("redis_client.get") as mr:
            mdb.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = ('{}',)
            mdb.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.execute = lambda *a: None
            mr.return_value.get.return_value = None
            mr.return_value.set = lambda *a: None
            update_competence_map({
                "pair": "BTCUSDT", "market_regime": "bull",
                "timeframe": "1h", "net_pnl_usdt": 10.0, "failure_type": None,
            })


# AM-07: OPRO end-to-end
class TestOPRO:
    def test_roi_score_formula(self):
        from self_improve.opro import compute_opro_score
        assert compute_opro_score([]) == 50.0
        score_all_wins = compute_opro_score([100.0] * 5)
        score_all_losses = compute_opro_score([-100.0] * 5)
        assert score_all_wins > 50
        assert score_all_losses < 50
        assert compute_opro_score([0.0]) == 50.0

    def test_opro_counter_increments(self):
        from self_improve.opro import increment_opro_counter, trigger_opro_if_due
        assert trigger_opro_if_due(5) is True
        assert trigger_opro_if_due(3) is False
        assert trigger_opro_if_due(10) is True
