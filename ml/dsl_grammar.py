"""
F54 — DSL grammar for the Crypto LLM-DSL Alpha Miner (cont. 55, 2026-05-28).

Implements the arXiv:2604.26747 "From Hypotheses to Factors" formulaic-alpha
grammar with crypto-specific terminal extensions.

Grammar (S-expression syntax, easy for LLMs to emit and humans to read):

  Expr      := Op | Terminal | Const
  Op        := (OpName Arg Arg ...)
  Terminal  := close | open | high | low | volume | vwap | ofi | vpin |
               funding_rate | bid_ask_imbalance | kyles_lambda |
               exchange_netflow_z | turbulence_index | mark_price | sentiment
  Const     := real number in [-2.0, 2.0]
  Window    := integer ∈ {5, 10, 20, 30, 60, 120, 240, 480, 720, 1440}

Operators:
  Arithmetic:  add sub mul div
  Unary:       log abs sign neg sqrt
  Time series: ts_mean ts_std ts_min ts_max ts_rank ts_skew ts_kurt
               ts_zscore ts_argmax ts_argmin ts_corr ts_cov ts_decay
  Cross:       rank scale clip
  Logical:     where_gt where_lt

Depth bounded at 6. Search space ≈ 10^15 — the LLM acts as a learned prior
sampling the productive sub-space.

This module ONLY parses, validates, and pretty-prints. Actual evaluation
against OHLCV arrays is in `ml/dsl_evaluator.py`.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


# ---- Allowed-symbol sets ----------------------------------------------------

TERMINALS = frozenset({
    "open", "high", "low", "close", "volume", "vwap",
    "ofi", "vpin", "kyles_lambda", "bid_ask_imbalance",
    "funding_rate", "exchange_netflow_z", "turbulence_index",
    "mark_price", "sentiment",
})

WINDOWS = (5, 10, 20, 30, 60, 120, 240, 480, 720, 1440)

# Operators with their declared arity (excluding the window argument where applicable).
# (name, n_data_args, takes_window)
OPS: dict[str, tuple[int, bool]] = {
    "add":         (2, False),
    "sub":         (2, False),
    "mul":         (2, False),
    "div":         (2, False),
    "log":         (1, False),
    "abs":         (1, False),
    "sign":        (1, False),
    "neg":         (1, False),
    "sqrt":        (1, False),
    "ts_mean":     (1, True),
    "ts_std":      (1, True),
    "ts_min":      (1, True),
    "ts_max":      (1, True),
    "ts_rank":     (1, True),
    "ts_skew":     (1, True),
    "ts_kurt":     (1, True),
    "ts_zscore":   (1, True),
    "ts_argmax":   (1, True),
    "ts_argmin":   (1, True),
    "ts_decay":    (1, True),
    "ts_corr":     (2, True),
    "ts_cov":      (2, True),
    "rank":        (1, False),
    "scale":       (1, False),
    "clip":        (3, False),   # clip(x, lo, hi)
    "where_gt":    (3, False),   # where_gt(x, threshold, then_value)
    "where_lt":    (3, False),
}

MAX_DEPTH = 6
CONST_MIN = -2.0
CONST_MAX = 2.0


# ---- AST node types ---------------------------------------------------------

@dataclass(frozen=True)
class Const:
    value: float

    def to_sexp(self) -> str:
        return f"{self.value:g}"


@dataclass(frozen=True)
class Terminal:
    name: str

    def to_sexp(self) -> str:
        return self.name


@dataclass(frozen=True)
class Op:
    name: str
    args: tuple  # tuple of (Const|Terminal|Op)
    window: int | None = None

    def to_sexp(self) -> str:
        head = self.name
        body = " ".join(a.to_sexp() for a in self.args)
        if self.window is not None:
            return f"({head} {body} {self.window})"
        return f"({head} {body})"


Node = Const | Terminal | Op


# ---- Parser -----------------------------------------------------------------

class DSLParseError(ValueError):
    pass


def _tokenize(s: str) -> list[str]:
    """Whitespace + parens tokenizer."""
    s = s.replace("(", " ( ").replace(")", " ) ")
    return [t for t in s.split() if t]


def _parse_atom(tok: str) -> Node:
    """Parse a non-list token as a terminal or constant.

    Const range is NOT enforced here — windowed ops consume their trailing
    arg as an integer window (e.g. 20, 60, 1440) which legitimately exceeds
    the [-2.0, 2.0] data-constant range. The range check is applied per-use
    in `validate()` so a Const that ends up as a real arithmetic operand
    still gets bounds-checked.
    """
    if tok in TERMINALS:
        return Terminal(tok)
    try:
        v = float(tok)
        return Const(v)
    except ValueError:
        raise DSLParseError(f"unknown_atom: {tok!r}")


def _parse(tokens: list[str], depth: int = 0) -> tuple[Node, list[str]]:
    if depth > MAX_DEPTH:
        raise DSLParseError(f"depth_exceeded ({depth} > {MAX_DEPTH})")
    if not tokens:
        raise DSLParseError("empty_expr")
    head = tokens[0]
    rest = tokens[1:]
    if head != "(":
        # Atom
        return _parse_atom(head), rest
    # List form: (opname arg1 arg2 ... [window])
    if not rest:
        raise DSLParseError("unclosed_paren")
    op_name = rest[0]
    body = rest[1:]
    if op_name not in OPS:
        raise DSLParseError(f"unknown_op: {op_name!r}")
    arity, takes_window = OPS[op_name]
    args: list[Node] = []
    while body and body[0] != ")":
        node, body = _parse(body, depth + 1)
        args.append(node)
    if not body or body[0] != ")":
        raise DSLParseError(f"unclosed_paren_in_op:{op_name}")
    body = body[1:]  # consume ')'
    # If the op takes a window, the trailing arg is the window int.
    window = None
    if takes_window:
        if len(args) < arity + 1:
            raise DSLParseError(f"missing_window_for_op:{op_name}")
        win_node = args[-1]
        args = args[:-1]
        if not isinstance(win_node, Const):
            raise DSLParseError(f"window_must_be_const_int_op:{op_name}")
        w = int(win_node.value)
        if w not in WINDOWS:
            raise DSLParseError(f"window_not_allowed:{w}")
        window = w
    if len(args) != arity:
        raise DSLParseError(
            f"arity_mismatch op={op_name} expected={arity} got={len(args)}")
    return Op(name=op_name, args=tuple(args), window=window), body


def parse(s: str) -> Node:
    """Parse an S-expression string into a DSL AST. Raises DSLParseError."""
    tokens = _tokenize(s.strip())
    node, rest = _parse(tokens)
    if rest:
        raise DSLParseError(f"trailing_tokens: {rest[:4]}")
    return node


# ---- Validation -------------------------------------------------------------

def validate(node: Node, max_depth: int = MAX_DEPTH) -> tuple[bool, str | None]:
    """Return (ok, reason). Used by the miner before sending to evaluator.

    Const-range check: any Const that survives parsing as a data operand
    (i.e. not consumed as the trailing window arg of a windowed op) MUST
    lie within [CONST_MIN, CONST_MAX]. Window args are integer values from
    the WINDOWS allowed set and already validated in `_parse`.
    """
    def _walk(n: Node, depth: int) -> tuple[bool, str | None]:
        if depth > max_depth:
            return False, f"depth_exceeded ({depth})"
        if isinstance(n, Const):
            if not (CONST_MIN <= n.value <= CONST_MAX):
                return False, f"const_out_of_range:{n.value}"
            return True, None
        if isinstance(n, Terminal):
            return True, None
        if isinstance(n, Op):
            arity, takes_window = OPS[n.name]
            if takes_window and n.window is None:
                return False, f"missing_window_op:{n.name}"
            if not takes_window and n.window is not None:
                return False, f"unexpected_window_op:{n.name}"
            if len(n.args) != arity:
                return False, f"arity_op:{n.name}"
            for a in n.args:
                ok, why = _walk(a, depth + 1)
                if not ok: return False, why
            return True, None
        return False, "unknown_node_type"
    return _walk(node, 0)


def required_terminals(node: Node) -> set[str]:
    """Return the set of terminal symbols this expression references.

    Used by the evaluator to short-circuit when a terminal isn't available
    for a given pair (e.g. ofi-less pair → skip OFI-using factors).
    """
    out: set[str] = set()
    def _walk(n: Node):
        if isinstance(n, Terminal):
            out.add(n.name)
        elif isinstance(n, Op):
            for a in n.args: _walk(a)
    _walk(node)
    return out


def max_window(node: Node) -> int:
    """Return the max window used anywhere in the tree (history requirement)."""
    out = [0]
    def _walk(n: Node):
        if isinstance(n, Op):
            if n.window is not None:
                out[0] = max(out[0], n.window)
            for a in n.args: _walk(a)
    _walk(node)
    return out[0]


def factor_hash(s_expr: str) -> str:
    """Deterministic 8-char hash for promotion IDs (F54_<hash>)."""
    return hashlib.sha1(s_expr.encode("utf-8")).hexdigest()[:8]


def serialize(node: Node) -> str:
    """AST → S-expression string. Inverse of parse()."""
    return node.to_sexp()


# ---- Grammar prompt builder -------------------------------------------------

def grammar_prompt_block() -> str:
    """Return a string describing the grammar — embed in the LLM mining prompt.

    The wording is structured for code-LLMs: BNF + examples + constraints.
    Total ~500 tokens.
    """
    terminals = ", ".join(sorted(TERMINALS))
    windows = ", ".join(str(w) for w in WINDOWS)
    ops_lines = []
    for op, (arity, takes_window) in sorted(OPS.items()):
        w = " [window]" if takes_window else ""
        ops_lines.append(f"  ({op} {'arg ' * arity}{w})")
    ops = "\n".join(ops_lines)
    return f"""\
