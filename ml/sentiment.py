"""L-05: CryptoBERT + FinBERT sentiment scoring."""
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()

_crypto_model = None
_fin_model = None


def _load_models():
    global _crypto_model, _fin_model
    if _crypto_model is None:
        from transformers import pipeline
        _crypto_model = pipeline("text-classification", model="kk08/CryptoBERT", truncation=True)
        _fin_model = pipeline("text-classification", model="ProsusAI/finbert", truncation=True)


def score_text(text: str, source_type: str = "news") -> float:
    """Return sentiment score -1.0 (bearish) to +1.0 (bullish)."""
    try:
        _load_models()
        model = _crypto_model if source_type in ("social", "reddit") else _fin_model
        result = model(text[:512])[0]
        label = result["label"].lower()
        score = result["score"]
        if "positive" in label or "bullish" in label:
            return score
        elif "negative" in label or "bearish" in label:
            return -score
        return 0.0
    except Exception as exc:
        log.error("sentiment_score_failed", error=str(exc))
        return 0.0


def update_pair_sentiment(pair: str, texts: list[str], source_type: str = "news") -> None:
    if not texts:
        return
    scores = [score_text(t, source_type) for t in texts]
    avg = sum(scores) / len(scores)
    normalized = (avg + 1) / 2   # scale to 0–1
    r = redis_client.get()
    r.set(redis_keys.SENTIMENT_PAIR.replace("{pair}", pair), normalized)


def update_global_sentiment(texts: list[str]) -> None:
    if not texts:
        return
    scores = [score_text(t) for t in texts]
    avg = (sum(scores) / len(scores) + 1) / 2
    redis_client.get().set(redis_keys.SENTIMENT_GLOBAL, avg)
