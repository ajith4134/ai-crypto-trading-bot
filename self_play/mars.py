"""
Section AD: Self-Play Strategy Discovery — AD-01 to AD-07.

Blueprint F41: AlphaZero-style self-play against a market simulator.

The 2026-05-21 (cont. 4) rewrite replaces the previous geometric-Brownian-motion
`next_tick()` with a real agent-based limit-order-book simulator. The new sim
maintains bid/ask depth, processes Poisson maker/taker/cancel arrivals, and
walks the book on taker fills — so strategies executing through it see real
slippage and partial fills.

Rule 4 honest gap to blueprint: this is NOT the learned MarS generative LOB
model from arXiv:2409.07486. That paper trains a transformer-based generative
model on real Binance order flow and conditions tick generation on the latent
market state. Replicating it needs the training data pipeline + model weights
that aren't built yet. What's here is hand-coded agent-based microstructure —
massively more realistic than GBM (real spread, real depth, real impact) but
still parametric. Migration to the learned model is a future session.

The policy/value heads (`_policy_head`, `_value_head`) remain None stubs —
training them on self-play rollouts is the next layer up. Documented at
their definitions.
"""
import json
import math
import random
import time
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────────
# F41 LOB simulator: agent-based limit-order-book engine.
# Replaces the pre-2026-05-21 GBM next_tick().
# ─────────────────────────────────────────────────────────────────────────────

class OrderBook:
    """Two-sided depth-tracked limit order book.

    Bids: list of [price, qty] sorted DESC by price (best bid at index 0).
    Asks: list of [price, qty] sorted ASC by price (best ask at index 0).
    Prices are rounded to tick precision; qty is float.
    """

    def __init__(self, mid: float, tick: float, depth_levels: int = 10):
        self.tick = tick
        self.depth_levels = depth_levels
        self.bids: list[list[float]] = []
        self.asks: list[list[float]] = []
        self._seed(mid)

    def _seed(self, mid: float) -> None:
        for i in range(self.depth_levels):
            self.bids.append([round(mid - (i + 1) * self.tick, 8),
                              random.uniform(0.5, 2.0)])
            self.asks.append([round(mid + (i + 1) * self.tick, 8),
                              random.uniform(0.5, 2.0)])

    # ── inspection ────────────────────────────────────────────────────────
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else float("inf")

    def mid(self) -> float:
        return (self.best_bid() + self.best_ask()) / 2.0

    def spread(self) -> float:
        return max(0.0, self.best_ask() - self.best_bid())

    def total_bid_depth(self) -> float:
        return sum(q for _, q in self.bids)

    def total_ask_depth(self) -> float:
        return sum(q for _, q in self.asks)

    # ── maker side: post limit ────────────────────────────────────────────
    def post_limit_buy(self, price: float, qty: float) -> None:
        price = round(price, 8)
        # Same price level? Increment qty.
        for level in self.bids:
            if abs(level[0] - price) < 1e-9:
                level[1] += qty
                return
        self.bids.append([price, qty])
        self.bids.sort(key=lambda x: -x[0])
        self.bids = self.bids[: max(self.depth_levels, 20)]

    def post_limit_sell(self, price: float, qty: float) -> None:
        price = round(price, 8)
        for level in self.asks:
            if abs(level[0] - price) < 1e-9:
                level[1] += qty
                return
        self.asks.append([price, qty])
        self.asks.sort(key=lambda x: x[0])
        self.asks = self.asks[: max(self.depth_levels, 20)]

    # ── taker side: market order walks the book ───────────────────────────
    def market_buy(self, qty: float) -> tuple[float, float, int]:
        """Sweep asks. Returns (filled_qty, vwap, levels_traversed)."""
        remaining = qty
        cost = 0.0
        filled = 0.0
        levels = 0
        while remaining > 1e-9 and self.asks:
            price, avail = self.asks[0]
            take = min(remaining, avail)
            cost += take * price
            filled += take
            remaining -= take
            self.asks[0][1] -= take
            if self.asks[0][1] <= 1e-9:
                self.asks.pop(0)
                levels += 1
        vwap = (cost / filled) if filled > 0 else 0.0
        return filled, vwap, levels

    def market_sell(self, qty: float) -> tuple[float, float, int]:
        """Sweep bids. Returns (filled_qty, vwap, levels_traversed)."""
        remaining = qty
        proceeds = 0.0
        filled = 0.0
        levels = 0
        while remaining > 1e-9 and self.bids:
            price, avail = self.bids[0]
            take = min(remaining, avail)
            proceeds += take * price
            filled += take
            remaining -= take
            self.bids[0][1] -= take
            if self.bids[0][1] <= 1e-9:
                self.bids.pop(0)
                levels += 1
        vwap = (proceeds / filled) if filled > 0 else 0.0
        return filled, vwap, levels

    # ── cancellation: maker pulls qty from random level ───────────────────
    def cancel_random(self, qty: float) -> None:
        side = self.bids if random.random() < 0.5 else self.asks
        if not side:
            return
        idx = random.randint(0, len(side) - 1)
        side[idx][1] = max(0.0, side[idx][1] - qty)
        if side[idx][1] <= 1e-9:
            side.pop(idx)


