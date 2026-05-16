"""L-06: Bayesian Online Changepoint Detection using ruptures."""
import json
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()
_price_buffer: dict[str, list] = {}


def update(pair: str, price: float) -> bool:
    """Add latest price; detect changepoint; return True if detected."""
    if pair not in _price_buffer:
        _price_buffer[pair] = []
    _price_buffer[pair].append(price)
    if len(_price_buffer[pair]) > 500:
        _price_buffer[pair] = _price_buffer[pair][-500:]
    if len(_price_buffer[pair]) < 50:
        return False
    try:
        import ruptures as rpt
        import numpy as np
        signal = np.array(_price_buffer[pair])
        algo = rpt.Binseg(model="rbf").fit(signal)
        bkps = algo.predict(n_bkps=1)
        last_bkp = bkps[0] if bkps else 0
        if last_bkp >= len(signal) - 5:
            r = redis_client.get()
            r.publish(redis_keys.CH_SYSTEM_EVENT, json.dumps({
                "event": "changepoint_detected", "pair": pair,
            }))
            log.info("changepoint_detected", pair=pair)
            return True
    except Exception as exc:
        log.error("bocpd_error", error=str(exc))
    return False
