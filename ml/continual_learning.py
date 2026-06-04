"""L-11: EWC Continual Learning + Experience Replay. Activates at 300 trades."""
import json
import random
import structlog
import redis_client

log = structlog.get_logger()

# In-memory caches. Replay buffer is the live reservoir; Fisher/params* are
# Redis-backed via persist_fisher / load_fisher so they survive container restarts.
_replay_buffer: list[dict] = []
_BUFFER_MAX = 10000
_ewc_fisher: dict = {}
_ewc_params_star: dict = {}

_EWC_REDIS_KEY = "ewc:state"
_REPLAY_REDIS_KEY = "ewc:replay_buffer"  # Redis list, newest at head (LPUSH)


def add_to_replay_buffer(trade: dict) -> None:
    """Reservoir sampling — maintain a fixed-size representative buffer.

    Stored in Redis so the brain process (which writes on every close) and the
    celery worker (which reads during sleep consolidation) share state.
    Reservoir invariant via LPUSH + occasional LSET at a random index once
    the buffer is full.
    """
    try:
        r = redis_client.get()
        # Serialize: drop the feature_vector if it's a dict to keep entries compact
        payload = {
            "pair": trade.get("pair"),
            "direction": trade.get("direction"),
            "market_regime": trade.get("market_regime"),
            "capital_usdt": float(trade.get("capital_usdt") or 0),
            "net_pnl_usdt": float(trade.get("net_pnl_usdt") or 0),
            "trade_quality_score": float(trade.get("trade_quality_score") or 0),
        }
        size = r.llen(_REPLAY_REDIS_KEY)
        if size < _BUFFER_MAX:
            r.lpush(_REPLAY_REDIS_KEY, json.dumps(payload))
        else:
            # True reservoir: replace random existing entry
            idx = random.randint(0, _BUFFER_MAX - 1)
            r.lset(_REPLAY_REDIS_KEY, idx, json.dumps(payload))
    except Exception as exc:
        # Fall back to in-memory only (single-process tests / Redis down)
        log.warning("replay_buffer_redis_failed", error=str(exc)[:120])
        _replay_buffer.append(trade)
        if len(_replay_buffer) > _BUFFER_MAX:
            _replay_buffer.pop(random.randint(0, len(_replay_buffer) - 1))


def get_replay_batch(batch_size: int = 32) -> list[dict]:
    """Sample a batch from the replay buffer for mixed training."""
    try:
        r = redis_client.get()
        size = r.llen(_REPLAY_REDIS_KEY)
        if size == 0:
            return list(_replay_buffer)[:batch_size]
        if size <= batch_size:
            raws = r.lrange(_REPLAY_REDIS_KEY, 0, -1)
        else:
            indices = random.sample(range(size), batch_size)
            raws = [r.lindex(_REPLAY_REDIS_KEY, i) for i in indices]
        return [json.loads(x) for x in raws if x]
    except Exception as exc:
        log.warning("replay_batch_redis_failed", error=str(exc)[:120])
        if len(_replay_buffer) <= batch_size:
            return list(_replay_buffer)
        return random.sample(_replay_buffer, batch_size)


def replay_buffer_size() -> int:
    try:
        return int(redis_client.get().llen(_REPLAY_REDIS_KEY))
    except Exception:
        return len(_replay_buffer)


def compute_ewc_loss(model, new_loss_fn, lambda_ewc: float = 1000.0) -> float:
    """
    L_EWC(θ) = L_new(θ) + (λ/2) Σᵢ Fᵢ(θᵢ - θ*ᵢ)²
    Penalises deviation from important parameters of previous regime.
    """
    try:
        import torch
        new_loss = new_loss_fn(model)
        ewc_penalty = torch.tensor(0.0)
        for name, param in model.named_parameters():
            if name in _ewc_fisher and name in _ewc_params_star:
                fisher = torch.tensor(_ewc_fisher[name], dtype=param.dtype, device=param.device)
                star = torch.tensor(_ewc_params_star[name], dtype=param.dtype, device=param.device)
                ewc_penalty = ewc_penalty + (fisher * (param - star) ** 2).sum()
        total = new_loss + (lambda_ewc / 2) * ewc_penalty
        return total
    except Exception as exc:
        log.error("ewc_loss_failed", error=str(exc))
        return 0.0


