"""
L-05 / Blueprint Feature 18 — CryptoBERT + FinBERT Sentiment Engine.

Blueprint Section 4.1 Feature 18 (arXiv:2411.12748):
  CryptoBERT — BERT fine-tuned on crypto-native corpora (Reddit, Telegram,
    Twitter, crypto news). Understands "HODL", "FUD", "rekt", "wen moon".
    +19% prediction accuracy / +2.9% directional accuracy vs generic BERT.
  FinBERT    — BERT fine-tuned on financial news + SEC filings.
  Real-time pipeline:
    News / Reddit / RSS text → CryptoBERT/FinBERT → sentiment scalar in
    [-1, +1] → Redis SENTIMENT_GLOBAL + SENTIMENT_PAIR → signals/engine.py
    direction logic + brain world-model observation.

Status before this rewrite:
  Module existed with score_text() + update_*_sentiment() but had ZERO live
  callers. The Fear & Greed Index proxy (data/feed.py:_poll_fear_greed) was
  the only writer of SENTIMENT_GLOBAL — and F&G has been stuck at 27
  (extreme fear) for the entire production window, causing the D-03 bull-
  short bias root cause documented in PROGRESS.md cont. 3.

What this rewrite changes:
  1. Replaces the slow `pipeline()` call path with direct AutoTokenizer +
     AutoModelForSequenceClassification — supports real batching (one
     forward pass for N texts) instead of N separate pipeline calls.
  2. Adds `score_batch(texts)` for efficient large-batch use from the
     periodic Celery task.
  3. Persists durable evidence keys (`sentiment:real_last_update_ts`,
     `sentiment:n_samples`, `sentiment:source`) so:
       - data/feed.py:_poll_fear_greed can DEFER to real sentiment when
         fresh (no proxy overwrite within the freshness window).
       - tools/feature_health.py can show 'real CryptoBERT+FinBERT' vs
         'F&G proxy' vs 'stale'.
  4. Per-pair sentiment uses web_intelligence.pairs_affected tagging from
     web_intel/collector.py — already populated by the LLM-extraction step
     so we get free per-pair routing.

Memory budget (celery_worker, 3G limit):
  CryptoBERT (kk08/CryptoBERT)   ~440 MB
  FinBERT    (ProsusAI/finbert)  ~440 MB
  Tokenizer overhead             ~ 30 MB
  Total                          ~1.0 GB resident. Leaves ~2GB for the rest
  of the celery workload.

Inference cost (CPU, batch_size=32, seq_len=128):
  ~50ms per item single-call, ~5-8ms per item in a 32-batch.
"""
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()

_MAX_LEN = 256          # BERT max is 512; 256 covers 99% of headlines + comments
_BATCH_SIZE = 32
_FRESHNESS_SECONDS = 30 * 60   # F&G proxy defers to real sentiment for this window

# Lazy-loaded singletons — loading both models takes ~10s on first call.
_crypto_tok = None
_crypto_mod = None
_fin_tok = None
_fin_mod = None


def _load_models() -> None:
    """One-time load. Called inside score_text/score_batch on first use."""
    global _crypto_tok, _crypto_mod, _fin_tok, _fin_mod
    if _crypto_mod is not None and _fin_mod is not None:
        return
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    import torch
    log.info("sentiment_models_loading_begin")
    if _crypto_mod is None:
        _crypto_tok = AutoTokenizer.from_pretrained("kk08/CryptoBERT")
        _crypto_mod = AutoModelForSequenceClassification.from_pretrained("kk08/CryptoBERT")
        _crypto_mod.eval()
    if _fin_mod is None:
        _fin_tok = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        _fin_mod = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        _fin_mod.eval()
    log.info("sentiment_models_loaded",
             crypto_labels=_crypto_mod.config.id2label,
             fin_labels=_fin_mod.config.id2label)


