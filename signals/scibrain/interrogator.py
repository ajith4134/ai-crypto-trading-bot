"""SciBrain Layer-4 — the Ollama Scientist Interrogator (the "replace Claude" piece).

For a candidate Decision, it interrogates WHY the circuit chose this direction and hunts
the "signal right, direction wrong" fault (the EDENUSDT post-mortem, automated). It runs the
TradingAgents bull/bear pattern (arXiv:2412.20138) as a DUAL-BRAIN (owner choice 2026-06-08):

  Stage 1 — LEAD SCIENTIST (qwen2.5:14b-instruct): given the module evidence + fusion verdict,
            emit a structured JSON judgement (direction, confidence, key_drivers, counter_evidence,
            wrong_direction_risk, agrees_with_fusion, narrative).
  Stage 2 — RED-TEAM CRITIC (deepseek-r1:8b, explicit reasoning): tries to PROVE the direction
            wrong; returns an adjusted wrong_direction_risk + critique.

Both produce typed JSON + a plain-English narrative shown live on the panel. Uses the bot's
existing llm.ollama_client.chat_ollama. Never raises into the pipeline — on any LLM/transport
failure it returns an `unavailable` Interrogation so scoring still proceeds.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import structlog

from .contracts import Decision

log = structlog.get_logger()

LEAD_MODEL = "qwen2.5:14b-instruct"
CRITIC_MODEL = "deepseek-r1:8b"
# Bump when the audit payload schema changes, so a stored forecast can be matched to the exact
# auditor that produced it (the auditor itself is calibrated/compared/demoted/replaced over time).
AUDIT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Interrogation:
    symbol: str
    available: bool
    verdict_direction: Optional[str]      # 'long'|'short'|'none'
    confidence: float                     # [0,1]
    wrong_direction_risk: float           # [0,1] — high = fusion may have the sign wrong
    agrees_with_fusion: bool
    key_drivers: list
    counter_evidence: list
    narrative: str
    responsible_factor: str = ""          # which module/factor most drove the direction
    critic_note: str = ""
    # --- Living-Intelligence semantics (Phase 7a) -------------------------------------------
    # This audit runs immediately AFTER the open and BEFORE any realized outcome exists. It is
    # therefore an EX-ANTE DECISION-RISK forecast (a probability the direction is wrong), NOT a
    # post-outcome verdict. These two fields travel with every payload so no downstream consumer
    # (dashboard, trade provenance, future calibration grader) can mislabel it as a result audit.
    audit_kind: str = "ex_ante_decision_risk"
    evaluated_at: str = "post_open_pre_outcome"
    # Actual auditor provenance — so the forecaster itself can be calibrated/compared/replaced.
    provider: str = ""          # provider that served the LEAD forecast ('groq'|'cerebras'|'ollama'…)
    model: str = ""             # the actual model that served it
    transport: str = ""         # 'cloud' | 'local'
    prompt_hash: str = ""       # sha1(lead prompt)[:16] — pins the exact prompt/schema that was asked
    schema_version: int = AUDIT_SCHEMA_VERSION
    lead_model: str = LEAD_MODEL
    critic_model: str = CRITIC_MODEL
    elapsed_s: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "available": self.available,
            "verdict_direction": self.verdict_direction,
            "confidence": round(float(self.confidence), 4),
            "wrong_direction_risk": round(float(self.wrong_direction_risk), 4),
            "agrees_with_fusion": self.agrees_with_fusion,
            "audit_kind": self.audit_kind,
            "evaluated_at": self.evaluated_at,
            "provider": self.provider,
            "model": self.model,
            "transport": self.transport,
            "prompt_hash": self.prompt_hash,
            "schema_version": self.schema_version,
            "responsible_factor": self.responsible_factor,
            "key_drivers": self.key_drivers, "counter_evidence": self.counter_evidence,
            "narrative": self.narrative, "critic_note": self.critic_note,
            "lead_model": self.lead_model, "critic_model": self.critic_model,
            "elapsed_s": round(self.elapsed_s, 1), "ts": round(self.ts, 3),
        }


def _evidence_block(decision: Decision) -> str:
    """Compact, model-readable dump of the module evidence behind a decision, with the
    COMPUTED responsibility attribution (which module owns what share of the verdict)."""
    lines = []
    for m in decision.contributing:
        if not m.ok:
            continue
        feats = ", ".join(f"{k}={_fmt(v)}" for k, v in m.features.items()
                          if k not in ("reason",))
        lines.append(
            f"- {m.module}: vote={m.direction:+.2f} conviction={m.conviction:.2f} "
            f"regime={m.regime_tag} | {feats}")
    block = "\n".join(lines) if lines else "- (no module produced usable evidence)"
    if decision.attribution:
        shares = ", ".join(f"{a['module']}={a['share']:+.2f}"
                           f"{'*' if a['aligned'] else ''}" for a in decision.attribution)
        block += (f"\n\nCOMPUTED ATTRIBUTION (share of the net directional vote; "
                  f"* = aligned with the verdict): {shares}\n"
                  f"PRIMARY DRIVER (computed): {decision.primary_driver}")
    return block


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.4f}"
    return v


def _lead_prompt(decision: Decision) -> str:
    return f"""You are the LEAD QUANTITATIVE SCIENTIST auditing an automated trade decision on the crypto perpetual {decision.symbol}.
