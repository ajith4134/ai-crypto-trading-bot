"""
Section AA: Autonomous Strategy Research Engine — AA-01 to AA-10.
Activates at 100 closed paper trades.
"""
import ast
import json
import math
import structlog
import redis_client
from db import db_conn

log = structlog.get_logger()


# AA-01: MCTS node
class MCTSNode:
    def __init__(self, hypothesis: str, parent=None):
        self.hypothesis = hypothesis
        self.parent = parent
        self.children: list["MCTSNode"] = []
        self.visits = 0
        self.value = 0.0

    def ucb(self, exploration_c: float = 1.41) -> float:
        if self.visits == 0:
            return float("inf")
        parent_visits = self.parent.visits if self.parent else 1
        return self.value / self.visits + exploration_c * math.sqrt(math.log(parent_visits) / self.visits)

    def best_child(self) -> "MCTSNode":
        return max(self.children, key=lambda c: c.ucb())


def originality_check(code: str, strategy_dir: str = "/opt/trading-bot/strategies") -> bool:
    """AA-04: AST structural similarity check against all existing strategies."""
    import os
    try:
        new_ast = ast.dump(ast.parse(code))
    except SyntaxError:
        return False

    for root, _, files in os.walk(strategy_dir):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                existing = open(fpath).read()
                existing_ast = ast.dump(ast.parse(existing))
                # Simple similarity: shared tokens / total tokens
                new_tokens = set(new_ast.split())
                ex_tokens = set(existing_ast.split())
                if not new_tokens:
                    continue
                similarity = len(new_tokens & ex_tokens) / len(new_tokens)
                if similarity > 0.80:
                    log.info("strategy_too_similar", similarity=round(similarity, 2), file=fname)
                    return False
            except Exception:
                continue
    return True


def world_model_prescreen(strategy_code: str) -> bool:
    """AA-05: Use World Model to pre-screen strategy before backtesting."""
    # Stub — real implementation wires to world_model/
    return True


def log_research_note(hypothesis: str, metrics: dict, decision: str, reason: str) -> None:
    """AA-09: Write structured research note to experiments table."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO experiments (experiment_type, hypothesis, metrics, decision, decision_reason)
                VALUES (%s, %s, %s, %s, %s)
            """, ("strategy_research", hypothesis, json.dumps(metrics), decision, reason))


def queue_for_paper_trial(strategy: dict) -> None:
    """AA-08: Submit strategy to paper trial queue."""
    from strategy.lifecycle import create_experimental
    sid = create_experimental(strategy)
    log.info("strategy_queued_for_trial", id=sid)


async def generate_hypothesis_via_airllm(trade_summary: str) -> str:
    """AA-02: Llama 3.1 70B background task — generate strategy hypothesis."""
    from celery_app import research_strategy
    result = research_strategy.delay(
        f"Analyse these trade outcomes and generate a new trading hypothesis: {trade_summary}. "
        "Output as JSON: {hypothesis: str, entry_conditions: list, exit_conditions: list}"
    )
    return result.id   # return Celery task ID; result retrieved asynchronously