class MaRSSimulator:
    """Agent-based market simulator with Poisson order arrivals.

    Each step:
      - Sample N_m maker arrivals (split across bid/ask, posted near best)
      - Sample N_t taker arrivals (market orders walking the opposite side)
      - Sample N_c cancellations (remove qty from random level)
      - Refill if either side is too thin (keeps the book non-pathological)

    Tunable rates are class constants. Reproducible — accepts a seed.
    """

    # Per-step arrival rates (Poisson lambdas). Tuned so a typical 200-step
    # episode sees ~400 makers, ~60 takers, ~100 cancels — comparable to
    # the L1 arrival density in a low-activity Binance perp tape.
    LAMBDA_MAKER = 2.0
    LAMBDA_TAKER = 0.30
    LAMBDA_CANCEL = 0.50

    # Taker size distribution: log-normal in "depth units" (relative to a
    # single-level qty). Mean ≈ 1.5 levels, occasionally up to 5+.
    TAKER_SIZE_MU = 0.4
    TAKER_SIZE_SIGMA = 0.6

    # Maker post depth: within ±N ticks of best price on its side.
    MAKER_POST_RANGE = 5

    def __init__(self, pair: str = "BTCUSDT",
                 initial_price: float = 50000.0,
                 seed: int | None = None):
        self.pair = pair
        if seed is not None:
            random.seed(seed)
        tick = max(initial_price * 0.0001, 1e-8)
        self.book = OrderBook(initial_price, tick=tick, depth_levels=10)
        self.step = 0
        self._last_taker_volume = 0.0
        self._last_step_summary: dict = {}

    # ── single tick ────────────────────────────────────────────────────────
    def next_tick(self) -> dict:
        n_makers = self._poisson(self.LAMBDA_MAKER)
        n_takers = self._poisson(self.LAMBDA_TAKER)
        n_cancels = self._poisson(self.LAMBDA_CANCEL)
        taker_volume = 0.0
        taker_levels = 0

        for _ in range(n_makers):
            self._maker_arrival()
        for _ in range(n_takers):
            qty, levels = self._taker_arrival()
            taker_volume += qty
            taker_levels += levels
        for _ in range(n_cancels):
            self.book.cancel_random(qty=random.uniform(0.1, 0.5))

        self._enforce_depth_floor()
        self.step += 1
        self._last_taker_volume = taker_volume
        self._last_step_summary = {
            "n_makers": n_makers, "n_takers": n_takers, "n_cancels": n_cancels,
            "taker_volume": taker_volume, "taker_levels": taker_levels,
        }

        return {
            "price":  round(self.book.mid(), 8),
            "volume": round(taker_volume, 4),
            "bid":    round(self.book.best_bid(), 8),
            "ask":    round(self.book.best_ask(), 8),
            "spread": round(self.book.spread(), 8),
            "depth_bid": round(self.book.total_bid_depth(), 4),
            "depth_ask": round(self.book.total_ask_depth(), 4),
            "step":   self.step,
        }

    def reset(self, initial_price: float | None = None) -> dict:
        price = initial_price or random.uniform(10000, 80000)
        self.book = OrderBook(price, tick=max(price * 0.0001, 1e-8),
                              depth_levels=10)
        self.step = 0
        self._last_taker_volume = 0.0
        return self.next_tick()

    # ── strategy execution: market orders go through the book ─────────────
    def execute_market_buy(self, qty: float) -> dict:
        mid_before = self.book.mid()
        filled, vwap, levels = self.book.market_buy(qty)
        slippage_bps = ((vwap - mid_before) / mid_before * 1e4) if mid_before else 0.0
        return {
            "side": "buy", "qty_requested": qty, "qty_filled": filled,
            "vwap": vwap, "mid_before": mid_before, "mid_after": self.book.mid(),
            "slippage_bps": slippage_bps, "levels_traversed": levels,
        }

    def execute_market_sell(self, qty: float) -> dict:
        mid_before = self.book.mid()
        filled, vwap, levels = self.book.market_sell(qty)
        slippage_bps = ((mid_before - vwap) / mid_before * 1e4) if mid_before else 0.0
        return {
            "side": "sell", "qty_requested": qty, "qty_filled": filled,
            "vwap": vwap, "mid_before": mid_before, "mid_after": self.book.mid(),
            "slippage_bps": slippage_bps, "levels_traversed": levels,
        }

    # ── internal helpers ───────────────────────────────────────────────────
    @staticmethod
    def _poisson(lam: float) -> int:
        """Knuth's algorithm — fine for small λ."""
        L = math.exp(-lam)
        k, p = 0, 1.0
        while p > L:
            k += 1
            p *= random.random()
        return k - 1

    def _maker_arrival(self) -> None:
        # Choose side; post within MAKER_POST_RANGE ticks of best price.
        if random.random() < 0.5 and self.book.bids:
            best = self.book.best_bid()
            offset = random.randint(0, self.MAKER_POST_RANGE) * self.book.tick
            self.book.post_limit_buy(best - offset, random.uniform(0.3, 1.5))
        elif self.book.asks:
            best = self.book.best_ask()
            offset = random.randint(0, self.MAKER_POST_RANGE) * self.book.tick
            self.book.post_limit_sell(best + offset, random.uniform(0.3, 1.5))

    def _taker_arrival(self) -> tuple[float, int]:
        # Log-normal size, market order against random side.
        qty = math.exp(random.gauss(self.TAKER_SIZE_MU, self.TAKER_SIZE_SIGMA))
        if random.random() < 0.5:
            _, _, levels = self.book.market_buy(qty)
        else:
            _, _, levels = self.book.market_sell(qty)
        return qty, levels

    def _enforce_depth_floor(self) -> None:
        """If either side has fewer than 3 levels, refill maker quotes near
        the existing best so the book never pathologically empties."""
        if len(self.book.bids) < 3:
            base = self.book.best_bid() or (self.book.best_ask() - self.book.tick)
            for i in range(5):
                self.book.post_limit_buy(base - (i + 1) * self.book.tick,
                                          random.uniform(0.5, 1.5))
        if len(self.book.asks) < 3:
            base = self.book.best_ask() or (self.book.best_bid() + self.book.tick)
            if not math.isfinite(base):
                base = self.book.best_bid() + self.book.tick
            for i in range(5):
                self.book.post_limit_sell(base + (i + 1) * self.book.tick,
                                          random.uniform(0.5, 1.5))


