"""
Section X: Master AI Brain — SOAR Cognitive Loop (X-01 to X-17).
Observe → Decide → Act. NO Reflect step (Feature 43B).
"""
import asyncio
import json
from datetime import datetime, timezone
import structlog
import redis_client
import redis_keys
import config
from db import db_conn
from memory.query import (
    get_open_trades, get_paper_closed_count, get_live_closed_count,
    get_rolling_win_rate, get_rolling_sharpe,
)
from execution.factory import get_engine
from risk.manager import (
    check_position_sizing, check_turbulence_circuit_breaker,
    assign_leverage, compute_initial_sl, check_dca_triggers,
)
from signals.engine import process_signals
from llm.fallback import handle_ollama_failure, ML_ONLY_SENTINEL

log = structlog.get_logger()


class MasterBrain:

    def __init__(self) -> None:
        self._engine = get_engine()
        self._stage = 1
        self._paper_closed = 0
        self._running = False

    # ------------------------------------------------------------------
    # X-02: Global Workspace — shared market snapshot
    # ------------------------------------------------------------------

    def _observe(self) -> dict:
        """OBSERVE phase: read all features from Redis; classify situation."""
        r = redis_client.get()

        regime = r.get(redis_keys.CURRENT_REGIME) or "unknown"
        turbulence = float(r.get(redis_keys.TURBULENCE_INDEX) or 0)
        global_sentiment = float(r.get(redis_keys.SENTIMENT_GLOBAL) or 0.5)
        active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))
        brain_stage = int(r.get(redis_keys.BRAIN_STAGE) or self._stage)

        observation = {
            "regime": regime,
            "turbulence": turbulence,
            "global_sentiment": global_sentiment,
            "active_pairs": active_pairs,
            "brain_stage": brain_stage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if regime != r.get("brain:last_regime"):
            r.set("brain:last_regime", regime)
            r.publish(redis_keys.CH_SYSTEM_EVENT, json.dumps({
                "event": "regime_change", "new_regime": regime,
            }))

        r.publish(redis_keys.CH_BRAIN_METRICS, json.dumps({
            "stage": brain_stage,
            "paper_closed": self._paper_closed,
            "regime": regime,
        }))

        return observation

    # ------------------------------------------------------------------
    # X-01: DECIDE phase
    # ------------------------------------------------------------------

    async def _decide(self, observation: dict) -> dict:
        """DECIDE phase: use LLM or ML-only to produce a trading decision."""
        r = redis_client.get()
        brain_stage = observation["brain_stage"]
        use_debate = brain_stage >= 3 and self._paper_closed >= 300

        prompt = (
            f"Market regime: {observation['regime']}. "
            f"Turbulence: {observation['turbulence']:.2f}. "
            f"Global sentiment: {observation['global_sentiment']:.2f}. "
            f"Active pairs: {len(observation['active_pairs'])}. "
            "Should we open new trades this cycle? Respond as JSON with keys: "
            "trade (bool), reason (str)."
        )

        verdict = ML_ONLY_SENTINEL
        if config.llm.no_reflection_enforced:
            try:
                if use_debate:
                    from llm.decision import decide
                    verdict = await decide(prompt, timeout=120)
                else:
                    from llm.router import classify
                    verdict = await classify(prompt, timeout=60)
            except Exception as exc:
                verdict = handle_ollama_failure(exc, "decide")

        r.publish(redis_keys.CH_BRAIN_DECISION, json.dumps({
            "verdict": verdict, "stage": brain_stage, "regime": observation["regime"],
        }))
        return verdict

    # ------------------------------------------------------------------
    # X-01: ACT phase
    # ------------------------------------------------------------------

    async def _act(self, observation: dict, decision: dict) -> None:
        """ACT phase: open trades, manage DCA, publish events."""
        brain_stage = observation.get("brain_stage", 1)

        # Stage 1 override: Baby Brain always tries to trade for data collection.
        # Blueprint: "Stage 1 = pure observation and data collection mode.
        #  Runs paper trades using basic logic purely to generate the first raw dataset."
        if brain_stage >= 2:
            # Stage 2+: respect LLM decision
            if decision.get("mode") == "ml_only" or decision.get("trade") is False:
                return
        # Stage 1: fall through and let signals decide (LLM decision is advisory only)

        if check_turbulence_circuit_breaker():
            return

        r = redis_client.get()

        # Read user-configured limits from Redis (set via dashboard before Start)
        max_open = r.get("bot:max_open_trades")
        min_open = r.get("bot:min_open_trades")
        max_pos_usdt = r.get("bot:max_position_usdt")

        if max_open is None or max_pos_usdt is None:
            # User hasn't configured settings yet — do not trade
            log.warning("brain_waiting_for_settings",
                        msg="Set min/max trades and max_position_usdt in dashboard first")
            return

        max_open = int(max_open)
        min_open = int(min_open) if min_open else 1
        max_pos_usdt = float(max_pos_usdt)

        balance = float(r.get(redis_keys.VIRTUAL_BALANCE) or r.get(redis_keys.ACCOUNT_BALANCE) or 0)
        if balance <= 0:
            return

        open_trades = get_open_trades()
        if len(open_trades) >= max_open:
            return

        total_deployed_pct = sum(
            float(t.get("capital_usdt", 0)) / balance * 100 for t in open_trades
        ) if balance > 0 else 0

        # Capital per trade = min(max_position_usdt, per_trade_min_pct% of balance)
        # Both limits apply — the smaller one wins
        pct_based = balance * config.capital.per_trade_min_pct / 100
        capital_per_trade = min(pct_based, max_pos_usdt)

        brain_state = {
            "stage": observation["brain_stage"],
            "active_strategy_id": None,
            "default_capital_usdt": capital_per_trade,
            "max_position_usdt": max_pos_usdt,
            "min_open_trades": min_open,
            "max_open_trades": max_open,
            "default_qty": 0.01,
        }

        await process_signals(observation["active_pairs"], brain_state, self._engine)

        for trade in open_trades:
            try:
                check_dca_triggers(trade, self._engine)
            except Exception as exc:
                log.warning("dca_check_failed", trade_id=trade.get("id"), error=str(exc))

    # ------------------------------------------------------------------
    # X-05: Brain stage transitions
    # ------------------------------------------------------------------

    def _check_stage_transition(self) -> None:
        self._paper_closed = get_paper_closed_count()
        r = redis_client.get()
        current_stage = int(r.get(redis_keys.BRAIN_STAGE) or self._stage)

        new_stage = current_stage
        if current_stage == 1 and self._paper_closed >= config.brain.stage_1_to_2_trades:
            new_stage = 2
        elif current_stage == 2 and self._paper_closed >= config.brain.stage_2_to_3_trades:
            wr = get_rolling_win_rate(100)
            if wr >= config.brain.stage_2_to_3_winrate:
                new_stage = 3
        elif current_stage == 3 and self._paper_closed >= config.brain.stage_3_to_4_trades:
            wr = get_rolling_win_rate(300)
            sh = get_rolling_sharpe(300)
            if wr >= config.brain.stage_3_to_4_winrate and sh >= config.brain.stage_3_to_4_sharpe:
                new_stage = 4

        if new_stage != current_stage:
            self._stage = new_stage
            r.set(redis_keys.BRAIN_STAGE, new_stage)
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE brain_state SET evolution_stage = %s, updated_at = NOW() WHERE id = 1",
                        (new_stage,),
                    )
            log.info("brain_stage_transition", from_stage=current_stage, to_stage=new_stage)
            try:
                from notifications.telegram import send_critical
                send_critical(f"Brain evolved to Stage {new_stage}!")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # X-16/X-17: Daily loss limit and max drawdown (live mode only)
    # ------------------------------------------------------------------

    def _check_live_circuit_breakers(self) -> bool:
        """Return True (block trades) if live circuit breakers are triggered."""
        if config.TRADING_MODE != "live" or not config.risk.circuit_breaker_live_only:
            return False

        r = redis_client.get()
        daily_pnl = float(r.get("brain:daily_pnl_usdt") or 0)
        balance = float(r.get(redis_keys.ACCOUNT_BALANCE) or 1)
        if daily_pnl / balance < -(config.risk.daily_loss_limit_pct / 100):
            log.warning("daily_loss_limit_hit", daily_pnl=daily_pnl)
            return True

        equity_peak = float(r.get("brain:equity_peak") or balance)
        drawdown_pct = (equity_peak - balance) / equity_peak * 100 if equity_peak > 0 else 0
        if drawdown_pct > config.risk.max_drawdown_pct:
            log.warning("max_drawdown_hit", drawdown_pct=drawdown_pct)
            return True

        return False

    # ------------------------------------------------------------------
    # Main SOAR loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """X-01: Main Observe → Decide → Act loop. Runs forever."""
        self._running = True
        log.info("brain_soar_loop_started", stage=self._stage)

        cycle = 0
        while self._running:
            try:
                # OBSERVE
                observation = self._observe()

                # Stage transition check every 10 cycles
                if cycle % 10 == 0:
                    self._check_stage_transition()

                # Live circuit breakers
                if self._check_live_circuit_breakers():
                    await asyncio.sleep(60)
                    continue

                # DECIDE
                decision = await self._decide(observation)

                # ACT
                await self._act(observation, decision)

                cycle += 1

            except Exception as exc:
                log.error("soar_cycle_error", error=str(exc), cycle=cycle)

            await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
        log.info("brain_stop_requested")