The decision engine is a circuit of PhD math/physics modules. Here is its verdict and the evidence each module produced:

FUSION VERDICT: direction={decision.direction or 'none'}, conviction={decision.conviction:.2f}, expected_move={decision.expected_move_pct}, regime={decision.regime}

MODULE EVIDENCE:
{_evidence_block(decision)}

Your job: decide whether this DIRECTION (long vs short) is justified by the evidence, and specifically whether the engine could have the SIGN WRONG (right setup, wrong direction) — a known failure mode.

The COMPUTED PRIMARY DRIVER above is the module that mathematically owns the largest share of the verdict. Confirm whether that driver SHOULD be trusted here, or whether it is leading the engine into a wrong-direction trap.

Respond with ONLY a JSON object, no prose before or after, exactly these keys:
{{"verdict_direction": "long|short|none",
 "confidence": 0.0-1.0,
 "agrees_with_fusion": true|false,
 "wrong_direction_risk": 0.0-1.0,
 "responsible_factor": "the single module/factor most responsible for this direction",
 "key_drivers": ["short phrase", ...],
 "counter_evidence": ["short phrase", ...],
 "narrative": "2-3 sentence plain-English explanation, naming WHY the responsible factor drove this direction"}}"""


def _critic_prompt(decision: Decision, lead: dict) -> str:
    return f"""You are a RED-TEAM RISK CRITIC. Another analyst judged a {decision.symbol} trade as direction={lead.get('verdict_direction')} (confidence {lead.get('confidence')}). Their reasoning: {lead.get('narrative')}

Module evidence:
{_evidence_block(decision)}

Try hard to PROVE the direction is WRONG. Consider: mean-reversion vs momentum regime mismatch, a coherent move that is actually exhaustion, conflicting module votes, low/contradicted conviction.