# ─────────────────────────────────────────────────────────────────────────────
# AlphaZero scaffolding. Policy and value heads remain None stubs — the
# blueprint's "trained policy/value via self-play rollouts" is a future
# session. Until then the priors are uniform-ish and MCTS leans on F34
# world model for value estimates.
# ─────────────────────────────────────────────────────────────────────────────
_policy_head = None   # NOTE: training loop not implemented (Rule 4)
_value_head  = None


def get_action_probabilities(market_state: dict) -> dict:
    """AD-02: Probability distribution over actions.
    Untrained — returns a fixed mildly-hold-biased prior."""
    if _policy_head is None:
        return {"open_long": 0.25, "open_short": 0.25, "hold": 0.4, "close": 0.1}
    try:
        import torch
        obs = torch.tensor([
            market_state.get("price", 0) / 100000,
            market_state.get("volume", 0) / 10000,
        ], dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            logits = _policy_head(obs)
            probs  = torch.softmax(logits, dim=-1).squeeze().tolist()
        actions = ["open_long", "open_short", "hold", "close"]
        return dict(zip(actions, probs))
    except Exception:
        return {"open_long": 0.25, "open_short": 0.25, "hold": 0.4, "close": 0.1}


def estimate_value(market_state: dict) -> float:
    """AD-03: Untrained — returns 0.0."""
    if _value_head is None:
        return 0.0
    try:
        import torch
        obs = torch.tensor([market_state.get("price", 0) / 100000],
                           dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            return float(_value_head(obs).squeeze())
    except Exception:
        return 0.0


_EPSILON = 0.20   # AlphaZero-style exploration in self-play episodes


def mcts_plan(market_state: dict, n_simulations: int = 50) -> str:
    """AD-04: UCB-based action choice with ε-greedy exploration. Uses F34
    world model for value estimates of imagined trajectories — known
    circular dependency we accept until policy/value heads are trained.

    Without exploration, untrained priors + near-zero predicted reward
    collapse every step to 'hold' and episodes produce zero fills (and
    therefore zero slippage signal). ε=0.20 keeps the agent firing
    market orders so the LOB upgrade is actually exercised."""
    if random.random() < _EPSILON:
        return random.choice(("open_long", "open_short", "hold", "close"))
    from world_model.model import imagine_trajectory
    action_scores: dict[str, float] = {}
    probs = get_action_probabilities(market_state)
    for action in ("open_long", "open_short", "hold", "close"):
        traj = imagine_trajectory(action, n_steps=10, initial_obs=market_state)
        ucb = traj["mean_pnl"] + 1.41 * probs.get(action, 0.25) * (n_simulations ** 0.5)
        action_scores[action] = ucb
    return max(action_scores, key=action_scores.get)


# ─────────────────────────────────────────────────────────────────────────────
# Self-play loop — now executes through the LOB so PnL respects slippage.
# ─────────────────────────────────────────────────────────────────────────────

def run_self_play_episode(n_steps: int = 200, position_qty: float = 0.5) -> dict:
    """AD-05: One episode against MarS.

    The strategy executes market orders through the LOB so PnL captures real
    slippage and partial fills. Aggregate stats (mean spread, mean slippage,
    levels traversed) are returned for observability.
    """
    sim = MaRSSimulator()
    obs = sim.reset()
    position: dict | None = None
    total_pnl = 0.0
    realized_fills = 0
    sum_slippage_bps = 0.0
    sum_spread = 0.0
    levels_traversed = 0

    for _ in range(n_steps):
        action = mcts_plan(obs)
        obs    = sim.next_tick()
        sum_spread += obs["spread"]

        if action == "open_long" and position is None:
            fill = sim.execute_market_buy(position_qty)
            if fill["qty_filled"] > 0:
                position = {"entry_vwap": fill["vwap"],
                            "qty": fill["qty_filled"], "direction": "long"}
                realized_fills += 1
                sum_slippage_bps += fill["slippage_bps"]
                levels_traversed += fill["levels_traversed"]
        elif action == "open_short" and position is None:
            fill = sim.execute_market_sell(position_qty)
            if fill["qty_filled"] > 0:
                position = {"entry_vwap": fill["vwap"],
                            "qty": fill["qty_filled"], "direction": "short"}
                realized_fills += 1
                sum_slippage_bps += fill["slippage_bps"]
                levels_traversed += fill["levels_traversed"]
        elif action == "close" and position:
            if position["direction"] == "long":
                fill = sim.execute_market_sell(position["qty"])
                pnl  = (fill["vwap"] - position["entry_vwap"]) * fill["qty_filled"]
            else:
                fill = sim.execute_market_buy(position["qty"])
                pnl  = (position["entry_vwap"] - fill["vwap"]) * fill["qty_filled"]
            total_pnl += pnl
            realized_fills += 1
            sum_slippage_bps += fill["slippage_bps"]
            levels_traversed += fill["levels_traversed"]
            position = None

    mean_spread        = sum_spread / max(1, n_steps)
    mean_slippage_bps  = sum_slippage_bps / max(1, realized_fills)
    return {
        "total_pnl":         round(total_pnl, 4),
        "steps":             n_steps,
        "fills":             realized_fills,
        "mean_spread":       round(mean_spread, 6),
        "mean_slippage_bps": round(mean_slippage_bps, 4),
        "levels_traversed":  levels_traversed,
        "final_mid":         round(sim.book.mid(), 8),
    }


def promote_from_self_play(win_rate: float, threshold: float = 0.55) -> bool:
    """AD-06: Promote strategy to paper trial if self-play win rate exceeds threshold."""
    if win_rate < threshold:
        return False
    strategy = {
        "name": f"self_play_strategy_{random.randint(1000, 9999)}",
        "source": "self_play",
        "code": "# Self-play derived strategy — parameters to be filled by research engine",
        "status": "experimental",
    }
    from strategy.lifecycle import create_experimental
    sid = create_experimental(strategy)
    log.info("self_play_strategy_promoted", id=sid, win_rate=win_rate)
    return True


def feed_to_world_model(episode_result: dict) -> None:
    """AD-07: Feed self-play outcomes to World Model as training data."""
    from world_model.model import update_on_trade_close
    update_on_trade_close(
        predicted_outcome={"mean_pnl": episode_result.get("total_pnl", 0) * 0.1},
        actual_pnl=episode_result.get("total_pnl", 0),
    )


# Convenience for instrumentation: persist last-episode microstructure
# stats to Redis so feature_health and the dashboard can read them.
def record_episode_stats(episode_result: dict) -> None:
    r = redis_client.get()
    try:
        r.set("self_play:last_mean_spread",       episode_result.get("mean_spread", 0))
        r.set("self_play:last_mean_slippage_bps", episode_result.get("mean_slippage_bps", 0))
        r.set("self_play:last_fills",             episode_result.get("fills", 0))
        r.set("self_play:last_levels_traversed",  episode_result.get("levels_traversed", 0))
        r.set("self_play:last_episode_ts",        int(time.time()))
        # Rolling per-episode history for the dashboard LOB stats panel.
        # LPUSH + LTRIM keeps the newest 50 episodes — bounded memory.
        import json as _json
        entry = {
            "ts":                int(time.time()),
            "total_pnl":         float(episode_result.get("total_pnl", 0)),
            "fills":             int(episode_result.get("fills", 0)),
            "mean_spread":       float(episode_result.get("mean_spread", 0)),
            "mean_slippage_bps": float(episode_result.get("mean_slippage_bps", 0)),
            "levels_traversed":  int(episode_result.get("levels_traversed", 0)),
            "steps":             int(episode_result.get("steps", 0)),
        }
        r.lpush("self_play:episode_history", _json.dumps(entry))
        r.ltrim("self_play:episode_history", 0, 49)
    except Exception as exc:
        log.warning("self_play_stats_persist_failed", error=str(exc)[:120])
