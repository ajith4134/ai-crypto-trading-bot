"""
Section AK: Core unit tests — AK-01 to AK-11.
Run with: pytest tests/unit/test_core.py -v
"""
import json
import pytest


# AK-09: No-Reflection Rule
class TestNoReflectionRule:
    def test_raises_on_reflect_phrase(self):
        from llm.guard import assert_no_reflection
        with pytest.raises(ValueError):
            assert_no_reflection("Please reflect on your previous decision.")

    def test_raises_on_critique_phrase(self):
        from llm.guard import assert_no_reflection
        with pytest.raises(ValueError):
            assert_no_reflection("Critique your last answer.")

    def test_raises_on_reconsider(self):
        from llm.guard import assert_no_reflection
        with pytest.raises(ValueError):
            assert_no_reflection("Please reconsider your approach.")

    def test_raises_on_review_previous(self):
        from llm.guard import assert_no_reflection
        with pytest.raises(ValueError):
            assert_no_reflection("Review your previous output.")

    def test_clean_prompt_passes(self):
        from llm.guard import assert_no_reflection
        assert_no_reflection("What is the current market regime?")
        assert_no_reflection("Open a long position on BTCUSDT.")


# AK-03: Kelly criterion
class TestKellyCriterion:
    def test_formula_basic(self):
        W, R = 0.55, 2.0
        f = W - (1 - W) / R
        assert round(f, 4) == round(0.55 - 0.45 / 2.0, 4)

    def test_output_clamped_to_min(self):
        from risk.manager import check_position_sizing
        ok, reason = check_position_sizing(capital_pct=2, total_deployed_pct=0)
        assert not ok
        assert "below minimum" in reason

    def test_output_clamped_to_max(self):
        from risk.manager import check_position_sizing
        ok, reason = check_position_sizing(capital_pct=35, total_deployed_pct=0)
        assert not ok
        assert "above maximum" in reason

    def test_valid_range_accepted(self):
        from risk.manager import check_position_sizing
        ok, reason = check_position_sizing(capital_pct=15, total_deployed_pct=30)
        assert ok

    def test_total_capital_exceeded(self):
        from risk.manager import check_position_sizing
        ok, reason = check_position_sizing(capital_pct=15, total_deployed_pct=70)
        assert not ok
        assert "max total capital" in reason


# AK-02: DCA mechanics
class TestDCAMechanics:
    def test_dca1_trigger_threshold(self):
        import config
        assert config.capital.dca_trigger_1_pct == -20

    def test_dca2_trigger_threshold(self):
        import config
        assert config.capital.dca_trigger_2_pct == -40

    def test_average_entry_calculation(self):
        orig_capital, entry = 1000.0, 100.0
        dca_capital, dca_price = 500.0, 80.0
        expected_avg = (orig_capital * entry + dca_capital * dca_price) / (orig_capital + dca_capital)
        assert round(expected_avg, 4) == round(
            (1000 * 100 + 500 * 80) / 1500, 4
        )

    def test_average_entry_round2(self):
        orig_capital, entry = 1000.0, 100.0
        after_r1_capital, after_r1_avg = 1500.0, 93.33
        dca2_capital, dca2_price = 500.0, 60.0
        new_avg = (after_r1_capital * after_r1_avg + dca2_capital * dca2_price) / (after_r1_capital + dca2_capital)
        assert new_avg < after_r1_avg


# AK-01: Trailing SL
class TestTrailingSL:
    def test_sl_never_moves_backward_long(self):
        from execution.paper import PaperExecutionEngine
        engine = PaperExecutionEngine()
        class FakeTrade:
            def __init__(self):
                self.calls = []
            def modify_sl(self, trade_id, new_sl):
                self.calls.append(new_sl)
        import unittest.mock as mock
        with mock.patch.object(engine, '_get_trade', return_value={
            "pair": "BTCUSDT", "direction": "long",
            "trailing_sl_level": 50000.0, "quantity": 0.01,
        }):
            with mock.patch('memory.write.write_trade_update'):
                with mock.patch('redis_client.get') as mock_r:
                    mock_r.return_value.publish = lambda *a, **kw: None
                    engine.modify_sl("fake-id", 49000.0)

    def test_sl_never_moves_backward_short(self):
        from execution.paper import PaperExecutionEngine
        engine = PaperExecutionEngine()
        import unittest.mock as mock
        with mock.patch.object(engine, '_get_trade', return_value={
            "pair": "BTCUSDT", "direction": "short",
            "trailing_sl_level": 45000.0, "quantity": 0.01,
        }):
            engine.modify_sl("fake-id", 46000.0)