Respond with ONLY a JSON object, exactly:
{{"wrong_direction_risk": 0.0-1.0, "critic_note": "1-2 sentences naming the single biggest reason the direction could be wrong, or 'no strong counter-case'"}}"""


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first balanced JSON object out of an LLM reply (strips reasoning/think
    tags and surrounding prose). Returns None if nothing parseable."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)   # deepseek-r1
    text = text.replace("```json", "").replace("```", "")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start:i + 1]
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _clip01(v, default=0.0) -> float:
    try:
        return float(min(1.0, max(0.0, float(v))))
    except (TypeError, ValueError):
        return default


def _complete(prompt: str, *, max_tokens: int, local_model: str,
              timeout_s: int, keep_alive: str, use_cloud: bool) -> tuple[str, dict]:
    """CLOUD-PRIMARY LLM call with local-Ollama fallback.

    Returns (text, meta) where meta = {transport, provider, model} records WHICH provider/model
    actually served this completion (cloud chain reports the provider; we map it back to its
    configured model; the local fallback reports the ollama model). The caller persists this so
    the auditor itself is attributable and can be calibrated, compared, demoted, or replaced.

    The bot is cloud-primary for LLM (llm.providers.call_chain → groq/cerebras/nvidia,
    70B–235B models, fast, OFF-box) with the local CPU Ollama as the fallback only when
    every cloud provider is in cooldown. The debate council already works this way. The
    scibrain audit historically called local Ollama directly, which on this CPU-only box
    is saturated (see finding-ollama-saturation) → audits timed out. Routing here makes the
    audit fast + higher-quality AND keeps it off the contended local CPU. Falls back to the
    local model so the audit still works when all cloud providers are cooled down."""
    if use_cloud:
        try:
            from llm.providers import call_chain, get_providers
            prov, text = call_chain(prompt, max_tokens=max_tokens, json_mode=True,
                                    timeout=min(timeout_s, 90))
            if text and text.strip():
                # call_chain returns the provider NAME; recover the model it's configured with
                model = next((m for (n, _u, m, _k) in get_providers() if n == prov), prov)
                return text, {"transport": "cloud", "provider": prov, "model": model}
            log.info("scibrain_audit_cloud_empty_fallback_local")
        except Exception as exc:
            # all providers cooled down / no key / transport — fall back to local Ollama
            log.info("scibrain_audit_cloud_unavailable_fallback_local", error=str(exc)[:140])
    from llm.ollama_client import chat_ollama
    text = chat_ollama(prompt, model=local_model, max_tokens=max_tokens,
                       timeout_s=timeout_s, keep_alive=keep_alive)
    return text, {"transport": "local", "provider": "ollama", "model": local_model}


def interrogate(decision: Decision, *, max_tokens: int = 320,
                lead_model: str = LEAD_MODEL, critic_model: str = CRITIC_MODEL,
                timeout_s: int = 600, keep_alive: str = "10m",
                use_cloud: bool = True) -> Interrogation:
    """Run the dual-brain interrogation. Safe: returns `unavailable` on any failure.

    use_cloud (default True): route the LLM calls through the bot's cloud-primary chain
    (llm.providers.call_chain — fast 70B+ models, off-box) with the local Ollama models
    (lead_model/critic_model) as the fallback. This keeps the audit off the saturated local
    CPU; set False (scibrain:audit_use_cloud=0) to force local-only. timeout_s defaults high
    for the slow local CPU path; cloud calls are capped to 90s inside _complete.
    """
    t0 = time.time()
    # ── Stage 1: lead scientist ──────────────────────────────────────────────
    lead_prompt = _lead_prompt(decision)
    prompt_hash = hashlib.sha1(lead_prompt.encode("utf-8")).hexdigest()[:16]
    try:
        lead_raw, lead_meta = _complete(lead_prompt, max_tokens=max_tokens,
                                        local_model=lead_model, timeout_s=timeout_s,
                                        keep_alive=keep_alive, use_cloud=use_cloud)
    except Exception as exc:
        log.warning("scibrain_interrogate_lead_failed", symbol=decision.symbol,
                    error=str(exc)[:160])
        return _unavailable(decision, f"lead_unavailable:{str(exc)[:60]}", time.time() - t0,
                            prompt_hash=prompt_hash)

    lead = _extract_json(lead_raw)
    if not lead:
        return _unavailable(decision, "lead_unparseable", time.time() - t0,
                            prompt_hash=prompt_hash)

    verdict = str(lead.get("verdict_direction", "none")).lower()
    if verdict not in ("long", "short", "none"):
        verdict = "none"
    confidence = _clip01(lead.get("confidence"), 0.0)
    agrees = bool(lead.get("agrees_with_fusion", verdict == (decision.direction or "none")))
    wdr_lead = _clip01(lead.get("wrong_direction_risk"), 0.5)

    # ── Stage 2: red-team critic (deepseek-r1) ───────────────────────────────
    critic_note = ""
    wdr = wdr_lead
    try:
        crit_raw, _crit_meta = _complete(_critic_prompt(decision, lead), max_tokens=max_tokens,
                                         local_model=critic_model, timeout_s=timeout_s,
                                         keep_alive=keep_alive, use_cloud=use_cloud)
        crit = _extract_json(crit_raw)
        if crit:
            critic_note = str(crit.get("critic_note", ""))[:400]
            wdr = float(np.clip(max(wdr_lead, _clip01(crit.get("wrong_direction_risk"),
                                                      wdr_lead)), 0.0, 1.0))
    except Exception as exc:
        log.info("scibrain_interrogate_critic_skipped", symbol=decision.symbol,
                 error=str(exc)[:120])
        critic_note = "(critic unavailable)"

    return Interrogation(
        symbol=decision.symbol, available=True, verdict_direction=verdict,
        confidence=confidence, wrong_direction_risk=wdr,
        agrees_with_fusion=agrees,
        responsible_factor=str(lead.get("responsible_factor",
                                        decision.primary_driver or ""))[:80],
        key_drivers=list(lead.get("key_drivers") or [])[:6],
        counter_evidence=list(lead.get("counter_evidence") or [])[:6],
        narrative=str(lead.get("narrative", ""))[:600],
        critic_note=critic_note, lead_model=lead_model, critic_model=critic_model,
        provider=str(lead_meta.get("provider", "")),
        model=str(lead_meta.get("model", "")),
        transport=str(lead_meta.get("transport", "")),
        prompt_hash=prompt_hash,
        elapsed_s=time.time() - t0,
    )


def _unavailable(decision: Decision, reason: str, elapsed: float,
                 prompt_hash: str = "") -> Interrogation:
    return Interrogation(
        symbol=decision.symbol, available=False,
        verdict_direction=decision.direction, confidence=0.0,
        wrong_direction_risk=0.0, agrees_with_fusion=True,
        responsible_factor=decision.primary_driver or "",
        key_drivers=[], counter_evidence=[],
        narrative=f"interrogation unavailable ({reason})",
        critic_note="", prompt_hash=prompt_hash, elapsed_s=elapsed,
    )