def _label_to_signed_score(label: str, prob: float) -> float:
    """Map a 3-class output to a scalar in [-1, +1].
    CryptoBERT labels: LABEL_0=negative, LABEL_1=neutral, LABEL_2=positive
                      (some checkpoints use {Bearish, Neutral, Bullish})
    FinBERT labels:   positive, negative, neutral
    """
    lab = label.lower()
    if any(k in lab for k in ("posit", "bull", "label_2")):
        return float(prob)
    if any(k in lab for k in ("negat", "bear", "label_0")):
        return -float(prob)
    return 0.0   # neutral / label_1


def _score_with_model(texts: list[str], tok, mod,
                      model_name: str | None = None) -> list[float]:
    """Run a single model over a list of texts; return signed scores in [-1, +1].
    Batched forward pass; falls back to neutral on per-batch error.

    `model_name` ('cryptobert' or 'finbert') is used to increment a per-model
    inference counter in Redis so the dashboard /models endpoint can report
    a live 'pretrained' status. Optional for backward compat — when omitted,
    counters are not bumped."""
    if not texts:
        return []
    import torch
    out: list[float] = []
    successful = 0
    for i in range(0, len(texts), _BATCH_SIZE):
        chunk = texts[i:i + _BATCH_SIZE]
        try:
            enc = tok(chunk, padding=True, truncation=True,
                      max_length=_MAX_LEN, return_tensors="pt")
            with torch.no_grad():
                logits = mod(**enc).logits
            probs = torch.softmax(logits, dim=-1).cpu().numpy()
            id2label = mod.config.id2label
            for row in probs:
                cls = int(row.argmax())
                out.append(_label_to_signed_score(id2label[cls], float(row[cls])))
            successful += len(chunk)
        except Exception as exc:
            log.warning("sentiment_batch_failed", err=str(exc)[:200],
                        batch_size=len(chunk))
            out.extend([0.0] * len(chunk))
    if model_name and successful > 0:
        try:
            r = redis_client.get()
            r.incrby(f"ml:sentiment:{model_name}_inference_count", successful)
            r.set(f"ml:sentiment:{model_name}_last_ts", int(__import__('time').time()))
        except Exception:
            pass
    return out


def score_text(text: str, source_type: str = "news") -> float:
    """Return sentiment score in [-1, +1]. Routes to CryptoBERT for social
    text (reddit/twitter/telegram), FinBERT for news. Empty / non-string → 0."""
    if not isinstance(text, str) or not text.strip():
        return 0.0
    _load_models()
    if source_type in ("social", "reddit", "twitter", "telegram"):
        return _score_with_model([text], _crypto_tok, _crypto_mod, "cryptobert")[0]
    return _score_with_model([text], _fin_tok, _fin_mod, "finbert")[0]


def score_batch(items: list[dict]) -> list[dict]:
    """Score a heterogeneous batch of items.

    items: list of {text: str, source_type: str ('news'|'social'|'reddit'|...)}
    returns: same list with added 'crypto_score', 'fin_score', 'combined' fields.

    Both models run on every text so we get parallel views (financial-news
    framing vs crypto-native framing). 'combined' is a source-weighted blend:
      news / rss  → 0.3 * crypto + 0.7 * fin
      social      → 0.7 * crypto + 0.3 * fin
      other       → 0.5 * crypto + 0.5 * fin

    This is the per-item building block consumed by the Celery beat task in
    celery_app.py which then aggregates into SENTIMENT_GLOBAL + SENTIMENT_PAIR.
    """
    if not items:
        return []
    _load_models()
    texts = [it.get("text", "") or "" for it in items]
    crypto_scores = _score_with_model(texts, _crypto_tok, _crypto_mod, "cryptobert")
    fin_scores = _score_with_model(texts, _fin_tok, _fin_mod, "finbert")

    out = []
    for it, cs, fs in zip(items, crypto_scores, fin_scores):
        st = (it.get("source_type") or "news").lower()
        if st in ("social", "reddit", "twitter", "telegram"):
            combined = 0.7 * cs + 0.3 * fs
        elif st in ("news", "rss", "press"):
            combined = 0.3 * cs + 0.7 * fs
        else:
            combined = 0.5 * cs + 0.5 * fs
        out.append({
            **it,
            "crypto_score": round(cs, 4),
            "fin_score": round(fs, 4),
            "combined": round(combined, 4),
        })
    return out


