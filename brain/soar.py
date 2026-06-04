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
from llm.fallback import (
    handle_ollama_failure, ML_ONLY_SENTINEL,
    ollama_in_cooldown, reset_ollama_health,
)

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

        prompt_parts = [
            f"Market regime: {observation['regime']}.",
            f"Turbulence: {observation['turbulence']:.2f}.",
            f"Global sentiment: {observation['global_sentiment']:.2f}.",
            f"Active pairs: {len(observation['active_pairs'])}.",
        ]
        # Blueprint F39A: OPRO-evolved prompt addendum. self_improve/opro.save_new_prompt
        # writes the latest LLM-proposed improvement here; we splice it in unless F30 governance
        # has deactivated F39A (in which case the saved addendum is ignored, not deleted).
        addendum = r.get("brain:executor_prompt_addendum")
        if addendum:
            try:
                from feature_governance.registry import is_active as _fg_active
                if _fg_active("F39A"):
                    prompt_parts.append(addendum.strip()[:500])
            except Exception:
                pass
        prompt_parts.append(
            "Should we open new trades this cycle? Respond as JSON with keys: trade (bool), reason (str)."
        )
        prompt = " ".join(prompt_parts)

        verdict = ML_ONLY_SENTINEL
        # cont. 2026-06-04: THROTTLE the decide LLM. The verdict is advisory (the
        # macro-veto was demoted — see _act), yet a synchronous 8-25s LLM call ran
        # EVERY ~cycle on this 6-CPU box, dominating loop time and occasionally
        # grazing the timeout. We now only call the LLM once per
        # `llm:decide_interval_s` (default 60s, Redis-tunable; set 0 to call every
        # cycle). Between calls the brain stays ML-only for that cycle (honest:
        # "we didn't ask the LLM"), which keeps the scan loop fast.
        import time as _time
        _r_thr = redis_client.get()
        # cont. 2026-06-04: MASTER SWITCH for the local LLM decide call. This
        # 6-CPU box runs the decision_model (llama3.2:3b) in 6-26s for a real
        # decide prompt — wildly variable under production load — so it routinely
        # exceeded any sane timeout and 500'd, then fell to cloud anyway. Since the
        # verdict is now ADVISORY (macro-veto demoted in _act, gates nothing), the
        # default is OFF: skip the LLM entirely, verdict stays ML-only, zero ollama
        # 500s, fast loop. Re-enable with Redis `llm:decide_enabled`="1" (e.g. on a
        # faster box); the throttle + timeout below then apply.
        _decide_enabled = (_r_thr.get("llm:decide_enabled") or "0") == "1"
        try:
            _decide_interval = int(_r_thr.get("llm:decide_interval_s") or 60)
        except Exception:
            _decide_interval = 60
        try:
            _last_llm_ts = float(_r_thr.get("brain:decide:last_llm_ts") or 0)
        except Exception:
            _last_llm_ts = 0.0
        _decide_due = (_time.time() - _last_llm_ts) >= _decide_interval

        # Circuit breaker: if recent ollama calls have been failing, skip the
        # request entirely for this cycle. Brain stays in ML-only mode silently
        # until cooldown elapses (see llm.fallback). Without this guard the loop
        # spams ollama_failure every ~14s when mistral/phi3 can't respond in 8s.
        if _decide_enabled and config.llm.no_reflection_enforced and _decide_due \
                and not ollama_in_cooldown("decide"):
            # cont. 2026-06-04: decide timeout was a hard 8s, but llama3.2:3b
            # (the new decision/router model) runs 6-9s warm on this CPU box —
            # calls landing >8s got client-cancelled mid-generation, which ollama
            # logs as 500/499 and the brain counts as a failure → cloud fallback.
            # Bumped to 12s (Redis-tunable `llm:decide_timeout_s`) to clear the
            # worst case. Harmless to trade latency: the verdict is advisory (see
            # _act macro-veto demotion) and the loop sleeps 5s/cycle regardless.
            try:
                _decide_timeout = int(redis_client.get().get("llm:decide_timeout_s") or 20)
            except Exception:
                _decide_timeout = 20
            # Mark the attempt time up front so the throttle advances even if the
            # call fails/times out — we don't want failures to retry every cycle.
            try:
                _r_thr.set("brain:decide:last_llm_ts", str(_time.time()))
            except Exception:
                pass
            try:
                if use_debate:
                    from llm.decision import decide
                    verdict = await decide(prompt, timeout=_decide_timeout)
                else:
                    from llm.router import classify
                    verdict = await classify(prompt, timeout=_decide_timeout)
                reset_ollama_health("decide")
            except Exception as exc:
                verdict = handle_ollama_failure(exc, "decide")

        # ──────────────────────────────────────────────────────────────────
        # Blueprint Section 9.1 — DECIDE phase layers wired inline (D-07).
        # L2 World Model imagination, L3 MemRL base-rate, L9 Metacognitive
        # confidence. Each runs ONCE per cycle, writes a Redis evidence key,
        # and contributes to the published decision_event so dashboard /
        # downstream consumers can see them. Failures degrade silently —
        # they're advisory inputs, not gates.
        # ──────────────────────────────────────────────────────────────────

        # L2 — World Model: imagine a 5-step trajectory under the "open_long"
        # anchor action using a representative active-pair mark price as the
        # initial observation. The trajectory's `uncertainty` (0-1) is the
        # brain's epistemic doubt about near-future state.
        world_model_uncertainty = None
        try:
            from world_model.model import imagine_trajectory
            active_pairs = observation.get("active_pairs") or []
            if active_pairs:
                _pair0 = active_pairs[0]
                _p = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", _pair0)) or 0)
                _v = float(r.get(f"{_pair0}:volume_24h") or 0)
                if _p > 0:
                    traj = imagine_trajectory(
                        "open_long", n_steps=5,
                        initial_obs={"price": _p, "volume": _v},
                    )
                    world_model_uncertainty = round(float(traj.get("uncertainty", 0)), 4)
                    r.set("brain:world_model_uncertainty", str(world_model_uncertainty))
        except Exception as exc:
            log.warning("decide_world_model_skipped", error=str(exc)[:120])

        # L3 — MemRL: query similar past trades for the current regime + sentiment
        # state and compute their realized win rate. "Base rate" of the current
        # context. Below 40% should make the brain wary; above 60% emboldens it.
        memrl_base_rate_pct = None
        memrl_sample_n = 0
        try:
            from memory.cognitive.memrl import get_base_rate_sample
            current_state = {
                "pair": (observation.get("active_pairs") or ["BTCUSDT"])[0],
                "direction": "long",
                "market_regime": observation.get("regime"),
                "capital_usdt": 100.0,
            }
            sample = get_base_rate_sample(current_state, top_k=30)
            memrl_sample_n = len(sample)
            if sample:
                wins = sum(1 for t in sample if float(t.get("net_pnl_usdt") or 0) > 0)
                memrl_base_rate_pct = round(100.0 * wins / len(sample), 1)
                r.set("brain:memrl_base_rate_pct", str(memrl_base_rate_pct))
                r.set("brain:memrl_sample_n", str(memrl_sample_n))
        except Exception as exc:
            log.warning("decide_memrl_skipped", error=str(exc)[:120])

        # L9 — Metacognitive confidence: distil brain:competence_map into a
        # single 0-100 score. competence_map keys are {win_bull, win_bear,
        # win_short, win_long, ...} mapped to lists of +1/-1 outcome markers.
        # Score = 50 + 50*mean(all markers) so 100=all wins, 0=all losses.
        # Priority gap from metacog goes into the event for dashboard visibility.
        metacog_confidence = None
        metacog_priority_gap = r.get("brain:priority_learning_gap")
        try:
            cmap_raw = r.get("brain:competence_map")
            if cmap_raw:
                cmap = json.loads(cmap_raw)
                if isinstance(cmap, dict):
                    allvals = []
                    for v in cmap.values():
                        if isinstance(v, list) and v:
                            allvals.extend(v)
                    if allvals:
                        metacog_confidence = round(
                            50.0 + 50.0 * sum(allvals) / len(allvals), 1)
                        r.set("brain:metacog_confidence", str(metacog_confidence))
        except Exception as exc:
            log.warning("decide_metacog_skipped", error=str(exc)[:120])

        decision_event = {
            "verdict": verdict, "stage": brain_stage, "regime": observation["regime"],
            "ts": observation.get("timestamp"),
            "turbulence": round(float(observation.get("turbulence", 0)), 3),
            "sentiment": round(float(observation.get("global_sentiment", 0.5)), 3),
            # Inline-decide layers (D-07)
            "world_model_uncertainty": world_model_uncertainty,
            "memrl_base_rate_pct": memrl_base_rate_pct,
            "memrl_sample_n": memrl_sample_n,
            "metacog_confidence": metacog_confidence,
            "metacog_priority_gap": metacog_priority_gap,
        }
        r.publish(redis_keys.CH_BRAIN_DECISION, json.dumps(decision_event))
        # Persist for /brain/decisions endpoint (blueprint 14.3) — the pubsub channel
        # only reaches live WebSocket clients; dashboard initial load needs history.
        # Cap at 200 entries via LTRIM so the list doesn't grow unbounded.
        try:
            r.lpush("brain:decisions_log", json.dumps(decision_event))
            r.ltrim("brain:decisions_log", 0, 199)
        except Exception:
            pass

        # Blueprint F38: Curiosity Engine — compute novelty from turbulence proxy.
        # F30 governance gate: skip when F38 deactivated.
        try:
            from feature_governance.registry import is_active as _fg_active_f38
            if not _fg_active_f38("F38"):
                return verdict
            from curiosity.engine import (
                compute_prediction_error_curiosity, get_exploration_budget,
                should_explore, log_exploration_hypothesis,
            )
            turbulence = float(observation.get("turbulence", 0))
            curiosity_score = compute_prediction_error_curiosity(turbulence)
            # Rough win rate from analytics key
            _wr_raw = r.get("analytics:metrics:all:100")
            _wr = json.loads(_wr_raw).get("win_rate", 50.0) if _wr_raw else 50.0
            exploration_budget = get_exploration_budget(self._paper_closed, _wr)
            r.set("brain:curiosity_score", round(curiosity_score, 2))
            r.set("brain:exploration_budget", round(exploration_budget, 2))
            if should_explore(curiosity_score, threshold=70):
                active = list(r.smembers(redis_keys.ACTIVE_PAIRS))
                if active:
                    import random as _rnd
                    log_exploration_hypothesis(
                        pair=_rnd.choice(active),
                        direction="unknown",
                        reason=f"High curiosity score {curiosity_score:.1f} — turbulence={turbulence:.3f}",
                    )
                log.info("curiosity_high", score=curiosity_score, budget=exploration_budget)
        except Exception as exc:
            log.warning("curiosity_engine_skipped", error=str(exc))

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
            # Explicit LLM "do not trade" → historically respected as a hard gate.
            # But Ollama failure (ml_only sentinel) must NOT block trading —
            # blueprint: "trading continues unaffected" on LLM failure.
            #
            # cont. (2026-06-04): DEMOTED to advisory. A non-deterministic cloud
            # LLM was vetoing the ENTIRE per-cycle pipeline (all 200 pairs at once)
            # whenever turbulence rose — it halted the bot for 2.5h while the
            # market was only moderately turbulent (turbulence=1.0, well below the
            # deterministic circuit-breaker's 2.5 threshold). The per-pair entry
            # pipeline already has turbulent-regime OFI tightening, the short
            # guard, sentiment/predict-all/conformal gates, AND the turbulence
            # circuit-breaker below — the macro LLM "no" was redundant and fragile.
            #
            # The veto now only blocks when explicitly re-armed via Redis
            # `brain:llm_macro_veto_enabled` = "1" (default OFF). When disarmed we
            # still log + count the LLM opinion so it stays visible (silent-
            # rejection rule), but process_signals runs and per-pair gates decide.
            if decision.get("trade") is False and decision.get("llm_available", True):
                _r = redis_client.get()
                _veto_armed = (_r.get("brain:llm_macro_veto_enabled") or "0") == "1"
                if _veto_armed:
                    _r.incr("brain:llm_macro_veto:blocked_count")
                    log.info("llm_macro_veto_blocked",
                             reason=str(decision.get("reason", ""))[:120],
                             regime=observation.get("regime"))
                    return
                _r.incr("brain:llm_macro_veto:advisory_count")
                log.info("llm_macro_veto_advisory_ignored",
                         reason=str(decision.get("reason", ""))[:120],
                         regime=observation.get("regime"),
                         msg="LLM said no-trade; advisory only, per-pair gates decide")
        # Stage 1: always fall through. Stage 2+ with ml_only: also fall through.

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

        # DCA checks run on every cycle regardless of trade capacity — trades in drawdown need DCA
        for trade in open_trades:
            try:
                check_dca_triggers(trade, self._engine)
            except Exception as exc:
                log.warning("dca_check_failed", trade_id=trade.get("id"), error=str(exc))

        # Dashboard Stop button — gate new signal-driven entries on bot:running.
        # Existing-position management (trailing SL in monitor_trailing_sl task, DCA above,
        # hedges) keeps running so operator can safely close-all after stopping.
        if r.get("bot:running") != "1":
            log.info("brain_act_skipped_bot_stopped", open_trades=len(open_trades))
            return

        if len(open_trades) >= max_open:
            return  # at capacity — skip signal generation, DCA already handled above

        # Capital per trade: user sets max_position_usdt directly via dashboard.
        # Only hard cap: never more than per_trade_max_pct (30%) of total account per trade.
        hard_cap = balance * config.capital.per_trade_max_pct / 100
        capital_per_trade = min(max_pos_usdt, hard_cap)

        # --- Option C: Auto-scale capital down when balance is thin ---
        # Cont. 43 (2026-05-24): DCA disabled (dca_rounds_max=0) → reserve_mult=1
        # (entry only). Previously 2x (1× entry + 0.5× DCA-1 + 0.5× DCA-2).
        # Spread the free balance across remaining slots at reserve_mult× each.
        # If balance can't sustain full-size trades, shrink size rather than skip entirely —
        # this keeps the brain generating data while respecting real capital limits.
        reserve_mult = 1 if config.capital.dca_rounds_max <= 0 else 2
        slots_remaining = max(1, max_open - len(open_trades))
        max_affordable = balance / (slots_remaining * reserve_mult)
        if max_affordable < capital_per_trade:
            capital_per_trade = round(max_affordable, 2)
            log.info("capital_auto_scaled",
                     requested=round(max_pos_usdt, 2),
                     effective=capital_per_trade,
                     free_balance=round(balance, 2),
                     slots_remaining=slots_remaining)

        # --- Option B: DCA reserve hard floor ---
        # After scaling, if we still can't cover even one trade with its reserve,
        # don't open anything. Better to wait for an SL to fire and return capital.
        # With DCA off the reserve is just the entry; with DCA on it's entry × 2.
        MIN_TRADE_USDT = 5.0
        if capital_per_trade < MIN_TRADE_USDT or balance < capital_per_trade * reserve_mult:
            log.warning("capital_starved",
                        free_balance=round(balance, 2),
                        capital_per_trade=round(capital_per_trade, 2),
                        msg="Pausing new trades — balance too low to cover entry reserve")
            return

        # Blueprint F21 MARL Day Agent: if loaded and active, use its risk_budget_pct
        # as an upper bound on capital_per_trade. Below 300 trades or without
        # checkpoints → no-op (agent reports active=False). F30 gate respected via
        # is_active("F21") inside ml.marl.
        #
        # cont. 52: anti-deadlock short-circuit. When `marl:day:deadlock_detected`
        # is set (sister to the minute-side detector, future-proof for a similar
        # all-zero collapse on the day agent), skip without sizing-down. The
        # flag is only cleared by a human checkpoint swap + restart.
        try:
            _day_deadlock = (r.get("marl:day:deadlock_detected") == "1"
                             or r.get("marl:day:deadlock_detected") == b"1")
            if not _day_deadlock:
                from ml.marl import get_day_agent_decision
                day_dec = get_day_agent_decision({
                    "sentiment": observation.get("global_sentiment", 0.5),
                    "turbulence": observation.get("turbulence", 0),
                })
                if day_dec.get("active"):
                    marl_cap = balance * float(day_dec["risk_budget_pct"]) / 100
                    if marl_cap < capital_per_trade:
                        log.info("marl_day_sizing",
                                 risk_budget_pct=day_dec["risk_budget_pct"],
                                 was=capital_per_trade, now=round(marl_cap, 2))
                        capital_per_trade = max(5.0, round(marl_cap, 2))
        except Exception as exc:
            log.warning("marl_day_sizing_skipped", error=str(exc)[:120])

        # Blueprint F16: Fractional Kelly position sizing — activates at 50+ closed trades.
        # Kelly informs; user cap (max_pos_usdt) and DCA reserve floor still enforce.
        # F30 governance gate: skip when F16 deactivated.
        if self._paper_closed >= 50:
            try:
                from feature_governance.registry import is_active as _fg_active
                if _fg_active("F16"):
                    from ml.kelly import get_position_size_pct
                    kelly_pct = get_position_size_pct(self._paper_closed)
                    kelly_capital = round(balance * kelly_pct / 100, 2)
                    if kelly_capital < capital_per_trade:
                        log.info("kelly_sizing", kelly_pct=kelly_pct,
                                 kelly_capital=kelly_capital, was=capital_per_trade)
                        capital_per_trade = max(5.0, kelly_capital)
            except Exception as exc:
                log.warning("kelly_sizing_skipped", error=str(exc))

        _stage = observation["brain_stage"]
        # F8 Strategy Selector — UCB1 multi-armed bandit across active +
        # experimental strategies, regime-filtered. Replaces the pre-2026-05-21
        # fixed-per-stage Redis key (which only ever picked one strategy and
        # blocked experimental strategies from accumulating trial data).
        # Falls back to the static seed key when F8 is governance-deactivated
        # or the selector returns None (no candidates).
        _strategy_uuid = None
        _f8_active = True
        try:
            from feature_governance.registry import is_active as _fg_active_f8
            _f8_active = _fg_active_f8("F8")
        except Exception:
            pass
        if _f8_active:
            try:
                from strategy.selector import select_strategy
                _strategy_uuid = select_strategy(
                    regime=observation.get("regime"),
                    brain_stage=_stage,
                )
            except Exception as exc:
                log.warning("strategy_selector_failed", error=str(exc)[:200])
        if _strategy_uuid is None:
            _uuid_key = f"bot:strategy_uuid:{min(_stage, 2)}"
            _strategy_uuid = r.get(_uuid_key) or None
        brain_state = {
            "stage": _stage,
            "active_strategy_id": _strategy_uuid,
            "default_capital_usdt": capital_per_trade,
            "max_position_usdt": max_pos_usdt,
            "min_open_trades": min_open,
            "max_open_trades": max_open,
        }

        await process_signals(observation["active_pairs"], brain_state, self._engine)

    # ------------------------------------------------------------------
    # X-05: Brain stage transitions
    # ------------------------------------------------------------------

    def _check_stage_transition(self) -> None:
        self._paper_closed = get_paper_closed_count()
        r = redis_client.get()
        r.set("brain:paper_closed", self._paper_closed)   # keep dashboard in sync
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

        # Blueprint F21 MARL: try to load PPO agents at startup. No-op below 300
        # trades or when checkpoints don't exist — the loop will retry every
        # stage-transition (re-checked on cycle % 10 below).
        try:
            from ml.marl import load_agents as _marl_load
            from memory.query import get_paper_closed_count as _pc
            _marl_load(_pc())
        except Exception as exc:
            log.warning("marl_load_startup_skipped", error=str(exc)[:120])

        cycle = 0
        while self._running:
            try:
                # OBSERVE
                observation = self._observe()

                # Stage transition check every 10 cycles
                if cycle % 10 == 0:
                    self._check_stage_transition()
                    # F21 MARL: re-attempt agent load after stage check so freshly
                    # trained checkpoints get picked up without a brain restart.
                    try:
                        from ml.marl import load_agents as _marl_load
                        _marl_load(self._paper_closed)
                    except Exception:
                        pass

                # HMM regime update every 60 cycles (~5 min) — model lives in brain container.
                # F30 governance gate: skip when F14 deactivated (per blueprint Feature 30 "no exemptions").
                if cycle % 60 == 0:
                    try:
                        from feature_governance.registry import is_active as _fg_active
                        if _fg_active("F14"):
                            import json as _json
                            from ml.hmm import update_regime
                            _r = redis_client.get()
                            returns_raw = _r.get("data:returns:recent")
                            if returns_raw:
                                update_regime(_json.loads(returns_raw))
                    except Exception as exc:
                        log.warning("hmm_update_skipped", error=str(exc))

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