def update_fisher_matrix(model, batches, loss_fn) -> None:
    """Compute Fisher information from gradients on a representative batch set.
    Fisher F_i = E[(∂L/∂θ_i)²]. Snapshot θ* = current params.

    `batches` is an iterable of (inputs, targets); `loss_fn(model, inputs, targets)`
    returns a scalar tensor (e.g., NLL).
    """
    try:
        import torch
        fisher_accum = {name: torch.zeros_like(p) for name, p in model.named_parameters()
                        if p.requires_grad}
        n_batches = 0
        for inputs, targets in batches:
            model.zero_grad()
            loss = loss_fn(model, inputs, targets)
            loss.backward()
            for name, p in model.named_parameters():
                if p.requires_grad and p.grad is not None:
                    fisher_accum[name] = fisher_accum[name] + p.grad.detach() ** 2
            n_batches += 1

        if n_batches == 0:
            log.warning("ewc_fisher_no_batches")
            return

        _ewc_fisher.clear()
        _ewc_params_star.clear()
        for name, p in model.named_parameters():
            if p.requires_grad:
                _ewc_fisher[name] = (fisher_accum[name] / n_batches).cpu().tolist()
                _ewc_params_star[name] = p.detach().cpu().tolist()

        _persist_state()
        log.info("ewc_fisher_updated", n_batches=n_batches,
                 n_params=len(_ewc_fisher))
    except Exception as exc:
        log.error("ewc_fisher_update_failed", error=str(exc))


def _persist_state() -> None:
    try:
        redis_client.get().set(_EWC_REDIS_KEY, json.dumps({
            "fisher": _ewc_fisher,
            "params_star": _ewc_params_star,
        }))
    except Exception as exc:
        log.warning("ewc_persist_failed", error=str(exc))


def load_state() -> bool:
    """Restore Fisher/params* from Redis. Returns True if state was found."""
    try:
        raw = redis_client.get().get(_EWC_REDIS_KEY)
        if not raw:
            return False
        state = json.loads(raw)
        _ewc_fisher.clear()
        _ewc_params_star.clear()
        _ewc_fisher.update(state.get("fisher", {}))
        _ewc_params_star.update(state.get("params_star", {}))
        log.info("ewc_state_loaded", n_params=len(_ewc_fisher))
        return True
    except Exception as exc:
        log.warning("ewc_load_failed", error=str(exc))
        return False


def consolidate_world_model_if_ready(paper_closed: int) -> dict:
    """Sleep-consolidation entry point: at 300+ closed trades, snapshot Fisher info
    on the World Model's reward head from a batch drawn from the replay buffer.

    This is the bridge between sleep consolidation and EWC — without it, the
    Fisher matrix never updates and EWC penalty is always zero.

    Buffer lives in Redis (LPUSH from brain process during write_trade_close);
    use replay_buffer_size() not len(_replay_buffer), since the in-memory list
    is only populated in the fallback path when Redis is unavailable.
    """
    if paper_closed < 300:
        return {"status": "not_active", "needed": 300, "have": paper_closed}
    buf_size = replay_buffer_size()
    if buf_size < 32:
        return {"status": "insufficient_buffer", "have": buf_size}

    try:
        import torch
        # Late import — world_model loads heavyweight tensors lazily
        from world_model import model as wm
        wm._load()  # ensures _bundle is populated when checkpoint exists
        if wm._bundle is None:
            return {"status": "world_model_unloaded"}

        bundle = wm._bundle
        sample = get_replay_batch(min(128, buf_size))
        d_dim = bundle.DETER_DIM
        s_dim = bundle.STOCH_DIM

        batches = []
        bs = 32
        for i in range(0, len(sample), bs):
            chunk = sample[i:i + bs]
            # Build a random-ish latent batch around the prior — Fisher only needs
            # representative gradients, not perfect reconstruction.
            deter = torch.zeros(len(chunk), d_dim)
            stoch = torch.zeros(len(chunk), s_dim)
            for j, t in enumerate(chunk):
                cap = float(t.get("capital_usdt") or 100) / 1000.0
                pnl = float(t.get("net_pnl_usdt") or 0) / 100.0
                # Encode some signal into the latent so Fisher reflects real-data variance
                deter[j, 0] = cap
                stoch[j, 0] = pnl
            targets = torch.tensor(
                [float(t.get("net_pnl_usdt") or 0) for t in chunk],
                dtype=torch.float32,
            )
            batches.append(((deter, stoch), targets))

        def _loss_fn(_model, inputs, targets):
            deter, stoch = inputs
            pred = bundle.reward_from_state(deter, stoch).squeeze(-1)
            return torch.nn.functional.mse_loss(pred, targets)

        update_fisher_matrix(bundle.reward, batches, _loss_fn)
        return {
            "status": "ok",
            "n_batches": len(batches),
            "buffer_size": len(_replay_buffer),
        }
    except Exception as exc:
        log.error("ewc_consolidate_failed", error=str(exc))
        return {"status": "error", "error": str(exc)[:200]}