You are mining novel formulaic alpha factors for crypto perpetual futures.

GRAMMAR (S-expression syntax):
  Expr     := (op_name arg1 arg2 ... [window])
            | terminal
            | constant
  terminal in {{{terminals}}}
  constant in [-2.0, 2.0]
  window   in {{{windows}}}
  max_depth = {MAX_DEPTH}

OPERATORS:
{ops}

EXAMPLES of valid factors:
  Body-vs-volume divergence (5-bar):
    (ts_corr (sub close open) volume 5)

  Liquidity-weighted reversal (20-bar):
    (mul (sign (sub (ts_mean close 20) close)) (ts_zscore volume 20))

  OFI persistence (30-bar):
    (ts_decay ofi 30)

  Funding-rate × momentum cross (60-bar):
    (mul (sign funding_rate) (sub close (ts_mean close 60)))

  Net-flow gated mean-reversion:
    (mul (sub (ts_mean close 60) close) (neg exchange_netflow_z))

CONSTRAINTS:
  - Output ONLY the S-expression. No prose, no markdown.
  - Each operator must use the exact name from OPERATORS.
  - Windowed operators (ts_*) MUST include a window arg from the allowed set.
  - Non-windowed operators MUST NOT include a window arg.
  - Constants MUST lie in [-2.0, 2.0]; bigger numbers are forbidden.
  - Tree depth MUST NOT exceed {MAX_DEPTH}.
"""