def update_global_sentiment(items: list[dict]) -> dict:
    """Score a batch, average the combined column, write to Redis on the
    0..1 scale (preserves compatibility with existing F&G consumers).
    Stamps freshness keys so _poll_fear_greed defers.

    Returns a summary dict for logging.
    """
    if not items:
        return {"status": "no_items", "n": 0}
    scored = score_batch(items)
    if not scored:
        return {"status": "scoring_failed", "n": 0}
    avg_signed = sum(s["combined"] for s in scored) / len(scored)
    normalized = round((avg_signed + 1) / 2, 4)   # -1..+1 → 0..1

    import time as _t
    r = redis_client.get()
    r.set(redis_keys.SENTIMENT_GLOBAL, normalized)
    r.set("sentiment:real_last_update_ts", str(int(_t.time())))
    r.set("sentiment:n_samples", str(len(scored)))
    r.set("sentiment:source", "cryptobert+finbert")
    r.set("sentiment:last_avg_signed", str(round(avg_signed, 4)))
    log.info("sentiment_global_updated",
             n=len(scored), avg_signed=round(avg_signed, 4),
             normalized=normalized)
    return {"status": "updated", "n": len(scored),
            "avg_signed": round(avg_signed, 4), "normalized": normalized}


def update_pair_sentiment(pair: str, items: list[dict]) -> dict:
    """Per-pair update. Items should be pre-filtered by web_intel pairs_affected.
    Writes to SENTIMENT_PAIR on the 0..1 scale + a freshness ts so consumers
    can prefer real per-pair sentiment over the global fallback."""
    if not pair or not items:
        return {"status": "no_items", "pair": pair, "n": 0}
    scored = score_batch(items)
    if not scored:
        return {"status": "scoring_failed", "pair": pair, "n": 0}
    avg_signed = sum(s["combined"] for s in scored) / len(scored)
    normalized = round((avg_signed + 1) / 2, 4)

    import time as _t
    r = redis_client.get()
    # cont. 69: TTL the per-pair sentiment so STALE values decay instead of
    # lingering forever. Only ~5 pairs/run get a fresh score (thin news feed),
    # so without a TTL the other ~370 :sentiment keys (no expiry) drove the
    # entry sentiment gate on hours-old data. 6h TTL → a pair not mentioned in
    # news for 6h drops its per-pair key and the gate fails open for it
    # (engine.py reads `{pair}:sentiment` and skips the gate when absent).
    _SENT_TTL = 21600  # 6h
    r.setex(redis_keys.SENTIMENT_PAIR.replace("{pair}", pair), _SENT_TTL, normalized)
    r.setex(f"sentiment:pair:{pair}:last_ts", _SENT_TTL, str(int(_t.time())))
    r.setex(f"sentiment:pair:{pair}:n_samples", _SENT_TTL, str(len(scored)))
    return {"status": "updated", "pair": pair, "n": len(scored),
            "avg_signed": round(avg_signed, 4), "normalized": normalized}


def real_sentiment_fresh() -> bool:
    """True iff CryptoBERT+FinBERT has written within FRESHNESS_SECONDS.
    Used by data/feed.py:_poll_fear_greed to decide whether to overwrite
    SENTIMENT_GLOBAL with the F&G proxy — when real sentiment is fresh, we
    let it stand. Otherwise the proxy fills the gap."""
    try:
        import time as _t
        last = redis_client.get().get("sentiment:real_last_update_ts")
        if not last:
            return False
        return (int(_t.time()) - int(last)) < _FRESHNESS_SECONDS
    except Exception:
        return False
