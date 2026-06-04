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
            # Durable evidence of firing — feature_health checker reads this.
            # The pub/sub event isn't persistent, and the maml:adapt_lock TTLs
            # out after 1h, so we keep a counter + last-pair under their own keys.
            import time as _t
            r.incr("bocpd:fires_count")
            r.set("bocpd:last_fired_ts", str(int(_t.time())))
            r.set("bocpd:last_fired_pair", pair)
            log.info("changepoint_detected", pair=pair)

            # Blueprint L-13: BOCPD changepoint triggers MAML recalibration.
            # Debounce so a multi-pair burst doesn't queue dozens of identical
            # MAML adaptation tasks — 1-hour lockout via Redis SET NX EX.
            try:
                from feature_governance.registry import is_active as _fg_active_f22
                if _fg_active_f22("F22") and r.set(
                    "maml:adapt_lock", "1", nx=True, ex=3600
                ):
                    from celery_app import maml_adapt_on_changepoint
                    maml_adapt_on_changepoint.apply_async()
                    log.info("maml_triggered", pair=pair)
            except Exception as exc:
                log.warning("maml_trigger_failed", error=str(exc)[:200])

            return True
    except Exception as exc:
        log.error("bocpd_error", error=str(exc))
    return False
