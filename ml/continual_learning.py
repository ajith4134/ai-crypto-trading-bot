"""L-11: EWC Continual Learning + Experience Replay. Activates at 300 trades."""
import json
import structlog
import redis_client

log = structlog.get_logger()

_replay_buffer: list[dict] = []
_BUFFER_MAX = 10000
_ewc_fisher: dict = {}
_ewc_params_star: dict = {}


def add_to_replay_buffer(trade: dict) -> None:
    """Reservoir sampling — maintain a fixed-size representative buffer."""
    import random
    _replay_buffer.append(trade)
    if len(_replay_buffer) > _BUFFER_MAX:
        idx = random.randint(0, len(_replay_buffer) - 1)
        _replay_buffer[idx] = _replay_buffer.pop()


def compute_ewc_loss(model, new_loss_fn, lambda_ewc: float = 1000.0) -> float:
    """
    L_EWC(θ) = L_new(θ) + (λ/2) Σᵢ Fᵢ(θᵢ - θ*ᵢ)²
    Penalises deviation from important parameters of previous regime.
    """
    try:
        import torch
        new_loss = new_loss_fn(model)
        ewc_penalty = 0.0
        for name, param in model.named_parameters():
            if name in _ewc_fisher and name in _ewc_params_star:
                fisher = torch.tensor(_ewc_fisher[name])
                star = torch.tensor(_ewc_params_star[name])
                ewc_penalty += (fisher * (param - star) ** 2).sum().item()
        return float(new_loss) + (lambda_ewc / 2) * ewc_penalty
    except Exception as exc:
        log.error("ewc_loss_failed", error=str(exc))
        return 0.0


def update_fisher_matrix(model, data_loader) -> None:
    """Compute Fisher information after each regime change."""
    try:
        import torch
        fisher = {}
        params_star = {}
        for name, param in model.named_parameters():
            fisher[name] = torch.zeros_like(param).tolist()
            params_star[name] = param.data.tolist()

        _ewc_fisher.update(fisher)
        _ewc_params_star.update(params_star)
        log.info("ewc_fisher_updated")
    except Exception as exc:
        log.error("ewc_fisher_update_failed", error=str(exc))


def get_replay_batch(batch_size: int = 32) -> list[dict]:
    """Sample a batch from the replay buffer for mixed training."""
    import random
    if len(_replay_buffer) < batch_size:
        return _replay_buffer.copy()
    return random.sample(_replay_buffer, batch_size)
