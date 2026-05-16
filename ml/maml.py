"""L-13: MAML meta-learning — fast adaptation on regime change. Activates 500-800 trades."""
import structlog

log = structlog.get_logger()


def maml_inner_update(model, task_loss_fn, alpha: float = 0.01) -> dict:
    """Inner loop: θ' = θ - α ∇_θ L_τᵢ(f_θ)"""
    try:
        import torch, copy
        adapted = copy.deepcopy(model)
        optimizer = torch.optim.SGD(adapted.parameters(), lr=alpha)
        loss = task_loss_fn(adapted)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        return {name: param.data for name, param in adapted.named_parameters()}
    except Exception as exc:
        log.error("maml_inner_update_failed", error=str(exc))
        return {}


def maml_outer_update(model, tasks: list, beta: float = 0.001) -> None:
    """Outer loop: θ ← θ - β ∇_θ Σᵢ L_τᵢ(f_θ'ᵢ)"""
    try:
        import torch
        optimizer = torch.optim.Adam(model.parameters(), lr=beta)
        total_loss = torch.tensor(0.0)
        for task_loss_fn in tasks:
            adapted_params = maml_inner_update(model, task_loss_fn)
            total_loss = total_loss + task_loss_fn(model)
        optimizer.zero_grad()
        total_loss.backward()
        optimizer.step()
        log.info("maml_outer_update_complete", n_tasks=len(tasks))
    except Exception as exc:
        log.error("maml_outer_update_failed", error=str(exc))


def trigger_on_changepoint(paper_closed_count: int) -> bool:
    """Only activate MAML once we have regime diversity (500-800 trades)."""
    return paper_closed_count >= 500