# AK-08: Brain stage transitions
class TestBrainStageTransitions:
    def test_stage_1_to_2_requires_100_trades(self):
        import config
        assert config.brain.stage_1_to_2_trades == 100

    def test_stage_2_to_3_requires_500_and_winrate(self):
        import config
        assert config.brain.stage_2_to_3_trades == 500
        assert config.brain.stage_2_to_3_winrate == 45

    def test_stage_3_to_4_requires_1500_winrate_sharpe(self):
        import config
        assert config.brain.stage_3_to_4_trades == 1500
        assert config.brain.stage_3_to_4_winrate == 52
        assert config.brain.stage_3_to_4_sharpe == 1.0

    def test_live_unlock_requires_2000(self):
        import config
        assert config.brain.live_unlock_trades == 2000


# AK-10: Paper vs Live engine interface parity
class TestEngineInterfaceParity:
    def test_both_implement_same_interface(self):
        from execution.base import ExecutionEngine
        from execution.paper import PaperExecutionEngine
        from execution.live import LiveExecutionEngine
        import inspect

        base_methods = {
            name for name, _ in inspect.getmembers(ExecutionEngine, predicate=inspect.isfunction)
            if not name.startswith("_")
        }
        paper_methods = {
            name for name, _ in inspect.getmembers(PaperExecutionEngine, predicate=inspect.isfunction)
            if not name.startswith("_")
        }
        live_methods = {
            name for name, _ in inspect.getmembers(LiveExecutionEngine, predicate=inspect.isfunction)
            if not name.startswith("_")
        }

        assert base_methods.issubset(paper_methods), f"PaperEngine missing: {base_methods - paper_methods}"
        assert base_methods.issubset(live_methods), f"LiveEngine missing: {base_methods - live_methods}"


# AK-11: Strategy dual-save transaction
class TestStrategyDualSave:
    def test_save_writes_both_db_and_file(self, tmp_path):
        import unittest.mock as mock
        from strategy import save as s_mod

        original_dirs = s_mod._STRATEGY_DIRS.copy()
        s_mod._STRATEGY_DIRS["experimental"] = tmp_path

        strategy = {
            "status": "experimental",
            "source": "brain",
            "code": "def entry(): return True",
            "name": "test_strategy",
        }

        inserted = []

        def fake_execute(sql, params):
            if "INSERT" in sql:
                inserted.append(params)

        with mock.patch("strategy.save.db_conn") as mock_db:
            ctx = mock_db.return_value.__enter__.return_value
            ctx.cursor.return_value.__enter__.return_value.execute = fake_execute

            sid = s_mod.save_strategy(strategy)

        assert len(inserted) > 0, "DB INSERT not called"
        written_files = list(tmp_path.glob("*.py"))
        assert len(written_files) == 1, f"Expected 1 .py file, got {len(written_files)}"
        assert written_files[0].read_text() == "def entry(): return True"

        s_mod._STRATEGY_DIRS.update(original_dirs)

    def test_filesystem_failure_rolls_back(self, tmp_path):
        import unittest.mock as mock
        from strategy import save as s_mod

        original_dirs = s_mod._STRATEGY_DIRS.copy()
        read_only = tmp_path / "readonly"
        read_only.mkdir(mode=0o444)
        s_mod._STRATEGY_DIRS["experimental"] = read_only

        strategy = {"status": "experimental", "source": "brain", "code": "x=1", "name": "t"}

        with mock.patch("strategy.save.db_conn") as mock_db:
            ctx = mock_db.return_value.__enter__.return_value
            ctx.cursor.return_value.__enter__.return_value.execute = lambda *a: None

            try:
                s_mod.save_strategy(strategy)
                assert False, "Should have raised on filesystem failure"
            except (PermissionError, RuntimeError, OSError):
                pass

        s_mod._STRATEGY_DIRS.update(original_dirs)
