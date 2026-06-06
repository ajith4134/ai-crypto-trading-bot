"""
Section T: Signal Engine & Rejected Signal Scanner — T-01 to T-07.
"""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
import structlog
import redis_client
import redis_keys
import config
from memory.write import write_signal, write_counterfactual

log = structlog.get_logger()


def generate_candidate_signals(pair: str, brain_state: dict) -> list[dict]:
    """T-01: Aggregate ML/indicator outputs into candidate signals for one pair."""
    r = redis_client.get()

    regime = r.get(redis_keys.CURRENT_REGIME) or "unknown"
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
    # Per-pair sentiment first, then global. After 2026-05-20 cont. 9 both keys
    # are preferentially written by ml/sentiment.py (real CryptoBERT+FinBERT);
    # data/feed.py:_poll_fear_greed only writes the F&G proxy as a fallback
    # when no real value has been published in the last 30 minutes. So this
    # read is automatically real-when-fresh, proxy-when-stale.
    _sent_raw = (r.get(redis_keys.SENTIMENT_PAIR.replace("{pair}", pair))
                 or r.get(redis_keys.SENTIMENT_GLOBAL))
    sentiment = float(_sent_raw) if _sent_raw else 0.5
    # Tag the consumed sentiment source so the dashboard / counterfactual sweep
    # can compare D-03 behaviour under real vs proxy sentiment. The
    # sentiment:source Redis key is owned by ml/sentiment.py (sets
    # 'cryptobert+finbert' on update) and data/feed.py (sets 'fear_greed_proxy'
    # only when real is stale).
    try:
        _sent_source = r.get("sentiment:source") or "unknown"
        r.incr(f"sentiment:consume_count:{_sent_source}")
    except Exception:
        _sent_source = "unknown"
    ofi = float(r.get(redis_keys.OFI.replace("{pair}", pair)) or 0)
    vpin = float(r.get(redis_keys.VPIN.replace("{pair}", pair)) or 0)

    if mark <= 0:
        return []

    # Blueprint F19/F20: TFT + PatchTST price forecasts — directional bias from model
    # If q50 forecast > mark → bullish; < mark → bearish. Adjusts signal_strength bonus.
    # F30 Feature Governance: skip TFT call if feature has been deactivated.
    tft_bias = 0.0
    try:
        from feature_governance.registry import is_active as _fg_active
        if not _fg_active("F19"):
            tft_bias = 0.0
        else:
            from ml.tft import get_price_forecast
            forecast = get_price_forecast(pair, "1h")
            if forecast and mark > 0:
                q50 = float(forecast.get("q50", mark))
                tft_bias = (q50 - mark) / mark  # positive = bullish, negative = bearish
    except Exception:
        pass

    # Stage 1 data collection: use OFI direction (momentum-based)
    # Stage 2+: require sentiment confirmation too
    ofi_strength = round(min(100, abs(ofi) * 10000), 2)

    if brain_state.get("stage", 1) <= 1:
        # Stage 1: OFI momentum — direction = positive OFI = long, negative = short
        if ofi > 0.0001:
            direction = "long"
        elif ofi < -0.0001:
            direction = "short"
        else:
            return []
        # trade_potential = OFI strength (how strong the momentum signal is)
        # direction_confidence = OFI + VPIN agreement (VPIN confirms informed directional flow)
        vpin_strength = round(min(100, vpin * 10000), 2)
        trade_potential = ofi_strength
        direction_conf  = round((ofi_strength + vpin_strength) / 2, 2)
    else:
        # D-03 / cont. 24 (2026-05-22): OFI sign drives direction, regime is a
        # CONFIDENCE INPUT not a hard gate. Prior version (D-03) made `regime`
        # a hard dictator with extreme-contrarian escape hatches
        # (`ofi < -0.002 AND sentiment < 0.30` for shorts in bull regime).
        # In practice the contrarian gate is unreachable — sentiment 0.43
        # for weeks, ofi rarely < -0.002 — so 99% of accepted signals in a
        # bull regime were long. Audit of 1555 closed trades showed 1002
        # direction failures (64%) under regime-dictator rules.
        #
        # New rule: OFI sign is the primary direction picker (microstructure
        # flow is the truest near-term direction signal). Regime concurrence
        # already earns a +15 strength bonus at the line below; regime
        # disagreement subtracts strength via the composite scoring, so the
        # downstream strength filter naturally tightens for against-regime
        # signals without hard-gating them out.
        #
        # Symmetric OFI thresholds (raised in turbulence). cont. 65k — now
        # Redis-tunable so trade volume can be adjusted live without a redeploy.
        # Default lowered 0.0005→0.0003 to clear more pairs of the noise floor in
        # calm markets → more signals → faster post-fix data accumulation for the
        # Layer 2a fusion model. On PAPER; bounded SL/TP + Layer 1 veto protect each
        # trade. Raise back via risk:ofi_min when quality matters more than volume.
        try:
            _OFI_MIN = float(r.get("risk:ofi_min") or 0.0003)
            _OFI_TURBULENT_MIN = float(r.get("risk:ofi_turbulent_min") or 0.0010)
        except (TypeError, ValueError):
            _OFI_MIN, _OFI_TURBULENT_MIN = 0.0003, 0.0010
        if regime == "turbulent":
            if abs(ofi) < _OFI_TURBULENT_MIN:
                return []
        elif abs(ofi) < _OFI_MIN:
            return []

        # cont. 65k-6 — MINIMUM-VOLATILITY filter. The bot was picking DEAD/flat
        # pairs (median vol_unit = 0.5%, the floor — no real movement) because the
        # lowered OFI threshold lets illiquid pairs' noise-OFI through. A dead pair
        # never reaches TP and ties up capital. Skip pairs whose vol_unit is at/near
        # the 0.5% floor — require genuine volatility. Redis: risk:min_vol_unit
        # (default 0.006 = 0.6%, just above the floor). Set 0 to disable.
        try:
            _min_vol = float(r.get("risk:min_vol_unit") or 0.006)
        except (TypeError, ValueError):
            _min_vol = 0.006
        if _min_vol > 0:
            try:
                from risk.manager import _volatility_unit as _vu
                if _vu(r, pair) < _min_vol:
                    try:
                        r.incr("signal:reject:low_volatility")
                    except Exception:
                        pass
                    return []
            except Exception:
                pass

        # cont. 65 — Pre-Open Candle Gate (PCG). Master switch
        # `pcg:enabled = "1"` enforces "no candle direction → no trade":
        # required 5m/15m forecasts must be live AND the strict cascade must
        # agree before this signal can fire. Held signals return [] this
        # tick; the brain's next decide cycle calls compute_signal again and
        # PCG re-checks. PCG-disabled state returns ("ready", None, ...)
        # so we fall through to the legacy cascade — never invents a
        # direction on its own.
        _pcg_used = False
        _cascade_audit = {}  # Initialize for feature_vector inclusion later
        try:
            from signals.pre_open_candle_gate import check as _pcg_check
            _pcg_verdict, _pcg_dir, _pcg_conf, _pcg_audit = _pcg_check(
                pair, ofi=ofi, regime=regime, tft_bias=tft_bias)
            # cont. 65k — MISSING-FORECAST FALLBACK (Redis toggle
            # pcg:missing_forecast_fallback, default "1"). The OFI-eligible pairs
            # are largely illiquid ones with NO 5m/15m forecast, so PCG holds them
            # in "wait" up to 600s → no trade → no data. When the verdict is
            # wait/drop *because the forecast is MISSING* (status waiting /
            # dropped_timeout), fall THROUGH to the cascade (which itself falls to
            # the balanced soft-lean / OFI), so trades open + data accumulates. A
            # cascade that has forecasts and actively DISAGREES still drops below.
            # Set the key "0" to restore the strict hold.
            _pcg_status = (_pcg_audit or {}).get("status", "")
            _missing = _pcg_status in ("waiting", "dropped_timeout")
            _mf_fallback = True
            try:
                _mf_fallback = (r.get("pcg:missing_forecast_fallback") or "1") == "1"
            except Exception:
                pass
            if _pcg_verdict in ("wait", "drop") and _missing and _mf_fallback:
                # No forecast → don't block; fall through to cascade/OFI below.
                try:
                    r.incr("pcg:missing_forecast_fallback_used")
                except Exception:
                    pass
            elif _pcg_verdict == "wait":
                return []
            elif _pcg_verdict == "drop":
                # Forecast PRESENT but cascade vetoed (real disagreement) → drop.
                return []
            elif _pcg_verdict == "ready" and _pcg_dir is not None:
                # PCG decided. Use its direction + cascade confidence.
                direction = _pcg_dir
                cascade_confidence = _pcg_conf
                _cascade_audit = _pcg_audit
                _pcg_used = True
        except Exception as _pcg_exc:
            log.warning("pcg_failed_fallback_cascade", pair=pair,
                        error=str(_pcg_exc)[:200])

        # F50b — Multi-TF hierarchical direction cascade (cont. 50).
        # Replaces the bare `direction = long if ofi>0 else short` picker
        # with a cascade across 1h / 15m / 5m CandleNet votes + 1h TFT
        # bias. ≥3 TFs agreeing wins; split → OFI tiebreaker; 4h macro
        # opposite → veto. Falls back to OFI cleanly when forecasts are
        # missing (cold start, F48 not yet trained).
        # cont. 65 — when PCG (above) decided the direction, skip the
        # legacy non-strict cascade entirely. Otherwise (PCG disabled or
        # PCG crashed) fall through to the original cascade behavior.
        if not _pcg_used:
            try:
                from signals.multi_tf_cascade import pick_direction_cascade
                _cascade_dir, _cascade_conf, _cascade_audit = \
                    pick_direction_cascade(pair, ofi=ofi, regime=regime,
                                           tft_bias=tft_bias)
                if _cascade_dir is None:
                    # Macro 4h veto — suppress this signal entirely.
                    try:
                        r.incr("cascade:signals_vetoed")
                    except Exception:
                        pass
                    log.info("signal_vetoed_by_cascade", pair=pair,
                             audit=_cascade_audit)
                    return []
                direction = _cascade_dir
                cascade_confidence = _cascade_conf
            except Exception as _exc:
                # Cascade module crashed — fall back to OFI-primary picker.
                log.warning("cascade_failed_fallback_ofi", pair=pair,
                            error=str(_exc)[:200])
                direction = "long" if ofi > 0 else "short"
                cascade_confidence = 50.0
                _cascade_audit = {"status": "cascade_exception"}

        # Blueprint F13 / X-09 / §10.9: bidirectional Direction Prediction
        # Model (cont. 25). The model can still flip the cascade pick when
        # it's strongly confident the opposite side wins (margin > 0.10).
        # F13's input view is per-pair microstructure + history; the
        # cascade's view is multi-TF candle structure. When they disagree
        # with high F13 confidence, the cascade may be picking a setup
        # that this specific pair historically loses on — trust the
        # learned per-pair model. F30 governance gate respected.
        try:
            from feature_governance.registry import is_active as _fg_active_f13
            _paper_closed_f13 = int(r.get("brain:paper_closed") or 0)
            if _paper_closed_f13 >= 100 and _fg_active_f13("F13"):
                from ml.direction_model import predict_best_direction
                _best = predict_best_direction(pair)
                if _best is not None and _best[0] != direction:
                    log.info("direction_flipped_by_f13", pair=pair,
                             was=direction, now=_best[0],
                             model_conf=_best[1],
                             cascade_conf=cascade_confidence,
                             ofi=ofi, regime=regime)
                    try:
                        r.incr("direction_model:flip_count")
                    except Exception:
                        pass
                    direction = _best[0]
        except Exception as exc:
            log.debug("direction_flip_skipped", error=str(exc)[:120])

        # cont. 65k LAYER 1 — MICROSTRUCTURE ENTRY VETO. The candle cascade is
        # lagging; the order-book jump detector (signals/microstructure.py) catches
        # sudden moves first. If a FRESH, STRONG order-flow jump is starting AGAINST
        # our intended direction, skip the entry — a sudden adverse move is beginning
        # that the candles won't show yet (buying right before a dump / shorting into
        # a rip). Defensive v1. Toggle micro:veto_enabled ("1" default);
        # micro:veto_threshold (jump_score floor, default 60). Freshness via ts.
        try:
            if (r.get("micro:veto_enabled") or "1") == "1":
                _mj = r.get(f"{pair}:micro:jump_score")
                _md = r.get(f"{pair}:micro:direction")
                _mts = r.get(f"{pair}:micro:ts")
                if _mj is not None and _md and _mts is not None:
                    _fresh = (time.time() - float(_mts)) <= 90
                    _thr = float(r.get("micro:veto_threshold") or 60)
                    if _fresh and float(_mj) >= _thr and _md in ("long", "short") \
                            and _md != direction:
                        try:
                            r.incr("micro:veto:count")
                            r.incr(f"micro:veto:{direction}")
                        except Exception:
                            pass
                        log.info("micro_jump_veto", pair=pair, signal_dir=direction,
                                 micro_dir=_md, jump_score=float(_mj),
                                 note="fresh adverse order-book jump → skip entry")
                        return []
        except Exception as _mexc:
            log.debug("micro_veto_skipped", error=str(_mexc)[:120])

        regime_bonus = 15 if (
            (regime == "bull" and direction == "long") or
            (regime == "bear" and direction == "short")
        ) else 0
        tft_bonus = 0
        if tft_bias != 0:
            agrees = (direction == "long" and tft_bias > 0) or (direction == "short" and tft_bias < 0)
            tft_bonus = 10 if agrees else -5

        # Blueprint F20 PatchTST: long-horizon (16h) directional bias from LONGSEQ_FORECAST.
        # Complements TFT's short-horizon view per blueprint "Why both TFT and PatchTST".
        # F30 governance gate; falls through silently if F20 deactivated or no forecast.
        patchtst_bonus = 0
        try:
            from feature_governance.registry import is_active as _fg_active_f20
            if _fg_active_f20("F20"):
                ptst_raw = r.get(redis_keys.LONGSEQ_FORECAST.replace("{pair}", pair))
                if ptst_raw:
                    ptst = json.loads(ptst_raw)
                    change_pct = float(ptst.get("predicted_change_pct", 0))
                    if abs(change_pct) > 0.5:  # >0.5% predicted move = directional signal
                        agrees_ptst = (direction == "long" and change_pct > 0) or \
                                      (direction == "short" and change_pct < 0)
                        # Lower weight than TFT — long-horizon is noisier
                        patchtst_bonus = 5 if agrees_ptst else -3
        except Exception:
            pass

        # Blueprint F48 CandleNet + Production Extensions (cont. 46):
        #   1m + 5m + 15m next-candle multi-task prediction (Idea D: 4-TF
        #   hierarchy).  Bonus scoring:
        #     all 3 TFs agree → +25 (was +15 for 2 TFs)
        #     2 of 3 agree    → +10
        #     1 of 3 agree    → 0
        #     any disagree    → -15
        #   Trend ceiling (counter-trend protection):
        #     1m/5m counter-trend → cap at 55
        #     15m counter-trend   → cap at 50 (15m bias is more reliable)
        #   Exhaustion override (Idea A — bull-market shorts after a peak):
        #     exhaustion_score > 2.0 + counter-trend signal direction
        #       → loosen ceiling to 70, allowing the counter-trend short
        #     exhaustion_score > 2.0 + confirming direction → +20 bonus
        candlenet_bonus = 0
        candlenet_trend_ceil = None
        try:
            from feature_governance.registry import is_active as _fg_cn
            # cont. 69: full multi-TF set for the ENTRY decision (was 1m/5m/15m).
            # 30m+1h now feed direction + trend ceiling; 1h is the most reliable.
            # Redis-tunable via `entry:candle_tfs` (CSV).
            _fg_map = {"5m": "F48_5m", "15m": "F48_15m",
                       "30m": "F48_30m", "1h": "F48_1h"}
            try:
                _tf_csv = r.get("entry:candle_tfs")
                if _tf_csv:
                    _fg_map = {t.strip(): f"F48_{t.strip()}"
                               for t in str(_tf_csv).split(",") if t.strip()}
            except Exception:
                pass
            # cont. 69: ON-DEMAND fetch. The periodic publisher is CPU-bound and
            # can't refresh 200 pairs × 5 TFs within the 90s TTL (30m starved to
            # ~half coverage). Rather than SKIP a missing TF (fail-open, the old
            # behaviour), compute it NOW so every trade evaluates the full TF
            # picture before deciding. run_inference is fast (model cached) and
            # also write-backs the forecast to Redis. Toggle:
            # `entry:candle_ondemand_enabled=0`.
            _ondemand = (r.get("entry:candle_ondemand_enabled") or "1") == "1"

            def _get_fc(_tf: str):
                _raw = r.get(f"{pair}:{_tf}:candle_forecast")
                if not _raw and _ondemand:
                    try:
                        from ml.candlenet import run_inference as _rinf
                        _fc_now = _rinf(pair, _tf)
                        if _fc_now:
                            try:
                                r.incr("candle:ondemand_compute_count")
                            except Exception:
                                pass
                            return _fc_now
                    except Exception as _od_exc:
                        log.debug("candle_ondemand_failed", pair=pair,
                                  tf=_tf, error=str(_od_exc)[:120])
                    _raw = r.get(f"{pair}:{_tf}:candle_forecast")
                if _raw:
                    try:
                        return json.loads(_raw)
                    except Exception:
                        return None
                return None

            _forecasts = {}
            _exhaustions = {}
            for _tf, _fid in _fg_map.items():
                if not _fg_cn(_fid):
                    continue
                _fc_val = _get_fc(_tf)
                if _fc_val is not None:
                    _forecasts[_tf] = _fc_val
                _exh_raw = r.get(f"{pair}:{_tf}:exhaustion_score")
                if _exh_raw:
                    try:
                        _exhaustions[_tf] = json.loads(_exh_raw)
                    except Exception:
                        pass

            def _cn_agrees(fc: dict, dir_: str) -> bool | None:
                # cont. 69: was dir1 (1-candle, noisiest). dir3 (3-candle ahead)
                # matches the multi-candle hold horizon and is less noisy.
                d = fc.get("dir3", fc.get("dir1"))
                if d is None:
                    return None
                return (dir_ == "long" and d > 0.5) or (dir_ == "short" and d <= 0.5)

            agree_count    = 0
            disagree_count = 0
            for _fc in _forecasts.values():
                a = _cn_agrees(_fc, direction)
                if a is True:
                    agree_count += 1
                elif a is False:
                    disagree_count += 1

            total_active = len(_forecasts)
            # cont. 69: scale to N active TFs (now up to 4: 5m/15m/30m/1h).
            # FULL consensus (all agree) → +25; clean majority, no dissent → +10;
            # any TF disagrees → −15.
            if total_active >= 3 and agree_count == total_active:
                candlenet_bonus = 25
            elif total_active >= 2 and agree_count >= 2 and disagree_count == 0:
                candlenet_bonus = 10
            elif disagree_count > 0:
                candlenet_bonus = -15

            # Trend ceiling — weighted toward whichever TF flags counter-trend.
            # 15m trend has a tighter cap because longer-horizon trend bias is
            # statistically more reliable (less noise).
            _tr_5m  = _forecasts.get("5m",  {}).get("trend")
            _tr_15m = _forecasts.get("15m", {}).get("trend")
            _tr_30m = _forecasts.get("30m", {}).get("trend")
            _tr_1h  = _forecasts.get("1h",  {}).get("trend")

            def _counter(tr: float | None) -> bool:
                if tr is None:
                    return False
                return ((direction == "long"  and tr < 0.40) or
                        (direction == "short" and tr > 0.60))

            # cont. 69: longer TFs (15m/30m/1h) are the reliable counter-trend
            # filter → tightest cap (50). A 5m-only counter → looser cap (55).
            if _counter(_tr_15m) or _counter(_tr_30m) or _counter(_tr_1h):
                candlenet_trend_ceil = 50.0
            elif _counter(_tr_5m):
                candlenet_trend_ceil = 55.0

            # F48 §Idea A — Exhaustion override.
            # When at least one TF shows a strong exhaustion signal AND that
            # exhaustion points opposite to the existing trend (i.e. a peak in
            # a bull is detected, score points DOWN), counter-trend shorts are
            # the intended trade. Loosen the ceiling to 70 in that case and
            # add +20 bonus when the trade direction aligns with the exhaustion.
            _exh_max = 0.0
            _exh_dir = 0
            for _e in _exhaustions.values():
                _s = float(_e.get("score", 0.0))
                if _s > _exh_max:
                    _exh_max = _s
                    _exh_dir = int(_e.get("direction", 0))

            EXHAUSTION_GATE = 2.0
            if _exh_max > EXHAUSTION_GATE:
                # exh_dir is the direction of the STREAK — exhausted streak up
                # implies a coming DOWN move, so the favourable trade direction
                # is the OPPOSITE of the streak direction.
                expected_reversal = "short" if _exh_dir == 1 else "long"
                if direction == expected_reversal:
                    # Counter-trend trade is now the intended trade.
                    # Loosen ceiling and add +20 bonus.
                    if candlenet_trend_ceil is not None:
                        candlenet_trend_ceil = max(candlenet_trend_ceil, 70.0)
                    candlenet_bonus += 20

            # ── cont. 66 movement + direction-quality entry gates ──
            # Fix 1 — HARD minimum-predicted-move gate. Trade audit showed 36%
            # of trades moved <0.2% (dead; net-negative on fees/slippage). The
            # old code here only soft-nudged the score by 5 — it never blocked.
            # Reject entries the model forecasts to go nowhere. "Expected move"
            # = best near-term horizon (max |mag3|,|mag5|, both in %) across the
            # available TFs. Redis-tunable `entry:min_predicted_move_pct`
            # (default 0.35 ≈ the live mag3 median); set 0 to disable.
            # Fail-open when no forecasts exist (PCG already holds those pairs).
            if _forecasts:
                _exp_move = max(
                    (max(abs(float(fc.get("mag3", 0.0) or 0.0)),
                         abs(float(fc.get("mag5", 0.0) or 0.0)))
                     for fc in _forecasts.values()),
                    default=0.0,
                )
                try:
                    _min_move = float(r.get("entry:min_predicted_move_pct") or 0.35)
                except Exception:
                    _min_move = 0.35
                if _min_move > 0 and _exp_move < _min_move:
                    try:
                        r.incr("signal:reject:low_predicted_move")
                        r.incr(f"signal:reject:low_predicted_move:{pair}")
                    except Exception:
                        pass
                    log.info("entry_rejected_low_predicted_move", pair=pair,
                             expected_move_pct=round(_exp_move, 3),
                             min_required=_min_move, direction=direction)
                    return []

                # Fix 2 — short-side guard. Wrong-way shorts were the single
                # biggest loss bucket (-$11.5k/7d, 414 trades that rose ~2% while
                # short). Don't short into a model that still leans UP on any
                # available TF. Redis-tunable: `entry:short_guard_enabled`="0"
                # disables; `entry:short_htf_bull_block` is the dir3 ceiling.
                if direction == "short":
                    try:
                        _sg_on = (r.get("entry:short_guard_enabled") or "1") == "1"
                        _bull_block = float(r.get("entry:short_htf_bull_block") or 0.55)
                    except Exception:
                        _sg_on, _bull_block = True, 0.55
                    if _sg_on:
                        _bull_tf = None
                        for _tf in ("1h", "30m", "15m", "5m"):   # cont. 69: 1h/30m now populated
                            _fc = _forecasts.get(_tf)
                            if _fc and float(_fc.get("dir3", 0.5) or 0.5) > _bull_block:
                                _bull_tf = _tf
                                break
                        if _bull_tf is not None:
                            try:
                                r.incr("signal:reject:short_htf_bullish")
                                r.incr(f"signal:reject:short_htf_bullish:{pair}")
                            except Exception:
                                pass
                            log.info("short_rejected_htf_bullish", pair=pair,
                                     blocking_tf=_bull_tf,
                                     dir3=float(_forecasts[_bull_tf].get("dir3", 0.5)),
                                     block_above=_bull_block)
                            return []

            # Magnitude soft de-emphasis (retained): tiny 1-candle move → -5 bonus.
            _mags = [abs(float(fc.get("mag1", 0.0) or 0.0)) for fc in _forecasts.values()]
            if _mags and max(_mags) < 0.3 and candlenet_bonus > 0:
                candlenet_bonus = max(0, candlenet_bonus - 5)
        except Exception:
            pass

        # F50c (cont. 54) — Mamba SSM additive bonus, parallel to candlenet_bonus.
        # Smaller weight (-/+10 vs CandleNet's -/+25) — Mamba is a SECOND
        # opinion across longer context, not a primary direction driver.
        # When Mamba model files don't exist yet, load_forecast returns None
        # and this loop adds 0 — cold-start safe.
        mamba_bonus = 0
        try:
            from ml.mamba_forecaster import load_forecast as _mamba_fc
            _m_agree = 0
            _m_disagree = 0
            _m_active = 0
            for _tf in ("1m", "5m", "15m"):
                _mf = _mamba_fc(pair, _tf)
                if not _mf:
                    continue
                _m_active += 1
                _d1 = float(_mf.get("dir1", 0.5))
                if (direction == "long" and _d1 > 0.55) or \
                   (direction == "short" and _d1 < 0.45):
                    _m_agree += 1
                elif (direction == "long" and _d1 < 0.45) or \
                     (direction == "short" and _d1 > 0.55):
                    _m_disagree += 1
            if _m_active >= 2:
                if _m_agree >= 2 and _m_disagree == 0:
                    mamba_bonus = 10
                elif _m_disagree >= 2:
                    mamba_bonus = -8
        except Exception:
            pass

        # F50g (cont. 54) — Chronos foundation forecaster bonus (1h horizon).
        # Auto-deferred when F19 TFT is trusted (model:tft:trusted=="1") — see
        # ml/foundation_forecast.py. Bonus is small (-/+8) — uncertainty-aware
        # forecast that's only the primary signal during F19 cold-start.
        foundation_bonus = 0
        try:
            from ml.foundation_forecast import load_forecast as _fdn_fc
            _ff = _fdn_fc(pair)
            if _ff:
                _d1h = float(_ff.get("dir1h", 0.5))
                if (direction == "long" and _d1h > 0.55) or \
                   (direction == "short" and _d1h < 0.45):
                    foundation_bonus = 8
                elif (direction == "long" and _d1h < 0.45) or \
                     (direction == "short" and _d1h > 0.55):
                    foundation_bonus = -8
        except Exception:
            pass

        # F24M (cont. 54) — Multiscale GNN leader-follower confirmation.
        # If the pair's leader has just moved in our direction across the
        # leader's recent lead_candles window, +5 confirmation. If contagion
        # score is very high (>80) — pair is highly coupled to all others —
        # reduce confidence by 5 (less unique edge, more crowded trade).
        gnn_multiscale_bonus = 0
        try:
            from ml.gnn_multiscale import get_contagion_score, get_multiscale_leader
            _lf = get_multiscale_leader(pair)
            if _lf:
                _leader_pair = _lf.get("leader")
                _lc = int(_lf.get("lead_candles", 1))
                _tf_lf = _lf.get("tf", "1h")
                if _leader_pair:
                    _leader_raw = r.lrange(
                        f"{_leader_pair}:{_tf_lf}:candles", 0, _lc)
                    if _leader_raw and len(_leader_raw) > _lc:
                        try:
                            _last = json.loads(_leader_raw[0])
                            _prev = json.loads(_leader_raw[_lc])
                            _last_c = float(_last.get("c", 0))
                            _prev_c = float(_prev.get("c", 0))
                            if _last_c > 0 and _prev_c > 0:
                                _moved_up = _last_c > _prev_c
                                if (direction == "long" and _moved_up) or \
                                   (direction == "short" and not _moved_up):
                                    gnn_multiscale_bonus = 5
                        except Exception:
                            pass
            _contagion = get_contagion_score(pair)
            if _contagion > 80.0:
                gnn_multiscale_bonus -= 5
        except Exception:
            pass

        # F52 (cont. 55) — Exchange net-flow directional gate.
        # Closes the bot's largest blind spot: every prior forecaster reads
        # exchange-internal data only; F52 sees coins entering/leaving
        # exchanges. ±8 for native pairs (BTC/ETH/SOL with real chain data),
        # halved to ±4 for proxy alts (BTC-β derived). Cold-start safe.
        netflow_bonus = 0
        try:
            from data.onchain_netflow import get_bonus as _netflow_get_bonus
            netflow_bonus = _netflow_get_bonus(pair, direction)
        except Exception:
            pass

        # F53 (cont. 55) — Qlib Alpha-158 Top-K formulaic-alpha composite.
        # 158 closed-form factors over OHLCV; Top-K (default 20) selected by
        # rolling 7d cross-sectional IC ≥ 0.02. Bonus weighted by fraction
        # of top-K factors agreeing with the trade direction (each factor's
        # IC sign tells us which way it predicts). Cold-start: top-K empty
        # until ≥168h of history → bonus=0, no regression.
        qlib_bonus = 0
        try:
            from ml.qlib_alphas import get_bonus as _qlib_get_bonus
            qlib_bonus = _qlib_get_bonus(pair, direction)
        except Exception:
            pass

        # F54 (cont. 55) — LLM-DSL Promoted-Factors Composite.
        # arXiv:2604.26747-grade weekly LLM (qwen2.5-coder) factor miner;
        # survivors gated by Sharpe>1.0, |IC|>0.03, decay-ratio ∈ [0.5,2.0]
        # and a deepseek-r1 overfit-pass. Cold-start: no promoted factors
        # until first weekly mining completes → bonus=0, no regression.
        dsl_bonus = 0
        try:
            from ml.llm_alpha_dsl import get_bonus as _dsl_get_bonus
            dsl_bonus = _dsl_get_bonus(pair, direction)
        except Exception:
            pass

        # F58 (cont. 56) — Liquidation cascade-imminent bonus.
        # +18 (native) / +12 (proxy) when the dominant liquidation cluster
        # within 1.5% of mark-price is on the side that benefits `direction`.
        # Same-sign cap as F50g; opposite-direction penalty only at high prob.
        # Cold-start: no liq data → bonus=0.
        cascade_bonus = 0
        try:
            from data.liquidation_levels import get_bonus as _cascade_get_bonus
            cascade_bonus = _cascade_get_bonus(pair, direction)
        except Exception:
            pass

        # Blueprint Section 7 / Feature 11 — Trade Potential Score as a
        # multi-source confluence, NOT a single-factor read on sentiment.
        # Pre-cont.22 this line was: `abs(sentiment - 0.5) * 2 * 100` which gave
        # every pair an identical strength of 42.0 (since per-pair sentiment was
        # globally clobbered to 0.29 by the F&G proxy). Replaced with a weighted
        # composite over the 7 sources already gathered in this function so the
        # base strength actually varies per pair and per signal context.
        #
        # Each source maps to [0, 100]; weights sum to 1.0; missing-source
        # neutral default = 50 so a single dead feed doesn't zero the score.
        # Brain can later learn the weights (Genetic Algorithm; deferred).
        #
        # OFI directional alignment — primary microstructure signal.
        if (direction == "long" and ofi > 0) or (direction == "short" and ofi < 0):
            _ofi_align = 1.0
        elif ofi == 0:
            _ofi_align = 0.5
        else:
            _ofi_align = 0.0
        # Per-pair magnitude normalization (cont. 23). The legacy `abs(ofi)*10000`
        # constant scaled all pairs by the same factor, structurally favouring
        # high-OFI alts and zeroing out anchor pairs (BTC OFI ≈ 5e-7 → score 0).
        # Now: maintain an EWMA of |ofi| per pair in Redis and score relative to
        # that pair's own scale. A reading at 2× the pair's typical |ofi| maps
        # to 100; at the typical value maps to 50; below typical maps lower.
        _ofi_ewma_key = f"{pair}:ofi_abs_ewma"
        _ofi_abs = abs(ofi)
        try:
            _prev_ewma_raw = r.get(_ofi_ewma_key)
            _prev_ewma = float(_prev_ewma_raw) if _prev_ewma_raw is not None else _ofi_abs
            # alpha=0.1 → ~10-tick effective horizon. Slow enough that one spike
            # doesn't reset the baseline; fast enough to track regime shifts.
            _new_ewma = 0.1 * _ofi_abs + 0.9 * _prev_ewma if _prev_ewma > 0 else _ofi_abs
            r.setex(_ofi_ewma_key, 3600, _new_ewma)  # 1h TTL bridges short outages
        except Exception:
            _new_ewma = _ofi_abs
        if _new_ewma > 0:
            _ofi_mag_score = min(100.0, (_ofi_abs / _new_ewma) * 50.0)
        else:
            _ofi_mag_score = 50.0
        ofi_score = round(_ofi_mag_score * _ofi_align, 2)
        # Sentiment distance from neutral (low weight today — input is
        # global-shared per cont.22; weight rises automatically once web_intel
        # starts emitting per-pair tagged articles).
        sent_score = round(min(100, abs(sentiment - 0.5) * 200), 2)
        # Regime confluence with chosen direction.
        if regime == "bull" and direction == "long":
            regime_score = 100.0
        elif regime == "bear" and direction == "short":
            regime_score = 100.0
        elif regime in ("unknown", "turbulent"):
            regime_score = 50.0
        else:
            regime_score = 0.0
        # TFT (F19) short-horizon forecast agreement.
        if tft_bias != 0:
            _tft_mag = min(100.0, abs(tft_bias) * 5000)  # 2% predicted move → 100
            _tft_agrees = (direction == "long" and tft_bias > 0) or \
                          (direction == "short" and tft_bias < 0)
            tft_score = _tft_mag if _tft_agrees else max(0.0, 50.0 - _tft_mag / 2)
        else:
            tft_score = 50.0
        # PatchTST (F20) long-horizon agreement — reuse the bonus already
        # computed above which encodes direction-agreement (+5/-3) or absence (0).
        if patchtst_bonus > 0:
            patchtst_score = min(100.0, 50.0 + patchtst_bonus * 10)
        elif patchtst_bonus < 0:
            patchtst_score = max(0.0, 50.0 + patchtst_bonus * 10)
        else:
            patchtst_score = 50.0
        # CandleNet (F46) short-horizon score — 1min/5min agreement mapped to [0,100].
        if candlenet_bonus > 0:
            candlenet_score = min(100.0, 50.0 + candlenet_bonus * 3)
        elif candlenet_bonus < 0:
            candlenet_score = max(0.0, 50.0 + candlenet_bonus * 3)
        else:
            candlenet_score = 50.0
        # Bot's historical directional accuracy on this pair.
        hist_score = _safe_dir_accuracy(r, pair, default=50.0)
        # VPIN — informed flow magnitude. Same per-pair normalization as OFI
        # (cont. 23): track an EWMA per pair and score relative to that scale.
        # The old `vpin * 30000` constant similarly biased against pairs whose
        # VPIN naturally sits in a lower range.
        _vpin_ewma_key = f"{pair}:vpin_ewma"
        try:
            _v_prev_raw = r.get(_vpin_ewma_key)
            _v_prev = float(_v_prev_raw) if _v_prev_raw is not None else vpin
            _v_new = 0.1 * vpin + 0.9 * _v_prev if _v_prev > 0 else vpin
            r.setex(_vpin_ewma_key, 3600, _v_new)
        except Exception:
            _v_new = vpin
        if _v_new > 0:
            vpin_score = min(100.0, (vpin / _v_new) * 50.0)
        else:
            vpin_score = 50.0

        # cont. 27 — F12 decoder writeback. Pull accumulated scorer deltas
        # (each clamped ±0.15 by actuator) and re-clip each effective weight
        # to [0.05, 0.40]. Composite is divided by _total_w so the
        # normalization handles shifted-sum gracefully — adding +0.04 to
        # regime_weight just shifts the relative emphasis, not the score
        # magnitude.
        _w_ofi    = 0.25
        _w_regime = 0.20
        _w_tft    = 0.15
        try:
            from metacognition.actuator import get_scorer_overrides
            _so = get_scorer_overrides()
            _w_ofi    = max(0.05, min(0.40, _w_ofi    + float(_so.get("ofi_weight_delta", 0.0))))
            _w_regime = max(0.05, min(0.40, _w_regime + float(_so.get("regime_weight_delta", 0.0))))
            _w_tft    = max(0.05, min(0.40, _w_tft    + float(_so.get("tft_weight_delta", 0.0))))
        except Exception:
            pass
        _components = [
            ("ofi",        ofi_score,       _w_ofi),   # primary directional signal
            ("regime",     regime_score,    _w_regime), # confluence with macro regime
            ("tft",        tft_score,       _w_tft),   # short-horizon ML
            ("patchtst",   patchtst_score,  0.08),     # long-horizon ML (reduced to make room for candlenet)
            ("candlenet",  candlenet_score, 0.12),     # F48 1min/5min next-candle
            ("hist_acc",   hist_score,      0.13),     # bot's past on this pair
            ("vpin",       vpin_score,      0.10),     # informed flow strength
            ("sentiment",  sent_score,      0.05),     # narrative — low weight while global-shared
        ]
        _total_w = sum(w for _, _, w in _components)
        trade_potential = round(
            sum(s * w for _, s, w in _components) / _total_w, 2)

        # Blueprint F13 confidence — for the FINAL direction (flip happened
        # upstream right after OFI picked the initial side). Falls back to
        # the heuristic ofi_strength+regime+tft+patchtst sum when model
        # unavailable. F30 governance gate respected.
        learned_conf = None
        try:
            from feature_governance.registry import is_active as _fg_active
            paper_closed = int(r.get("brain:paper_closed") or 0)
            if paper_closed >= 100 and _fg_active("F13"):
                from ml.direction_model import predict_direction_confidence
                learned_conf = predict_direction_confidence(pair, direction)
        except Exception:
            pass

        if learned_conf is not None:
            direction_conf = learned_conf
        else:
            # cont. 54 — fold in P1 Mamba, P2 Chronos, P6 multiscale-GNN bonuses
            # cont. 55 — fold in F52 net-flow, F53 Qlib top-K, F54 LLM-DSL bonuses
            # cont. 56 — fold in F58 liquidation-cascade bonus
            direction_conf = round(min(100, max(0,
                ofi_strength + regime_bonus + tft_bonus + patchtst_bonus
                + candlenet_bonus + mamba_bonus + foundation_bonus
                + gnn_multiscale_bonus
                + netflow_bonus + qlib_bonus + dsl_bonus
                + cascade_bonus
            )), 2)

        # F51b (cont. 51): Funding-rate extremes gate. In crypto perps,
        # funding > +0.05% / 8h means longs are crowded and paying high cost —
        # the next funding settlement often triggers an unwind that pushes
        # price DOWN. Symmetric for shorts at funding < -0.05%. Apply a -20
        # penalty to direction_conf when direction aligns with the crowded
        # side. Soft penalty (not hard reject) so high-conviction signals
        # still pass. Source: Gate.io futures-signals research, Phemex.
        try:
            _funding_raw = r.get(redis_keys.FUNDING_RATE.replace("{pair}", pair))
            if _funding_raw is not None:
                _funding = float(_funding_raw)
                _crowded_long  = _funding >  0.0005   # > +0.05% per 8h
                _crowded_short = _funding < -0.0005   # < -0.05% per 8h
                if (direction == "long"  and _crowded_long) or \
                   (direction == "short" and _crowded_short):
                    _before = direction_conf
                    direction_conf = round(max(0.0, direction_conf - 20.0), 2)
                    log.info("funding_penalty_applied",
                             pair=pair, direction=direction,
                             funding_rate=_funding,
                             conf_before=_before, conf_after=direction_conf)
                    try:
                        r.incr("brain:funding_gate:penalty_count")
                    except Exception:
                        pass
        except (TypeError, ValueError, AttributeError) as exc:
            log.debug("funding_gate_skipped", pair=pair, error=str(exc)[:120])

        # F53 (cont. 68): Idiosyncratic-momentum gate. "Most altcoin momentum
        # is fake and driven purely by a BTC pump" (user's pasted framework).
        # A high-β alt that's only moving because BTC moved has no independent
        # edge — when BTC reverts, the alt round-trips and gives it all back
        # (a prime no-movement / flat-result trade). Decompose the pair's recent
        # return into BTC-explained (β·BTC_ret) + residual (idiosyncratic). If
        # the move is MOSTLY BTC-driven AND our direction merely rides BTC,
        # reject. Reuses data.onchain_netflow._btc_beta_proxy (cached β).
        # Redis: `entry:idiosyncratic_gate_enabled` (default "0" = OFF until
        # paper-validated), `entry:idiosyncratic_min_frac` (default 0.30 =
        # require ≥30% of the move to be the pair's own), and
        # `entry:idiosyncratic_window_min` (default 30 1m candles).
        try:
            if (r.get("entry:idiosyncratic_gate_enabled") or "0") == "1":
                _min_idio = float(r.get("entry:idiosyncratic_min_frac") or 0.30)
                _win = int(r.get("entry:idiosyncratic_window_min") or 30)

                def _window_ret(sym: str) -> float | None:
                    # 1m candle lists are newest-first (matches _btc_beta_proxy).
                    raw = r.lrange(f"{sym}:1m:candles", 0, _win)
                    if not raw or len(raw) < max(5, _win // 2):
                        return None
                    cs = [float(json.loads(c)["c"]) for c in raw]
                    new, old = cs[0], cs[-1]
                    return (new / old - 1.0) if old > 0 else None

                _alt_ret = _window_ret(pair)
                _btc_ret = _window_ret("BTCUSDT")
                if (_alt_ret is not None and _btc_ret is not None
                        and abs(_alt_ret) > 1e-9):
                    from data.onchain_netflow import _btc_beta_proxy
                    _beta = _btc_beta_proxy(pair)
                    _residual = _alt_ret - _beta * _btc_ret
                    _idio_frac = abs(_residual) / (abs(_alt_ret) + 1e-9)
                    _rides_btc = (
                        (direction == "long"  and _alt_ret > 0 and _btc_ret > 0) or
                        (direction == "short" and _alt_ret < 0 and _btc_ret < 0))
                    if _rides_btc and _idio_frac < _min_idio:
                        try:
                            r.incr("signal:reject:btc_beta_fake_momentum")
                        except Exception:
                            pass
                        log.info("idiosyncratic_gate_reject", pair=pair,
                                 direction=direction, alt_ret=round(_alt_ret, 5),
                                 btc_ret=round(_btc_ret, 5), beta=round(_beta, 3),
                                 idio_frac=round(_idio_frac, 3), min_frac=_min_idio)
                        return []
        except Exception as exc:
            log.debug("idiosyncratic_gate_skipped", pair=pair, error=str(exc)[:120])

        # F48 trend ceiling: counter-trend signals are hard-capped at 55/50
        # (or loosened to 70 by the Idea A exhaustion override) regardless of
        # how strong OFI or other signals are. Prevents the "high confidence
        # short in bull market" scenario that produced -$8K+ in losses.
        if candlenet_trend_ceil is not None:
            direction_conf = round(min(direction_conf, candlenet_trend_ceil), 2)

        # F48 §Idea C — RL Entry Timing Agent gate.
        # After the signal score crosses threshold but before returning the
        # signal, the PPO agent decides: enter now / wait one candle / skip.
        # When inactive (< 1000 paper trades) the agent returns "enter".
        entry_decision = "enter"
        try:
            from ml.entry_timing_agent import decide_entry
            entry_decision = decide_entry(pair, signal_score=trade_potential)
        except Exception:
            pass

        if entry_decision == "skip":
            # Record telemetry then suppress the signal entirely.
            try:
                r.incr("brain:entry_timing:skipped_count")
            except Exception:
                pass
            return []
        # "wait" → still return the signal but mark it pending so the executor
        # can defer to the next decide cycle. "enter" is the default path.

        # P4 (cont. 53) — Candle-close entry confirmation, pre-1000 only.
        # When the RL entry-timing agent is inactive (paper_closed < 1000),
        # require the most recent CLOSED 1m candle to confirm the signal
        # direction (close > open for long, close < open for short).
        # Bypass when 1m CandleNet dir1 is high-conviction (> 0.7 for long,
        # < 0.3 for short) — the model already has strong evidence.
        #
        # Deadlock detector (per silent-rejection / RL-deadlock rule): when
        # reject rate > 80% over the last 50+ calls, auto-disable by setting
        # Redis flag `signal:p4:disabled=1`. Manually re-enable by deleting
        # the flag. Counters: `signal:p4:total_calls`, `signal:p4:reject_count`.
        try:
            _p4_disabled = r.get("signal:p4:disabled") == "1"
            _paper_closed_p4 = int(r.get("brain:paper_closed") or 0)
            if not _p4_disabled and _paper_closed_p4 < 1000 \
                    and entry_decision == "enter":
                _candles_raw = r.lrange(f"{pair}:1m:candles", 0, 0)
                if _candles_raw:
                    _c = json.loads(_candles_raw[0])
                    _open_p = float(_c.get("o", 0))
                    _close_p = float(_c.get("c", 0))
                    # High-conviction bypass
                    _fc_1m_raw = r.get(f"{pair}:1m:candle_forecast")
                    _dir1_p4 = 0.5
                    if _fc_1m_raw:
                        try:
                            _dir1_p4 = float(json.loads(_fc_1m_raw).get("dir1", 0.5))
                        except Exception:
                            pass
                    _high_conv = (direction == "long" and _dir1_p4 > 0.7) or \
                                 (direction == "short" and _dir1_p4 < 0.3)
                    if not _high_conv and _open_p > 0 and _close_p > 0:
                        _bullish = _close_p > _open_p
                        _bearish = _close_p < _open_p
                        _reject_long = direction == "long" and not _bullish
                        _reject_short = direction == "short" and not _bearish
                        try:
                            r.incr("signal:p4:total_calls")
                        except Exception:
                            pass
                        if _reject_long or _reject_short:
                            try:
                                r.incr("signal:p4:reject_count")
                                r.incr("signal:reject:candle_close_confirm")
                            except Exception:
                                pass
                            # Deadlock detector tick.
                            try:
                                _tot = int(r.get("signal:p4:total_calls") or 0)
                                _rej = int(r.get("signal:p4:reject_count") or 0)
                                if _tot >= 50 and (_rej / _tot) > 0.80:
                                    r.set("signal:p4:disabled", "1")
                                    log.warning("p4_auto_disabled",
                                                reject_rate=round(_rej / _tot, 3),
                                                total=_tot)
                            except Exception:
                                pass
                            return []
                    else:
                        try:
                            r.incr("signal:p4:total_calls")
                        except Exception:
                            pass
        except Exception as exc:
            log.debug("p4_candle_close_confirm_skipped",
                      pair=pair, error=str(exc)[:120])

    # cont. 65 — feature_vector now snapshots the FULL 32-column predict-all
    # input (prediction.features.FEATURE_COLUMNS) so xgb_predictor retrains
    # see non-degenerate inputs. Previous 9-key snapshot left 23 columns
    # zero-filled at training time, which caused the trained model to output
    # constant predictions for every pair. Falls back to the legacy 9-key
    # shape if prediction.features fails (cold start, import error). Legacy
    # consumers (ml.direction_model) read individual keys, so we merge both
    # shapes into one dict.
    _legacy_fv = {
        "sentiment": sentiment, "ofi": ofi, "vpin": vpin, "mark": mark,
        "funding_rate": _safe_float(r.get(f"{pair}:funding_rate")),
        "change_24h":   _safe_float(r.get(f"{pair}:change_24h")),
        "volume_24h":   _safe_float(r.get(f"{pair}:volume_24h")),
        "amihud":       _safe_float(r.get(f"{pair}:amihud")),
        "dir_acc_pair": _safe_dir_accuracy(r, pair),
    }
    _full_fv = dict(_legacy_fv)
    try:
        from prediction.features import live_features as _live_features
        _full_fv.update(_live_features(
            pair,
            signal_strength=trade_potential,
            trade_potential=trade_potential,
        ))
    except Exception as _fv_exc:
        log.debug("feature_vector_snapshot_partial",
                  pair=pair, error=str(_fv_exc)[:120])

    # cont. 66d — Include cascade votes in feature_vector for dashboard visibility
    if "_cascade_audit" in locals() and isinstance(_cascade_audit, dict):
        _cascade_votes = _cascade_audit.get("votes", {})
        for tf, vote in _cascade_votes.items():
            _full_fv[f"cascade_{tf}"] = vote

    return [{
        "pair": pair,
        "direction": direction,
        "timeframe": "1h",
        "market_regime": regime,
        "signal_strength": trade_potential,
        "trade_potential": trade_potential,
        "direction_confidence": direction_conf,
        "brain_stage": brain_state.get("stage", 1),
        "mark_price": mark,
        "entry_decision": entry_decision,   # F48 §Idea C — "enter" | "wait"
        "feature_vector": json.dumps(_full_fv),
    }]


def _ewma_smooth(r, source_key: str, ewma_key: str,
                 alpha: float = 0.4) -> float | None:
    """Read `source_key` from Redis, EWMA-smooth it against the running
    average stored at `ewma_key`, persist the new average, return the
    smoothed value. Returns None if source_key is unset.

    alpha is the weight on the NEW observation (so alpha=0.4 means each
    fresh tick contributes 40% and the running history retains 60%).
    Three-tick effective horizon at alpha=0.4: a single spike decays
    to roughly 0.6³ ≈ 22% influence within 3 brain decide cycles.

    Added 2026-05-21 cont. 16 so L2/L9 inline gates don't cascade on a
    single spurious uncertainty/confidence spike — the smoother gives
    the brain one decide cycle to self-correct before the gate floor
    triggers a wave of hard rejections.
    """
    raw = r.get(source_key)
    if raw is None:
        return None
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return None
    prev_raw = r.get(ewma_key)
    if prev_raw is None:
        smoothed = x
    else:
        try:
            prev = float(prev_raw)
            smoothed = alpha * x + (1.0 - alpha) * prev
        except (TypeError, ValueError):
            smoothed = x
    try:
        r.setex(ewma_key, 600, smoothed)   # 10 min TTL — long enough to bridge restarts
    except Exception:
        pass
    return smoothed


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _safe_dir_accuracy(r, pair: str, default: float = 50.0) -> float:
    """Per-pair historical directional accuracy 0-100. 50 = no prior info."""
    try:
        raw = r.get(f"brain:directional_accuracy:{pair}")
        if not raw:
            return default
        import json as _j
        d = _j.loads(raw)
        rate = d.get("rate")
        if rate is None and d.get("total"):
            rate = 100.0 * d.get("correct", 0) / d["total"]
        return float(rate) if rate is not None else default
    except Exception:
        return default


# cont. 70 Track 1b — ARCHETYPE CAPITAL TILT (kept in engine because strategy/ is
# NOT a brain bind-mount → avoids an image rebuild). cont.69v (N=6642): the dominant
# edge axis is strategy ARCHETYPE — FADE/mean-reversion win, momentum/breakout/
# continuation + ML-confirmers lose. Tilt CAPITAL toward winners, away from losers.
# Gate-NOT-kill: losers keep trading at reduced size (UCB selector keeps learning;
# 17-day-bull window + small-N noise can self-correct). EXACT-name match — NOT
# substring: swing_sweep_fade & premium_index_z_fade are LOSERS despite "fade".
_ARCH_FADE_WINNERS = (
    "funding_extreme_fade,hurst_gated_revert,kalman_pair_residual_revert,"
    "exchange_netflow_inflow_fade,depth_weighted_ofi,hmm_regime_gate_overlay,"
    "mean_reversion_strict,stage1_ofi_momentum")
_ARCH_MOMENTUM_LOSERS = (
    "classical_ofi_cont,hawkes_lambda_spike_ride,oi_price_divergence,"
    "microprice_gradient,vol_target_sizing_overlay,dual_momentum,donchian_breakout,"
    "turtle_system,swing_sweep_fade,patchtst_direction_confirmer,"
    "premium_index_z_fade,momentum_continuation")


def _strategy_name_cached(strategy_id: str, r) -> str | None:
    """Resolve strategy_id -> name, Redis-cached 5 min (same TTL as the router)."""
    ck = f"strategy:router:name:{strategy_id}"
    try:
        c = r.get(ck)
        if c is not None:
            c = c.decode() if isinstance(c, bytes) else c
            return None if c == "__NULL__" else c
    except Exception:
        pass
    name = None
    try:
        from db import db_conn
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM strategies WHERE id = %s",
                            (strategy_id,))
                row = cur.fetchone()
                if row:
                    name = row[0]
    except Exception as exc:
        log.debug("archetype_name_lookup_failed",
                  strategy_id=strategy_id, error=str(exc)[:120])
        return None
    try:
        r.setex(ck, 300, name or "__NULL__")
    except Exception:
        pass
    return name


def _archetype_capital_mult(strategy_id: str, r) -> float:
    """cont. 70 Track 1b — capital multiplier for the routed strategy's archetype.
    1.0 when disabled / unknown / unclassified. Boost fade winners, cut momentum
    losers. Clipped to the router's [0.25, 2.0] capital band. Redis-tunable; default
    ON. Flip off: structural:archetype_tilt_enabled=0."""
    if not strategy_id:
        return 1.0
    try:
        if (r.get("structural:archetype_tilt_enabled") or b"1") in ("0", b"0"):
            return 1.0
        name = _strategy_name_cached(strategy_id, r)
        if not name:
            return 1.0

        def _set(key, default):
            raw = r.get(key)
            raw = (raw.decode() if isinstance(raw, bytes) else raw) or default
            return {x.strip() for x in raw.split(",") if x.strip()}

        if name in _set("structural:archetype_fade_patterns", _ARCH_FADE_WINNERS):
            m = float(r.get("structural:archetype_fade_mult") or 1.3)
            r.incr("structural:archetype_tilt:boost_count")
        elif name in _set("structural:archetype_momentum_patterns",
                          _ARCH_MOMENTUM_LOSERS):
            m = float(r.get("structural:archetype_momentum_mult") or 0.6)
            r.incr("structural:archetype_tilt:cut_count")
        else:
            return 1.0
        return max(0.25, min(2.0, m))
    except Exception as exc:
        log.debug("archetype_capital_mult_skipped", error=str(exc)[:120])
        return 1.0


def accept_or_reject(signal: dict, brain_state: dict) -> tuple[bool, str]:
    """T-02: Apply Brain-learned criteria to accept or reject a signal.

    Blueprint F25 GA: Stage 2+ thresholds (signal strength, turbulence cap) come
    from GA-evolved params in Redis when F25 is active. Falls back to hardcoded
    defaults below 50 trades or when GA hasn't run yet.
    """
    strength = float(signal.get("signal_strength") or 0)
    regime = signal.get("market_regime", "unknown")
    stage = brain_state.get("stage", 1)
    # cont. 61 — pair + direction extraction used by the audit-fix gates
    # below (liquidity / sentiment / BTC-lead). Without this, those gates
    # raise NameError and fall open silently, defeating their purpose.
    pair = signal.get("pair", "") or ""
    direction = signal.get("direction", "") or ""

    # Default thresholds (used at Stage 1 and as fallback). cont. 65k — the
    # EFFECTIVE stage≥2 gate is capped by risk:min_signal_strength via a final
    # ceiling clamp just before the strength check (after GA/Bayes), so the
    # data-accumulation target (18) holds regardless of those override layers.
    default_min_strength = 0.1 if stage <= 1 else 30
    default_turb_cap = 3.0
    min_strength = default_min_strength
    turb_cap = default_turb_cap

    if stage >= 2:
        try:
            from feature_governance.registry import is_active as _fg_active_f25
            if _fg_active_f25("F25"):
                from ml.genetic_algorithm import get_active_params
                ga = get_active_params()
                # Safety cap: GA fitness fn only penalizes "<5 hist. trades pass"
                # — it can (and did, cont. 18 audit) evolve min_signal_strength
                # higher than what live signal generation actually produces, which
                # silently kills every trade. Cap GA's effective output at 35 so
                # garbage evolution can't deadlock the bot. Floor at 15 too,
                # mirroring PARAM_BOUNDS lower edge.
                _ga_min = float(ga.get("min_signal_strength", default_min_strength))
                # cont. 24c: cap lowered 35 → 28 while bootstrapping the
                # direction-not-regime-locked flow. Against-regime trades lose
                # ~20 strength from the composite's regime_score=0 path, so a
                # 35 cap effectively still blocks all shorts in bull regime
                # (avg short strength = 19.8, avg long 33.6 — see signals
                # diagnostic 2026-05-22 04:55). 28 lets the strongest 1/3 of
                # against-regime signals through to gather direction-flip data.
                # Revert to 35 once both directions are represented in 200+
                # post-fix closed trades and GA can re-optimise on real data.
                min_strength = max(15.0, min(28.0, _ga_min))
                # cont. 27 — F9 decoder writeback. Layer the accumulated
                # min_signal_strength_delta on top of the GA value, then
                # re-clip to the absolute [15, 35] band. cont. 55: bucketed
                # by (regime × strength-band) — get_bucket_delta resolves to
                # the right bucket and falls back through regime-wide → global.
                # Reads zero / no-effect when overrides empty or F46 gated off.
                try:
                    from metacognition.actuator import get_bucket_delta
                    _delta = get_bucket_delta(
                        regime=regime,
                        signal_strength=strength,
                        delta_key="min_signal_strength_delta",
                    )
                    if _delta != 0.0:
                        min_strength = max(15.0, min(35.0, min_strength + _delta))
                except Exception:
                    pass
                turb_cap = float(ga.get("turbulence_cap", default_turb_cap))
        except Exception:
            pass

    # F8 Strategy Router: per-strategy entry overrides layered on TOP of the
    # GA-evolved defaults above (so per-strategy values win the tie). Added
    # 2026-05-21 cont. 9 alongside the typed entry_overrides JSONB column on
    # the strategies table.
    overrides = None
    routed_strategy_id = brain_state.get("active_strategy_id")
    if routed_strategy_id:
        try:
            from strategy.router import get_entry_overrides
            overrides = get_entry_overrides(routed_strategy_id)
        except Exception as exc:
            log.debug("entry_router_lookup_failed", error=str(exc)[:120])
    if overrides:
        if "min_signal_strength" in overrides:
            try:
                min_strength = float(overrides["min_signal_strength"])
            except (TypeError, ValueError):
                pass
        if "turbulence_cap" in overrides:
            try:
                turb_cap = float(overrides["turbulence_cap"])
            except (TypeError, ValueError):
                pass

    def _record(accepted: bool, reason: str) -> None:
        if overrides and routed_strategy_id:
            try:
                from strategy.router import record_routed_entry_decision
                record_routed_entry_decision(routed_strategy_id, accepted, reason)
            except Exception:
                pass

    # Regime whitelist gate — only applies when override is set.
    #
    # cont. 56 (2026-05-28) — DEADLOCK FIX: this gate previously hard-rejected
    # whenever the selector picked a strategy whose whitelist excluded the
    # live regime. In production on 2026-05-28 the selector cached an old
    # "bull"-only strategy in bear regime, producing 37,302 silent rejects
    # over ~7h with NO deadlock detector firing. Two changes here:
    #   1. When the rejection rate exceeds 80% over 50+ recent decisions
    #      AND the reject reason is `regime_not_in_strategy_whitelist`,
    #      DO NOT reject — log critical, bump a deadlock counter, force-
    #      clear `brain:active_strategy_id` so the next SOAR tick re-selects,
    #      and fall through to GA defaults. The trader keeps trading; the
    #      brain re-picks; safety net per memory rl_deadlock_detector.
    #   2. Even outside the deadlock case, we now emit a structured WARNING
    #      every 200 same-reason rejections so a slow drain is visible in
    #      logs — closes the feedback_silent_rejection failure mode.
    if overrides and "regime_whitelist" in overrides:
        wl = overrides["regime_whitelist"]
        if isinstance(wl, list) and wl and regime not in wl:
            # Deadlock-detector arming check.
            _force_pass = False
            try:
                _r_rej = redis_client.get()
                _tot_acc = int(_r_rej.get("strategy_router:entry_accepted_count") or 0)
                _tot_rej = int(_r_rej.get("strategy_router:entry_rejected_count") or 0)
                _total = _tot_acc + _tot_rej
                if _total >= 50:
                    _reject_frac = _tot_rej / _total
                    if _reject_frac > 0.80:
                        _force_pass = True
                        _r_rej.incr("strategy_router:regime_whitelist_deadlock_count")
                        _r_rej.set("strategy_router:regime_whitelist_deadlock_last_ts",
                                   int(time.time()))
                        # Force the brain to re-pick on the next SOAR tick.
                        _r_rej.delete("brain:active_strategy_id")
                        _r_rej.delete(f"selector:cache:{regime}")
                        log.error(
                            "strategy_router_regime_deadlock_break",
                            regime=regime,
                            whitelist=list(wl),
                            strategy_id=routed_strategy_id,
                            reject_frac=round(_reject_frac, 3),
                            accepted=_tot_acc, rejected=_tot_rej,
                            action="cleared_active_strategy_and_passed_through",
                        )
                # Slow-drain visibility WARN — fires every 200 rejects.
                if _tot_rej % 200 == 0 and _tot_rej > 0:
                    log.warning(
                        "strategy_router_regime_reject_slow_drain",
                        regime=regime, whitelist=list(wl),
                        strategy_id=routed_strategy_id,
                        total_rejected=_tot_rej,
                        last_reason="regime_not_in_strategy_whitelist",
                    )
            except Exception:
                pass
            if not _force_pass:
                _record(False, "regime_not_in_strategy_whitelist")
                return False, f"regime_{regime}_not_in_strategy_whitelist"
            # Falling through: ignore the strategy's whitelist for this signal,
            # use GA defaults for min_strength + turb_cap below.
            log.debug("strategy_router_regime_pass_through",
                      regime=regime, strategy_id=routed_strategy_id)

    # Phase 2 (cont. 63, 2026-05-29) — Bayesian Beta-distribution adaptive
    # threshold. Overrides the GA/override-resolved min_strength when the
    # Bayesian module has converged and produced a fresh recommendation
    # (refresh_ts < 2h). Cold-start: returns None → fall back to GA value.
    # Util-calibration (Phase 4) is already blended in inside the module's
    # refresh task, so consumers only need to call this single function.
    try:
        from signals.bayes_threshold import get_adaptive_min_strength as _bayes_t
        _adaptive = _bayes_t()
        if _adaptive is not None:
            _prev_min = min_strength
            min_strength = float(_adaptive)
            try:
                redis_client.get().incr(redis_keys.BAYES_APPLIED_COUNT)
            except Exception:
                pass
            if abs(_prev_min - min_strength) > 0.5:
                log.debug("bayes_threshold_applied",
                          pair=pair, direction=direction,
                          ga_min=_prev_min, bayes_min=min_strength,
                          strength=strength)
    except Exception as _bayes_exc:
        log.debug("bayes_threshold_skipped", error=str(_bayes_exc)[:120])

    # Predict-all Phase C (cont. 63, 2026-05-29) — SOFT prediction gate.
    # Default OFF (`prediction:gate_enabled` != "1") until a trained model
    # exists AND user explicitly flips the gate on. When enabled:
    #   * read predictions:{pair} Redis hash (written by Phase B refresh loop)
    #   * if missing → reject with `prediction_not_ready` (recoverable for
    #     the replay pool)
    #   * if conformal_confidence < gate_conf (default 0.60) → reject
    #     `prediction_low_confidence`
    #   * if predicted_rr_p50 < gate_rr (default 1.5) → reject
    #     `prediction_low_rr`
    #   * if predicted_direction disagrees with the engine's direction →
    #     reject `prediction_direction_mismatch`
    # Cold-start safety: when gate disabled OR no prediction available,
    # behaviour is identical to pre-Phase-C.
    try:
        import json as _json_pred
        _r_pred = redis_client.get()
        _gate_on = (_r_pred.get("prediction:gate_enabled") in ("1", b"1"))
        if _gate_on:
            _gate_conf = float(_r_pred.get(
                "prediction:gate_conformal_conf") or 0.60)
            _gate_rr = float(_r_pred.get(
                "prediction:gate_rr_p50") or 1.50)
            _gate_conf = max(0.0, min(1.0, _gate_conf))
            _gate_rr = max(0.5, min(10.0, _gate_rr))
            _raw_pred = _r_pred.get(f"predictions:{pair}")
            if _raw_pred is None:
                _record(False, "prediction_not_ready")
                try:
                    _r_pred.incr("prediction:gate:reject:not_ready")
                except Exception:
                    pass
                return False, "prediction_not_ready"
            try:
                _pred = (_json_pred.loads(_raw_pred)
                         if isinstance(_raw_pred, (str, bytes))
                         else dict(_raw_pred))
            except Exception:
                _pred = None
            if not isinstance(_pred, dict):
                _record(False, "prediction_not_ready")
                return False, "prediction_not_ready"
            _conf = float(_pred.get("conformal_confidence") or 0.0)
            _rr = float(_pred.get("predicted_rr_p50") or 0.0)
            _pdir = str(_pred.get("predicted_direction") or "").lower()
            if _conf < _gate_conf:
                _record(False, "prediction_low_confidence")
                try:
                    _r_pred.incr("prediction:gate:reject:low_confidence")
                except Exception:
                    pass
                return False, f"prediction_low_confidence_{_conf:.2f}"
            if _rr < _gate_rr:
                _record(False, "prediction_low_rr")
                try:
                    _r_pred.incr("prediction:gate:reject:low_rr")
                except Exception:
                    pass
                return False, f"prediction_low_rr_{_rr:.2f}"
            if _pdir and _pdir not in (direction, ""):
                _record(False, "prediction_direction_mismatch")
                try:
                    _r_pred.incr("prediction:gate:reject:direction_mismatch")
                except Exception:
                    pass
                return False, f"prediction_dir_mismatch_{_pdir}_vs_{direction}"
            try:
                _r_pred.incr("prediction:gate:accept_count")
            except Exception:
                pass
    except Exception as _pgate_exc:
        log.debug("prediction_gate_skipped", error=str(_pgate_exc)[:200])

    # cont. 65k — final CEILING on the effective gate. GA + Bayes + util-calib
    # layers above can push min_strength back up to ~25-28, re-blocking the
    # weak-but-real signals we want to accumulate. This caps the EFFECTIVE gate
    # at risk:min_signal_strength (default 18) so the data-accumulation target
    # holds regardless of those layers. Remove/raise the key to restore them.
    try:
        import redis_client as _rc_ms
        _ms_ceiling = _rc_ms.get().get("risk:min_signal_strength")
        if _ms_ceiling is not None and stage > 1:
            min_strength = min(min_strength, float(_ms_ceiling))
    except Exception:
        pass

    # cont. 70 — STRUCTURAL-EDGE GATE (Track 1). cont.69v (N=6642) showed the
    # real edge is STRUCTURAL (regime + UTC session), NOT the per-trade model
    # score. Two sub-gates, both Redis-toggleable (default ON) and gate-NOT-kill
    # (regime gate auto-passes on deadlock; session gate is a soft strength
    # penalty, never a hard reject) so the 17-day mostly-bull sample can't
    # deadlock the bot. Flip off live: set structural:regime_gate_enabled /
    # structural:session_gate_enabled to "0".
    try:
        _rs = redis_client.get()
        _seen = _rs.incr("structural:gate:total_seen")
        # (a) Regime gate — cont.69v: 'unknown' regime averaged -0.97%cap (only
        #     losing regime). Reject unknown-regime entries. DEADLOCK-SAFE: if
        #     >80% of the last-50+ signals were unknown (classifier likely stuck),
        #     force-pass so the bot never starves on a broken regime label.
        if _rs.get("structural:regime_gate_enabled") not in ("0", b"0") \
                and regime == "unknown":
            _rej = int(_rs.get("structural:regime_gate:reject_count") or 0)
            if _seen >= 50 and _rej / _seen > 0.80:
                _rs.incr("structural:regime_gate:deadlock_pass_count")
                if _rej % 200 == 0:
                    log.warning("structural_regime_gate_deadlock_pass",
                                reject_frac=round(_rej / _seen, 3),
                                seen=_seen, rejected=_rej,
                                note="classifier likely stuck on 'unknown' — passing")
            else:
                _rs.incr("structural:regime_gate:reject_count")
                _rs.incr("signal:reject:regime_unknown")
                _record(False, "regime_unknown")
                return False, "regime_unknown"
        # (b) Session gate — cont.69v: UTC hours 04/06/09 were net-negative.
        #     SOFT penalty: raise the effective strength bar in losing hours
        #     (never a hard reject). Bad-hours set + penalty are Redis-tunable.
        if _rs.get("structural:session_gate_enabled") not in ("0", b"0"):
            _hour = datetime.now(timezone.utc).hour
            _bad_raw = _rs.get("structural:session_bad_hours")
            _bad_raw = (_bad_raw.decode() if isinstance(_bad_raw, bytes)
                        else _bad_raw) or "4,6,9"
            _bad_set = {int(x) for x in _bad_raw.split(",")
                        if x.strip().isdigit()}
            if _hour in _bad_set:
                _pen = float(_rs.get("structural:session_penalty") or 6.0)
                min_strength = min_strength + _pen
                _rs.incr("structural:session_gate:penalty_count")
    except Exception as _struct_exc:
        log.debug("structural_edge_gate_skipped", error=str(_struct_exc)[:160])

    if strength < min_strength:
        _record(False, "signal_too_weak")
        return False, "signal_too_weak"
    if regime == "turbulent":
        r = redis_client.get()
        turbulence = float(r.get(redis_keys.TURBULENCE_INDEX) or 0)
        if turbulence > turb_cap:
            _record(False, "turbulence_too_high")
            return False, "turbulence_too_high"

    # cont. 61 audit fix — BTC lead-lag entry gate. The user's existing
    # ml/transfer_entropy.py emits all-zero values (math bug at lines
    # 22-32 of that file — histogram MI is broken). Until the TE
    # implementation is fixed, gate alt entries on BTC's recent
    # direction over a configurable window. Theory: in trending crypto
    # markets, BTC leads alts by 5-30 minutes. Entering an alt LONG
    # when BTC dumped in the last 30 min is a high-risk timing error.
    #
    # Logic:
    #   - Skip if pair IS BTC (no self-reference)
    #   - Read BTCUSDT:1m:candles, get N-bar return
    #   - If return < -gate_pct AND direction=long → block
    #   - If return > +gate_pct AND direction=short → block
    # Configurable via `risk:btc_lead_disabled=1` to disable,
    # `risk:btc_lead_window_bars` (default 15), `risk:btc_lead_gate_pct`
    # (default 0.5% = 50 bps move).
    try:
        r_btc = redis_client.get()
        # cont. 69s — STRENGTH-BYPASS + REGIME-SCALED BTC-lead gate. The prior
        # flat 0.5% threshold was strength- AND regime-blind: it killed str-89
        # bull-regime longs on a -0.5% BTC wiggle (live audit 2026-06-02:
        # btc_dump_*_blocks_long rejected 43 signals in 6h, max_str 89.4 — the
        # dashboard "overly conservative in bull regime" pattern). Now:
        #   (a) high-conviction signals (strength >= bypass) skip the gate — a
        #       89-strength long shouldn't die on BTC noise;
        #   (b) in the ALIGNED trend regime the block threshold widens by
        #       `bull_mult` so only a genuine BTC move (not noise) blocks.
        # Redis-tunable + counters (silent-rejection rule):
        #   risk:btc_lead_strength_bypass (default 60), risk:btc_lead_bull_mult (2.0)
        if r_btc.get("risk:btc_lead_disabled") != "1" and pair != "BTCUSDT":
            _btc_window = int(r_btc.get("risk:btc_lead_window_bars") or 15)
            _btc_gate_pct = float(r_btc.get("risk:btc_lead_gate_pct") or 0.5)
            try:
                _btc_bypass_str = float(r_btc.get("risk:btc_lead_strength_bypass") or 60.0)
            except (TypeError, ValueError):
                _btc_bypass_str = 60.0
            try:
                _btc_bull_mult = float(r_btc.get("risk:btc_lead_bull_mult") or 2.0)
            except (TypeError, ValueError):
                _btc_bull_mult = 2.0
            _reg = (regime or "").lower()
            if strength >= _btc_bypass_str:
                # High conviction → bypass the BTC-lead timing gate entirely.
                try:
                    r_btc.incr("signal:btc_lead_strength_bypass_count")
                    r_btc.incr("te:consumer_passed_count")
                except Exception:
                    pass
            else:
                _btc_candles_raw = r_btc.lrange("BTCUSDT:1m:candles", 0, _btc_window)
                if _btc_candles_raw and len(_btc_candles_raw) >= _btc_window:
                    _btc_first = json.loads(_btc_candles_raw[-1])
                    _btc_last = json.loads(_btc_candles_raw[0])
                    _btc_open = float(_btc_first.get("c") or _btc_first.get("o") or 0)
                    _btc_close = float(_btc_last.get("c") or 0)
                    if _btc_open > 0:
                        _btc_ret_pct = 100.0 * (_btc_close - _btc_open) / _btc_open
                        # Widen the block threshold when the trade aligns WITH the
                        # trend regime (long in bull / short in bear): transient BTC
                        # counter-moves are noise inside a confirmed trend.
                        _long_gate = _btc_gate_pct * (_btc_bull_mult if "bull" in _reg else 1.0)
                        _short_gate = _btc_gate_pct * (_btc_bull_mult if "bear" in _reg else 1.0)
                        if direction == "long" and _btc_ret_pct < -_long_gate:
                            try:
                                r_btc.incr("signal:reject:btc_dump_blocks_long")
                                r_btc.incr("te:consumer_fire_count")
                            except Exception:
                                pass
                            _record(False, "btc_recent_dump_blocks_long")
                            return False, f"btc_dump_{_btc_ret_pct:.2f}pct_blocks_long"
                        if direction == "short" and _btc_ret_pct > _short_gate:
                            try:
                                r_btc.incr("signal:reject:btc_pump_blocks_short")
                                r_btc.incr("te:consumer_fire_count")
                            except Exception:
                                pass
                            _record(False, "btc_recent_pump_blocks_short")
                            return False, f"btc_pump_{_btc_ret_pct:.2f}pct_blocks_short"
                        try:
                            r_btc.incr("te:consumer_passed_count")
                        except Exception:
                            pass
    except Exception as exc:
        log.debug("btc_lead_gate_skipped", error=str(exc)[:120])

    # cont. 63 (2026-05-29) — REGIME-AWARE sentiment gate. Replaces the
    # cont. 61 symmetric ±0.30 threshold which produced 2,165 / 2,911
    # rejects in 2h (74% of all rejections) because mildly-bullish
    # sentiment (0.43-0.45) was killing every SHORT signal even in a
    # bear regime. New behaviour:
    #
    #   * bear regime  — shorts are with-regime, allow aggressively
    #                    (block only when sentiment > +0.60 i.e. strongly
    #                    bullish, very-strong opposing evidence).
    #                    Longs are contra-regime, require only mild
    #                    bearish confirmation to block (sentiment < -0.10).
    #   * bull regime  — mirror: longs aggressive (block only at < -0.60);
    #                    shorts blocked at > +0.10.
    #   * turbulent    — disable gate (regime itself is the uncertainty
    #                    signal; don't add a second noisy filter).
    #   * unknown      — keep symmetric ±0.30 (original cont. 61 default).
    #
    # All thresholds Redis-overridable per regime; cleanly falls open on
    # missing keys. Recoverable rejection — Phase 1 replay pool re-queues
    # so a sentiment flip within 15 min re-evaluates the signal.
    try:
        r_sent = redis_client.get()
        if r_sent.get("risk:sentiment_gate_disabled") != "1":
            _sent_raw = r_sent.get(f"{pair}:sentiment")
            if _sent_raw is not None and regime != "turbulent":
                _sent = float(_sent_raw)
                # Defaults: (block_short_above, block_long_below).
                # Encoded as 4 floats per regime so each side tunes independently.
                _regime_defaults = {
                    "bear":    (0.60, -0.10),   # short-friendly
                    "bull":    (0.10, -0.60),   # long-friendly
                    "unknown": (0.30, -0.30),   # original symmetric
                }
                _bs_def, _bl_def = _regime_defaults.get(
                    regime, _regime_defaults["unknown"])
                try:
                    _block_short_above = float(r_sent.get(
                        f"risk:sentiment_block_short_above:{regime}") or _bs_def)
                    _block_long_below = float(r_sent.get(
                        f"risk:sentiment_block_long_below:{regime}") or _bl_def)
                except (TypeError, ValueError):
                    _block_short_above, _block_long_below = _bs_def, _bl_def
                if direction == "long" and _sent < _block_long_below:
                    try:
                        r_sent.incr("signal:reject:sentiment_bearish_long")
                        r_sent.incr(f"signal:reject:sentiment_long:{regime}")
                        r_sent.set("sentiment:consume_count:cryptobert",
                                   int(r_sent.get("sentiment:consume_count:cryptobert") or 0) + 1)
                    except Exception:
                        pass
                    _record(False, "sentiment_bearish_blocks_long")
                    return False, f"sentiment_{_sent:.2f}_blocks_long"
                if direction == "short" and _sent > _block_short_above:
                    try:
                        r_sent.incr("signal:reject:sentiment_bullish_short")
                        r_sent.incr(f"signal:reject:sentiment_short:{regime}")
                        r_sent.set("sentiment:consume_count:cryptobert",
                                   int(r_sent.get("sentiment:consume_count:cryptobert") or 0) + 1)
                    except Exception:
                        pass
                    _record(False, "sentiment_bullish_blocks_short")
                    return False, f"sentiment_{_sent:.2f}_blocks_short"
                # Fired but not blocking — still count as consumed
                try:
                    r_sent.incr("sentiment:consume_count:passed")
                    r_sent.incr(f"sentiment:consume_count:passed:{regime}")
                except Exception:
                    pass
    except Exception as exc:
        log.debug("sentiment_gate_skipped", error=str(exc)[:120])

    # cont. 61 audit fix — Per-entry liquidity gate. The scanner already
    # filters at scan time ($50M default) but pairs can still flash-dump
    # within the 8h scan window. Audit 2026-05-29 found:
    #   GUAUSDT: 7 trades × avg -$161, 21min each (lost $1,128)
    #   INUSDT:  5 trades × avg -$170, 16min each (lost $854)
    #   SIRENUSDT: 1 trade × -$176, 0min (rugged)
    # The gate is a tiered filter:
    #   tier 1 (vol >= $50M): no restriction
    #   tier 2 ($10M-$50M): widen min_pct (will be picked up by initial_sl
    #                       compute) + flag for capital halving
    #   tier 3 (< $10M):     reject outright
    # Borderline tier 2 communicates via redis key trade-side, consumed
    # by the position-sizer below.
    try:
        r_liq = redis_client.get()
        _scan_tier = r_liq.hget(f"scanner:meta:{pair}", "quote_volume_24h")
        if _scan_tier is not None:
            _qv_24h = float(_scan_tier)
            _min_liq_floor = float(r_liq.get("entry:liquidity_floor_usd") or 10_000_000)
            _borderline_max = float(r_liq.get("entry:liquidity_borderline_usd") or 50_000_000)
            if _qv_24h < _min_liq_floor:
                try:
                    r_liq.incr("signal:reject:liquidity_floor")
                    r_liq.set("signal:reject:liquidity_last_pair", pair)
                    r_liq.set("signal:reject:liquidity_last_qv24h", _qv_24h)
                except Exception:
                    pass
                _record(False, "liquidity_below_floor")
                return False, f"liquidity_below_floor_{int(_qv_24h)}"
            elif _qv_24h < _borderline_max:
                # Flag for capital halving by consumer
                try:
                    r_liq.setex(f"signal:liquidity_borderline:{pair}", 60, "1")
                    r_liq.incr("signal:liquidity_borderline_count")
                except Exception:
                    pass
    except Exception as exc:
        log.debug("liquidity_gate_skipped", error=str(exc)[:120])

    # F56 (cont. 56) — Conformal-uncertainty soft abstain. When every
    # warmed-up forecaster's calibrated 90% interval straddles the neutral
    # line for this signal's direction confidence, we genuinely don't have
    # the statistical edge to take the trade. Cold-start safe — when no
    # model has enough calibration data, `should_abstain` returns False.
    # Wrapped in RL-deadlock-detector pattern (memory: rl_deadlock_detector)
    # via the F56 module's own kill switch + bounded false positives.
    try:
        from ml.conformal_wrapper import should_abstain as _cf_abstain
        if _cf_abstain(direction_conf=signal.get("direction_conf")):
            _record(False, "conformal_uncertain")
            return False, "conformal_uncertain"
    except Exception:
        pass

    _record(True, "")
    return True, ""


async def process_signals(pairs: list[str], brain_state: dict, engine) -> list[str]:
    """T-01 to T-04: Generate, filter, log all signals, start counterfactual tracking.

    cont. 63 (2026-05-29) — Phase 1 + 3: Replay-pool consumer prepends recently-
    rejected pairs that still pass freshness/drift/regime checks. Phase 3 Thompson
    sampling re-ranks the combined pair set when bandit:enabled is "1". Both are
    no-ops when their kill switches flip off.
    """
    import redis_client
    r = redis_client.get()
    opened_trade_ids = []
    max_open = int(r.get("bot:max_open_trades") or 999)

    # cont. 70 — Launch-Pad P5 funnel. When launchpad:enabled=1 the buffer is the
    # SOLE source of opens (owner D1): qualified-green slots only, in movement
    # order, up to max_open. If nothing qualifies the engine WAITS (no forcing).
    # Kill switch off → _lp_mode False → the legacy 160-pair flow below is untouched.
    _lp_mode = False
    _lp_dir: dict[str, str] = {}
    _lp_slot: dict[str, int] = {}
    _lp_gate = None
    try:
        from signals.launch_pad import gate as _lp_gate
        _lp = _lp_gate.funnel_pairs(r)
        if _lp is not None:
            _lp_mode = True
            _lp_pairs, _lp_dir, _lp_slot = _lp
            if not _lp_pairs:
                try:
                    r.incr("launchpad:open:reject:no_qualified")
                except Exception:
                    pass
                log.info("launchpad_funnel_wait", reason="no_qualified_green")
                return opened_trade_ids
            pairs = _lp_pairs
            log.info("launchpad_funnel_active", n_pairs=len(pairs))
    except Exception as _lp_exc:
        log.warning("launchpad_funnel_skipped", error=str(_lp_exc)[:200])
        _lp_mode = False

    # Phase 1 consumer (cont. 63) — pull non-stale entries from the replay
    # pool and prepend their pairs so the engine re-evaluates them BEFORE
    # fresh-signal pairs this cycle. This is the hysteresis: a borderline
    # rejection gets a second chance within 15 min as long as price/regime
    # haven't drifted. The actual entry decision still flows through the
    # same accept_or_reject pipeline — replay only changes ordering.
    replay_pairs_consumed: set[str] = set()
    try:
        from signals.replay_pool import (
            fetch_fresh_entries as _replay_fetch,
            remove_entry as _replay_remove,
        )
        # Launch-pad funnel (cont. 70) is itself the ranker; skip the replay-pool
        # hysteresis so its entries aren't consumed while the funnel is live.
        _replay_entries = [] if _lp_mode else _replay_fetch(limit=25)
        if _replay_entries:
            replay_first: list[str] = []
            for _entry in _replay_entries:
                _p = _entry.get("pair")
                if _p and _p not in replay_pairs_consumed:
                    replay_first.append(_p)
                    replay_pairs_consumed.add(_p)
                # Remove from pool — the engine will either re-accept (which
                # opens a trade and records consume_count) or re-reject (which
                # pushes back into the pool with a fresh timestamp).
                _raw = _entry.get("_raw")
                if _raw:
                    _replay_remove(_raw)
            if replay_first:
                # Prepend, dedup; keep original pairs order for the tail.
                pairs = replay_first + [p for p in pairs
                                        if p not in replay_pairs_consumed]
                log.info("replay_pool_prepended",
                         n_replay=len(replay_first),
                         total_pairs=len(pairs))
    except Exception as _replay_exc:
        log.warning("replay_pool_consumer_skipped",
                    error=str(_replay_exc)[:200])

    # Phase 3 (cont. 63) — Multi-play Thompson sampling re-rank. Only kicks
    # in when bandit:enabled = "1" AND there are more pairs than open slots
    # remaining. Cold-buckets fall back to strength-based scoring inside
    # slot_selector.rank, so this is safe to call always.
    try:
        from signals.slot_selector import rank as _bandit_rank
        from memory.query import get_open_trades as _bandit_get_open
        _open_n_for_rank = len(_bandit_get_open())
        _slots_left = max(0, max_open - _open_n_for_rank)
        if not _lp_mode and _slots_left and len(pairs) > _slots_left:
            _current_regime = r.get(redis_keys.CURRENT_REGIME) or "unknown"
            _ranked = _bandit_rank([
                {"pair": p, "regime": _current_regime,
                 "signal_strength": 50.0}
                for p in pairs
            ])
            pairs = [c["pair"] for c in _ranked]
    except Exception as _bandit_exc:
        log.debug("bandit_rank_skipped", error=str(_bandit_exc)[:200])

    # cont. 65k-6 — PER-PAIR DEDUP. The bot had no guard against opening multiple
    # positions on the same pair → 79 trades on 45 pairs (XLM ×4 etc.), stacking
    # correlated exposure. Build the open-count-per-pair map once; skip a pair that
    # already has `risk:max_open_per_pair` (default 1) open positions.
    from memory.query import get_open_trades
    try:
        _max_per_pair = int(r.get("risk:max_open_per_pair") or 1)
    except (TypeError, ValueError):
        _max_per_pair = 1
    _open_per_pair = {}
    for _ot in get_open_trades():
        _op = _ot.get("pair")
        _open_per_pair[_op] = _open_per_pair.get(_op, 0) + 1

    for pair in pairs:
        # Re-check open trade count each pair so we never exceed max_open
        if len(get_open_trades()) >= max_open:
            break
        # Per-pair dedup: don't stack positions on the same pair.
        if _open_per_pair.get(pair, 0) >= _max_per_pair:
            try:
                r.incr("signal:reject:dup_pair")
            except Exception:
                pass
            continue
        candidates = generate_candidate_signals(pair, brain_state)
        for signal in candidates:
            # cont. 70 — Launch-Pad owns direction (D1/D2): in funnel mode a
            # signal may only fire in the slot's staged direction; otherwise skip.
            if _lp_mode and not _lp_gate.direction_ok(
                    _lp_dir, pair, signal.get("direction")):
                try:
                    r.incr("launchpad:open:reject:dir_mismatch")
                except Exception:
                    pass
                continue
            # cont. 65k — DIRECTION-BALANCE GUARD. Guarantees the accumulated
            # trade book stays roughly balanced long/short so Layer-2 training
            # data is never mono-directional (the owner's concern). When open
            # trades on one side exceed the other by more than `balance_margin`
            # (default 0.20 → 60/40), block NEW entries on the over-represented
            # side until the minority side catches up. Redis-tunable:
            # risk:dir_balance_enabled ("1" default), risk:dir_balance_margin.
            # Only engages once there are enough open trades to judge (≥5).
            try:
                if (r.get("risk:dir_balance_enabled") or "1") == "1":
                    # cont. 65k — balance on RECENTLY-OPENED trades (rolling
                    # counter), NOT current open positions: stale old trades
                    # (e.g. 6 shorts awaiting time-exit) would otherwise skew the
                    # book 100% and hard-block one side forever → zero opens.
                    # Only engage once enough recent volume exists (≥ _MIN_N) so
                    # cold-start trades flow freely first. Counters decay via a
                    # rolling key the open-path increments.
                    _MIN_N = int(r.get("risk:dir_balance_min_n") or 12)
                    try:
                        _rl = int(r.get("trades:recent:long") or 0)
                        _rs = int(r.get("trades:recent:short") or 0)
                    except (TypeError, ValueError):
                        _rl = _rs = 0
                    _rn = _rl + _rs
                    if _rn >= _MIN_N:
                        _margin = float(r.get("risk:dir_balance_margin") or 0.25)
                        _sdir = signal.get("direction")
                        _over_short = (_rs - _rl) / _rn > _margin
                        _over_long = (_rl - _rs) / _rn > _margin
                        if (_sdir == "short" and _over_short) or \
                           (_sdir == "long" and _over_long):
                            try:
                                r.incr("signal:reject:dir_balance")
                                r.incr(f"signal:reject:dir_balance:{_sdir}")
                            except Exception:
                                pass
                            log.info("dir_balance_block", pair=pair,
                                     signal_dir=_sdir, recent_long=_rl,
                                     recent_short=_rs,
                                     note="recent opens skewed — hold this side")
                            continue
            except Exception as _dbexc:
                log.debug("dir_balance_skipped", error=str(_dbexc)[:120])

            # Blueprint F45 Cross-Sectional Momentum (Liu-Tsyvinski 2022) consumer.
            # Reads the producer's rank/return/max-1h-return Redis keys (written by
            # signals/xsmom.py every 5 min via Celery beat) and modulates signal
            # strength by cross-sectional rank. Lottery-effect filter penalizes
            # pairs that spiked >30% inside the 7d window.
            #
            # Sparse-data fallback: if any of rank / max are missing (pair too
            # new, producer hasn't run, candles unavailable), this block silently
            # no-ops. Never gates signal flow on F45 alone.
            try:
                from feature_governance.registry import is_active as _fg_active_f45
                if _fg_active_f45("F45"):
                    rank_raw = r.get(redis_keys.XSMOM_RANK.replace("{pair}", pair))
                    if rank_raw is not None:
                        rank = float(rank_raw)
                        max_raw = r.get(redis_keys.XSMOM_MAX_1H_7D.replace("{pair}", pair))
                        max_1h_7d = float(max_raw) if max_raw is not None else 0.0
                        _direction = signal.get("direction")
                        _old_str = float(signal.get("signal_strength") or 0)
                        _mult = 1.0
                        if _direction == "long":
                            if   rank >= 0.8: _mult = 1.10
                            elif rank <= 0.2: _mult = 0.85
                        elif _direction == "short":
                            if   rank <= 0.2: _mult = 1.10
                            elif rank >= 0.8: _mult = 0.85
                        # Lottery filter: spike survivors mean-revert.
                        if max_1h_7d > 0.30:
                            _mult *= 0.85
                        if _mult != 1.0:
                            signal["signal_strength"] = round(
                                min(100.0, _old_str * _mult), 2)
                            signal["trade_potential"] = signal["signal_strength"]
                            log.info("xsmom_modulated",
                                     pair=pair, direction=_direction,
                                     rank=round(rank, 3),
                                     max_1h_7d=round(max_1h_7d, 4),
                                     mult=round(_mult, 3),
                                     old_strength=_old_str,
                                     new_strength=signal["signal_strength"])
                            try:
                                r.incr("xsmom:consume_count")
                                r.set("xsmom:consume_last_ts", int(time.time()))
                            except Exception:
                                pass
            except Exception as exc:
                log.debug("xsmom_modulator_skipped", pair=pair, error=str(exc)[:120])

            # Blueprint F35 MemRL: per-candidate base-rate check on similar past trades.
            # Uses get_base_rate_sample (Phase 1 only) — Phase 2 re-ranks by PnL DESC which
            # biases toward wins and would hide loss patterns. Phase 1 gives the true win rate
            # of semantically-similar setups, which is what we need to skip bad-setup signals.
            # Rejected signals still go through write_signal + counterfactual tracking so we
            # can validate (via shadow_win_rate) whether MemRL was right to reject.
            memrl_override = None
            try:
                from feature_governance.registry import is_active as _fg_active
                paper_closed = int(r.get("brain:paper_closed") or 0)
                if paper_closed >= 10 and _fg_active("F35"):
                    from memory.cognitive.memrl import get_base_rate_sample
                    fv_raw = signal.get("feature_vector")
                    fv = fv_raw if isinstance(fv_raw, dict) else json.loads(fv_raw or "{}")
                    memrl_context = {
                        "pair": pair,
                        "direction": signal.get("direction"),
                        "market_regime": signal.get("market_regime"),
                        "feature_vector": fv,
                    }
                    # cont. 24: tightened window 6h → 1h and threshold 0.30 → 0.20.
                    # Both the SL ratchet (cont. 23) and the regime-not-dictator
                    # direction fix (cont. 24) deployed in the same session; all
                    # trades older than ~1h are from the broken-trail / long-only
                    # era and produce systematically wrong base-rate estimates.
                    # 1h window means MemRL bootstraps cleanly: empty samples for
                    # the first hour (falls through to skip), then re-engages on
                    # post-fix data only. Threshold 0.20 lets borderline setups
                    # through while still blocking 1-in-7 catastrophes.
                    #
                    # cont. 51 (2026-05-26): widened back from 1h → 4h. The 1h
                    # window was too tight after cont. 50 stabilised the trail/
                    # direction logic — a single bad minute (e.g. a coordinated
                    # 14-stopout in 38 seconds at 06:50 UTC) could anchor MemRL
                    # for the next hour and block ALL signals (the cont. 51
                    # symptom). 4h sample includes enough diversity to recover
                    # from a single market-flip cascade while still excluding
                    # the pre-cont.24 broken-trail era.
                    memories = get_base_rate_sample(memrl_context, top_k=30,
                                                    recent_hours=4)
                    # cont. 65k-3 — MemRL kill switch for the accumulation phase.
                    # The base-rate win_rate is computed from HISTORICAL trades,
                    # which are poisoned by the mono-short losing history (~17% wr)
                    # → MemRL rejects EVERY new long AND short. Same poisoned-data
                    # pattern as F13. Disable (risk:memrl_disabled=1) until clean
                    # post-fix outcomes accumulate, then re-enable. Threshold also
                    # Redis-tunable (risk:memrl_threshold, default 0.20).
                    _memrl_off = False
                    try:
                        _memrl_off = (r.get("risk:memrl_disabled") == "1")
                    except Exception:
                        pass
                    if memories and len(memories) >= 10 and not _memrl_off:
                        wins = sum(1 for m in memories if (m.get("net_pnl_usdt") or 0) > 0)
                        wr = wins / len(memories)
                        # cont. 27 — F9 writeback layered onto MemRL threshold.
                        # Base 0.20 (Redis-tunable), delta capped by actuator,
                        # absolute floor 0.05 / ceiling 0.40.
                        try:
                            _memrl_thresh = float(r.get("risk:memrl_threshold") or 0.20)
                        except (TypeError, ValueError):
                            _memrl_thresh = 0.20
                        try:
                            from metacognition.actuator import get_filter_overrides
                            _fo = get_filter_overrides()
                            _memrl_thresh = max(0.05, min(0.40,
                                _memrl_thresh + float(_fo.get("memrl_threshold_delta", 0.0))))
                        except Exception:
                            pass
                        if wr < _memrl_thresh:
                            memrl_override = ("reject", f"memrl_low_wr_{wr:.2f}_{len(memories)}n")
                            log.info("memrl_rejected", pair=pair, direction=signal.get("direction"),
                                     win_rate=round(wr, 2), n=len(memories))
                        elif wr > 0.60:
                            signal["signal_strength"] = round(
                                min(100.0, float(signal.get("signal_strength") or 0) * 1.2), 2)
                            signal["trade_potential"] = signal["signal_strength"]
                            log.info("memrl_boosted", pair=pair, direction=signal.get("direction"),
                                     win_rate=round(wr, 2), n=len(memories),
                                     new_strength=signal["signal_strength"])
            except Exception as exc:
                log.warning("memrl_check_skipped", pair=pair, error=str(exc)[:100])

            # F35 Slow Memory cluster consumer (added 2026-05-21 cont. 14).
            # Closes the cont. 6 producer/consumer gap — memory_clusters rows
            # are PRODUCED by sleep consolidation but until now nothing read
            # them. This block fetches the nearest matching cluster's
            # aggregated win_rate/n_trades and applies signal modulation when
            # the cluster has enough trades (>= 30) for the signal to be load-
            # bearing. NEVER hard-rejects on cluster context alone — clusters
            # are aggregated wisdom, not per-signal verdict.
            cluster_override = None
            try:
                if paper_closed >= 30 and _fg_active("F35"):
                    from memory.cognitive.memrl import get_cluster_context
                    cluster = get_cluster_context({
                        "pair": pair,
                        "direction": signal.get("direction"),
                        "market_regime": signal.get("market_regime"),
                    })
                    if cluster and cluster["n_trades"] >= 30:
                        cwr = cluster["win_rate"]
                        cn  = cluster["n_trades"]
                        if cwr < 20.0:
                            # Cluster consistently loses — strong scale-down.
                            old_str = float(signal.get("signal_strength") or 0)
                            signal["signal_strength"] = round(old_str * 0.7, 2)
                            signal["trade_potential"] = signal["signal_strength"]
                            log.info("cluster_scaled_down", pair=pair,
                                     direction=signal.get("direction"),
                                     cluster_wr=cwr, cluster_n=cn,
                                     match_kind=cluster["match_kind"],
                                     old_strength=old_str,
                                     new_strength=signal["signal_strength"])
                            try:
                                r.incr("memrl:cluster_scaled_down_count")
                            except Exception:
                                pass
                        elif cwr > 80.0:
                            # Cluster consistently wins — boost.
                            old_str = float(signal.get("signal_strength") or 0)
                            signal["signal_strength"] = round(
                                min(100.0, old_str * 1.15), 2)
                            signal["trade_potential"] = signal["signal_strength"]
                            log.info("cluster_boosted", pair=pair,
                                     direction=signal.get("direction"),
                                     cluster_wr=cwr, cluster_n=cn,
                                     match_kind=cluster["match_kind"],
                                     old_strength=old_str,
                                     new_strength=signal["signal_strength"])
                            try:
                                r.incr("memrl:cluster_boosted_count")
                            except Exception:
                                pass
            except Exception as exc:
                log.debug("cluster_context_skipped", pair=pair, error=str(exc)[:100])

            # Blueprint F24 GNN: leader-follower confirmation boost. If this pair has a
            # detected leader (per ml/gnn.get_interasset_signals) AND that leader moved
            # in the signal's direction over the past N hours, boost signal_strength.
            # This is the blueprint's "BTC leads alts by ~15 min" effect.
            try:
                from feature_governance.registry import is_active as _fg_active_f24
                if _fg_active_f24("F24"):
                    ias_raw = r.get(redis_keys.INTERASSET_SIGNALS)
                    if ias_raw:
                        ias = json.loads(ias_raw)
                        lf = (ias.get("leader_followers") or {}).get(pair)
                        if lf:
                            leader = lf["leader"]
                            lag = int(lf.get("lead_candles", 1))
                            leader_candles = r.lrange(
                                redis_keys.CANDLES.replace("{pair}", leader).replace("{interval}", "1h"),
                                0, max(lag, 1),
                            )
                            if leader_candles and len(leader_candles) >= 2:
                                # CANDLES is newest-first; head is current, tail of slice is `lag` hours ago.
                                cur_c = float(json.loads(leader_candles[0])["c"])
                                past_c = float(json.loads(leader_candles[-1])["c"])
                                if past_c > 0:
                                    lead_move = (cur_c - past_c) / past_c
                                    direction_long = signal.get("direction") == "long"
                                    same_dir = (direction_long and lead_move > 0.005) or \
                                               (not direction_long and lead_move < -0.005)
                                    if same_dir:
                                        old_strength = float(signal.get("signal_strength") or 0)
                                        signal["signal_strength"] = round(min(100.0, old_strength * 1.15), 2)
                                        signal["trade_potential"] = signal["signal_strength"]
                                        log.info("gnn_leader_boost",
                                                 pair=pair, leader=leader,
                                                 lead_move_pct=round(lead_move * 100, 2),
                                                 old_strength=old_strength,
                                                 new_strength=signal["signal_strength"])
            except Exception as exc:
                log.warning("gnn_leader_boost_skipped", pair=pair, error=str(exc)[:100])

            # Blueprint F24M (Evolving Multiscale GNN) — sympathy-pump boost.
            # The legacy F24 block above uses 1h-only lead-lag. F24M detects the
            # leader on whichever of 15m/1h gives the strongest cross-TF lead-lag,
            # so it catches faster sector rotations (the framework's "lead coin
            # gets a buy wall → its lagging sector peers pump within ~15 min"
            # effect at sub-hourly resolution). Runs in addition to F24; the
            # ×1.12 multiplier is deliberately below F24's ×1.15 so that when
            # both fire the combined boost stays bounded (and signal_strength is
            # capped at 100 regardless). Advisory only — never a veto.
            try:
                from feature_governance.registry import is_active as _fg_active_f24m
                if _fg_active_f24m("F24M"):
                    from ml.gnn_multiscale import get_multiscale_leader
                    _ms_lf = get_multiscale_leader(pair)
                    if _ms_lf:
                        _ms_leader = _ms_lf["leader"]
                        _ms_tf = _ms_lf.get("tf", "1h")
                        _ms_lag = max(1, int(_ms_lf.get("lead_candles", 1)))
                        _ms_candles = r.lrange(
                            redis_keys.CANDLES.replace("{pair}", _ms_leader)
                                              .replace("{interval}", _ms_tf),
                            0, _ms_lag,
                        )
                        if _ms_candles and len(_ms_candles) >= 2:
                            # CANDLES newest-first: head=current, slice tail=`lag` candles ago.
                            _ms_cur = float(json.loads(_ms_candles[0])["c"])
                            _ms_past = float(json.loads(_ms_candles[-1])["c"])
                            if _ms_past > 0:
                                _ms_move = (_ms_cur - _ms_past) / _ms_past
                                _ms_is_long = signal.get("direction") == "long"
                                _ms_same = ((_ms_is_long and _ms_move > 0.005) or
                                            (not _ms_is_long and _ms_move < -0.005))
                                if _ms_same:
                                    _ms_old = float(signal.get("signal_strength") or 0)
                                    signal["signal_strength"] = round(min(100.0, _ms_old * 1.12), 2)
                                    signal["trade_potential"] = signal["signal_strength"]
                                    try:
                                        r.incr("multiscale_gnn:sympathy_boost_count")
                                    except Exception:
                                        pass
                                    log.info("gnn_multiscale_sympathy_boost",
                                             pair=pair, leader=_ms_leader, tf=_ms_tf,
                                             lead_candles=_ms_lag,
                                             lead_move_pct=round(_ms_move * 100, 2),
                                             old_strength=_ms_old,
                                             new_strength=signal["signal_strength"])
            except Exception as exc:
                log.warning("gnn_multiscale_sympathy_skipped", pair=pair, error=str(exc)[:100])

            # Blueprint F34 World Model — Model-Predictive Planning (MPP).
            # For each candidate action (open_long, open_short, hold), sample N
            # stochastic rollouts through the RSSM and score by mean cumulative
            # reward. Pick argmax. Used as ADVISORY ONLY (signal-strength modulation),
            # NEVER a hard veto — a buggy world model must not be able to silence
            # the entire bot. Gated on F34 active + brain_stage >= 2 + bundle loaded
            # (plan_best_action returns None on fallback so this is safe).
            try:
                from feature_governance.registry import is_active as _fg_active_f34
                if brain_state.get("stage", 1) >= 2 and _fg_active_f34("F34"):
                    from world_model.model import plan_best_action
                    fv_raw = signal.get("feature_vector")
                    fv = fv_raw if isinstance(fv_raw, dict) else json.loads(fv_raw or "{}")
                    mpp_obs = dict(fv) if isinstance(fv, dict) else {}
                    mpp_obs.update({
                        "ofi": float(r.get(redis_keys.OFI.replace("{pair}", pair)) or 0),
                        "vpin": float(r.get(redis_keys.VPIN.replace("{pair}", pair)) or 0),
                        "turbulence": float(r.get(redis_keys.TURBULENCE_INDEX) or 0),
                    })
                    mpp = plan_best_action(mpp_obs, n_rollouts=32, horizon=8)
                    if mpp:
                        best = mpp.get("best_action")
                        sig_dir = signal.get("direction")
                        old_strength = float(signal.get("signal_strength") or 0)
                        if (best == "open_long" and sig_dir == "long") or \
                           (best == "open_short" and sig_dir == "short"):
                            signal["signal_strength"] = round(min(100.0, old_strength * 1.05), 2)
                            tag = "confirm"
                        elif (best == "open_long" and sig_dir == "short") or \
                             (best == "open_short" and sig_dir == "long"):
                            signal["signal_strength"] = round(old_strength * 0.85, 2)
                            tag = "disagree"
                        elif best == "hold":
                            signal["signal_strength"] = round(old_strength * 0.90, 2)
                            tag = "hold_suggested"
                        else:
                            tag = "neutral"
                        signal["trade_potential"] = signal["signal_strength"]
                        signal["mpp_best_action"] = best
                        signal["mpp_tag"] = tag
                        log.info("mpp_planned", pair=pair, direction=sig_dir,
                                 best_action=best, tag=tag,
                                 mean_reward=mpp.get("mean_reward"),
                                 uncertainty=mpp.get("uncertainty"),
                                 old_strength=old_strength,
                                 new_strength=signal["signal_strength"])
            except Exception as exc:
                log.warning("mpp_skipped", pair=pair, error=str(exc)[:120])

            # Blueprint F35 MemRL — direct Q-value consumer. The Phase 2 re-rank
            # in retrieve_relevant_memories produces a Q-ordered list of memories,
            # but the most direct consumer is here: look up Q(current_bucket,
            # current_direction) and modulate signal_strength accordingly.
            # Without this consumer, the Q-table is producer-only (Rule 4 asymmetry).
            # Gated on F35 + brain_stage >= 2 + bucket trustworthy (n_samples >= 20).
            try:
                from feature_governance.registry import is_active as _fg_active_qf35
                if brain_state.get("stage", 1) >= 2 and _fg_active_qf35("F35"):
                    from memory.cognitive.q_learning import (
                        state_bucket as _q_bucket, get_q as _q_get,
                        is_q_trustworthy as _q_trust,
                    )
                    _q_b = _q_bucket(
                        regime=signal.get("market_regime"),
                        pair=pair,
                        strength=signal.get("signal_strength"),
                    )
                    _q_action = signal.get("direction")
                    _q_val, _q_n = _q_get(_q_b, _q_action)
                    if _q_trust(_q_n):
                        _old_str = float(signal.get("signal_strength") or 0)
                        # Mild modulation — never zeroes the signal, never over-amplifies.
                        # Q is in [-1, +1] from reward_from_pnl; scale to [0.85, 1.15].
                        _q_mult = 1.0 + 0.15 * max(-1.0, min(1.0, _q_val))
                        signal["signal_strength"] = round(
                            max(0.0, min(100.0, _old_str * _q_mult)), 2)
                        signal["trade_potential"] = signal["signal_strength"]
                        signal["q_value"] = round(_q_val, 4)
                        signal["q_n_samples"] = _q_n
                        try:
                            import time as _ts
                            r.incr("memrl:q_consume_count")
                            r.set("memrl:q_consume_last_ts", str(int(_ts.time())))
                        except Exception:
                            pass
                        log.info("q_modulated", pair=pair, direction=_q_action,
                                 bucket=_q_b, q_value=round(_q_val, 4),
                                 n_samples=_q_n, multiplier=round(_q_mult, 3),
                                 old_strength=_old_str,
                                 new_strength=signal["signal_strength"])
            except Exception as exc:
                log.warning("q_modulate_skipped", pair=pair, error=str(exc)[:120])

            # Inline DECIDE-phase gating per blueprint Section 9.1 — L2 World
            # Model uncertainty + L9 Metacognitive confidence. Both layers
            # already publish to brain:* keys from brain/soar.py:_decide; this
            # block converts those advisory values into trading-path gates.
            # Mirror MemRL pattern: soft-scale signal_strength when in caution
            # band, hard-reject at the catastrophic floor.
            #
            # Post 2026-05-21 cont. 16: reads go through _ewma_smooth so a
            # single spurious spike (e.g. world_model_uncertainty jumps to
            # 0.95 for one decide tick) doesn't cascade into a wave of hard
            # rejections before the brain self-corrects on its next tick.
            l2_override = None
            l9_override = None
            try:
                _u = _ewma_smooth(r, "brain:world_model_uncertainty",
                                  "signals:ewma:wm_uncertainty", alpha=0.4)
                if _u is not None:
                    if _u > 0.80:
                        l2_override = ("reject",
                                       f"world_model_uncertain_{_u:.2f}")
                    elif _u > 0.50:
                        # 0.5→1.0 scale, 1.0 at u=0.5 down to 0.7 at u=0.8
                        _scale = max(0.5, 1.0 - (_u - 0.5))
                        _old = float(signal.get("signal_strength") or 0)
                        signal["signal_strength"] = round(_old * _scale, 2)
                        signal["trade_potential"] = signal["signal_strength"]
                        log.info("l2_uncertainty_scaled", pair=pair,
                                 uncertainty=_u, scale=round(_scale, 3),
                                 old_strength=_old,
                                 new_strength=signal["signal_strength"])
                        try:
                            r.incr("inline_decide:l2_scale_count")
                            r.set("inline_decide:l2_last_ts", int(time.time()))
                        except Exception:
                            pass
            except Exception as exc:
                log.debug("l2_gate_skipped", error=str(exc)[:120])

            try:
                _c = _ewma_smooth(r, "brain:metacog_confidence",
                                  "signals:ewma:metacog_confidence", alpha=0.4)
                if _c is not None:
                    if _c < 20.0:
                        l9_override = ("reject",
                                       f"metacog_low_confidence_{_c:.1f}")
                    elif _c < 40.0:
                        # Recalibrated cont. 22b: divisor 50→40. Original threshold
                        # was tuned during the broken-SL era when metacog_confidence
                        # was depressed by every-trade-loses outcomes; with the base
                        # composite (cont. 22) producing varied strengths in 36-40
                        # range, 50.0 divisor was slashing every signal by ~19%
                        # post-MPP, pushing finals to 21-25 (below the 35 GA cap).
                        # 20→40 scale linearly, 0.5 at c=20 up to 1.0 at c=40.
                        # Confidence ≥ 40 no longer penalises.
                        _scale = max(0.5, _c / 40.0)
                        _old = float(signal.get("signal_strength") or 0)
                        signal["signal_strength"] = round(_old * _scale, 2)
                        signal["trade_potential"] = signal["signal_strength"]
                        log.info("l9_confidence_scaled", pair=pair,
                                 confidence=_c, scale=round(_scale, 3),
                                 old_strength=_old,
                                 new_strength=signal["signal_strength"])
                        try:
                            r.incr("inline_decide:l9_scale_count")
                            r.set("inline_decide:l9_last_ts", int(time.time()))
                        except Exception:
                            pass
            except Exception as exc:
                log.debug("l9_gate_skipped", error=str(exc)[:120])

            # Reject precedence (cont. 55): R4 bot-confidence floor → R2 pair
            # suspension → L9 (catastrophic confidence) → L2 (high uncertainty)
            # → MemRL (low historical win-rate) → standard accept_or_reject. The
            # FIRST reject wins; subsequent gates aren't evaluated.
            _r4_r2_block = None
            try:
                from metacognition.confidence import should_hard_skip
                if should_hard_skip():
                    _r4_r2_block = "bot_confidence_below_floor"
                    try:
                        r.incr("bot:confidence:hard_skip_count")
                    except Exception:
                        pass
            except Exception:
                pass
            if not _r4_r2_block:
                try:
                    import json as _json_r2
                    _susp = r.get(redis_keys.BRAIN_PAIR_SUSPENSION)
                    _susp = _json_r2.loads(_susp) if _susp else {}
                    _sd = _susp.get(signal.get("pair")) if isinstance(_susp, dict) else None
                    if isinstance(_sd, dict) and _sd.get("blocked"):
                        _r4_r2_block = "pair_suspended_blocked"
                        try:
                            r.incr(f"pair:suspension:blocked_count:{signal.get('pair')}")
                        except Exception:
                            pass
                except Exception:
                    pass

            if _r4_r2_block:
                accepted, rejection_reason = False, _r4_r2_block
            elif l9_override and l9_override[0] == "reject":
                accepted, rejection_reason = False, l9_override[1]
                try:
                    r.incr("inline_decide:l9_reject_count")
                except Exception:
                    pass
            elif l2_override and l2_override[0] == "reject":
                accepted, rejection_reason = False, l2_override[1]
                try:
                    r.incr("inline_decide:l2_reject_count")
                except Exception:
                    pass
            elif memrl_override and memrl_override[0] == "reject":
                accepted, rejection_reason = False, memrl_override[1]
            else:
                accepted, rejection_reason = accept_or_reject(signal, brain_state)

            # Idea 2 Postmortem RAG (cont. 64) — if accept_or_reject rejected
            # the signal as "signal_too_weak" but vector-search over past
            # F9 postmortems shows similar rejections were wrong > 70% of
            # the time, override to accept. Bounded — only the strength
            # gate is bypassable. Other reject reasons (turbulent, MemRL,
            # suspension, etc.) pass through unchanged.
            if (not accepted
                    and rejection_reason == "signal_too_weak"):
                try:
                    from metacognition.postmortem_rag import (
                        prior_belief as _rag_belief,
                        should_apply_bonus as _rag_apply,
                    )
                    _rag_b, _rag_ev = _rag_belief(
                        pair=signal.get("pair"),
                        direction=signal.get("direction"),
                        timeframe=signal.get("timeframe"),
                        regime=signal.get("market_regime"),
                        signal_strength=signal.get("signal_strength"),
                        rejection_reason=rejection_reason,
                        brain_stage=signal.get("brain_stage"),
                    )
                    if _rag_apply(_rag_b, _rag_ev):
                        accepted = True
                        rejection_reason = None
                        log.info("postmortem_rag_override",
                                 pair=signal.get("pair"),
                                 belief=round(_rag_b, 3),
                                 n=_rag_ev.get("n"),
                                 avg_sim=_rag_ev.get("avg_sim"))
                        try:
                            r.incr("postmortem_rag:override_applied_count")
                        except Exception:
                            pass
                    else:
                        try:
                            r.incr("postmortem_rag:override_skipped_count")
                        except Exception:
                            pass
                except Exception as _rag_exc:
                    log.debug("postmortem_rag_skipped",
                              err=str(_rag_exc)[:160])

            # Blueprint F37 Multi-Agent Debate Council — activates at Stage 3 / 300 trades.
            # F37 verdict scales position size or vetoes the trade. Below
            # threshold or when F37 deactivated → no-op (full allocation).
            #
            # cont. 69 (Option 1 redesign): the SYNCHRONOUS verdict is now the
            # INSTANT deterministic scorer (debate.fallback), NOT the LLM debate.
            # phi3 on this CPU-only box runs ~4 tok/s → a synchronous 3-agent ×
            # 3-round LLM debate either times out ("degraded" → trades blocked,
            # the cont.-69 incident) or burns cloud quota. The LLM debate now runs
            # ASYNC (enqueued after open_trade) purely for verbal-reinforcement
            # weight learning + the learned-prior cache the scorer consults. The
            # LLM can therefore never again block a real-time entry.
            debate_log = None
            debate_size_mult = 1.0
            if accepted:
                try:
                    from feature_governance.registry import is_active as _fg_active_f37
                    _paper_closed = int(r.get("brain:paper_closed") or 0)
                    _stage = brain_state.get("stage", 1)
                    if _stage >= 3 and _paper_closed >= 300 and _fg_active_f37("F37"):
                        from debate.fallback import deterministic_verdict
                        _balance = float(r.get(redis_keys.VIRTUAL_BALANCE)
                                         or r.get(redis_keys.ACCOUNT_BALANCE) or 0)
                        _capital_pct = (brain_state.get("default_capital_usdt", 100)
                                        / _balance * 100) if _balance > 0 else 5.0
                        market_ctx = {
                            "regime": signal.get("market_regime")
                                      or r.get(redis_keys.CURRENT_REGIME) or "unknown",
                            "turbulence": float(r.get(redis_keys.TURBULENCE_INDEX) or 0),
                        }
                        debate_log = deterministic_verdict(
                            signal, market_ctx, _capital_pct, _balance)
                        verdict = debate_log.get("verdict")
                        if verdict in ("skip", "skip_risk"):
                            accepted = False
                            rejection_reason = f"debate_{verdict}"
                        elif verdict == "reduced_allocation":
                            debate_size_mult = 0.7
                        elif verdict == "exploratory":
                            debate_size_mult = 0.05
                        # full_allocation → 1.0 (default)
                        log.info("debate_applied", pair=pair, verdict=verdict,
                                 size_mult=debate_size_mult, source="deterministic",
                                 score=debate_log.get("score"))
                        # cont. 69 TODO: enqueue async batched LLM debate here
                        # (after open_trade, with signal_id+trade_id) for verbal-
                        # reinforcement weight learning. See next_impl checklist.
                except Exception as exc:
                    log.warning("debate_skipped", pair=pair, error=str(exc)[:200])

            # Blueprint F21 MARL Minute Agent: final execution-timing veto.
            # Returns 'enter' (proceed), 'hold' (defer this cycle), or 'skip' (reject).
            # No-op when no checkpoint loaded — returns 'enter'.
            if accepted:
                try:
                    from feature_governance.registry import is_active as _fg_active_f21
                    # cont. 52: anti-deadlock short-circuit. When the minute
                    # agent has been flagged as a "always-skip" deadlock by
                    # the counter below, treat F21 as inactive on the consume
                    # side. The flag is only cleared by a human swapping the
                    # checkpoint and restarting (deliberate — prevents silent
                    # re-enable of a broken model).
                    _marl_deadlock = (r.get("marl:minute:deadlock_detected") == "1"
                                      or r.get("marl:minute:deadlock_detected") == b"1")
                    if _fg_active_f21("F21") and not _marl_deadlock:
                        from ml.marl import get_minute_agent_action
                        _minute_obs = {
                            "ofi": float(r.get(redis_keys.OFI.replace("{pair}", pair)) or 0),
                            "vpin": float(r.get(redis_keys.VPIN.replace("{pair}", pair)) or 0),
                            "spread": 0.0,
                        }
                        minute_action = get_minute_agent_action(_minute_obs)
                        if minute_action == "skip":
                            accepted = False
                            rejection_reason = "marl_minute_skip"
                            log.warning("marl_minute_rejected", pair=pair,
                                        action="skip", signal_strength=signal.get("signal_strength"))
                            try:
                                r.incr("marl:minute:skip_count")
                                r.set("marl:minute:last_skip_ts", int(time.time()))
                                # Anti-deadlock: if >80% of last 50 decisions are skip, disable
                                _skip_n = int(r.get("marl:minute:skip_count") or 0)
                                _call_n = int(r.get("marl:minute:call_count") or 1)
                                if _call_n >= 50 and _skip_n / max(_call_n, 1) > 0.80:
                                    r.set("marl:minute:deadlock_detected", "1")
                                    log.error("marl_minute_deadlock",
                                              skip_pct=round(_skip_n / max(_call_n, 1), 2),
                                              call_n=_call_n,
                                              msg="Minute agent skipping >80% — checkpoint likely broken")
                            except Exception:
                                pass
                        elif minute_action == "hold":
                            accepted = False
                            rejection_reason = "marl_minute_hold"
                            log.info("marl_minute_rejected", pair=pair, action="hold",
                                     signal_strength=signal.get("signal_strength"))
                except Exception as exc:
                    log.warning("marl_minute_skipped", pair=pair, error=str(exc)[:120])

            signal["accepted"] = accepted
            signal["rejection_reason"] = rejection_reason if not accepted else None
            if debate_log is not None:
                signal["debate_verdict"] = debate_log.get("verdict")

            signal_id = write_signal(signal)

            if accepted:
                trade_id = None
                try:
                    from risk.manager import (
                        compute_initial_sl, assign_leverage, _volatility_unit,
                    )
                    _routed_sid = brain_state.get("active_strategy_id")
                    initial_sl = compute_initial_sl(pair, signal["direction"],
                                                    strategy_id=_routed_sid)
                    # Blueprint F12: dynamic capital allocation by Trade Potential
                    # Score. Pre-cont.41 the brain handed every signal the same
                    # `default_capital_usdt` regardless of potential — capital
                    # could only scale DOWN (DCA reserve, Kelly, Day Agent), never
                    # UP for high-potential trades. Result: every trade landed
                    # near the floor at $14-24 / 2-4% of balance, well below the
                    # 5%–30% band the blueprint specifies.
                    #
                    # Cont. 41 refined: scale by potential within the blueprint
                    # 5%–30% band, but DO NOT clamp at the brain's
                    # `default_capital_usdt` — that key carries Kelly/DayAgent's
                    # *expected-value* sizing, which represents the AVERAGE trade.
                    # High-potential trades should be allowed to exceed it (they're
                    # above-average bets). The TRUE hard ceiling is the absolute
                    # 30% band cap plus the DCA-reserve auto-scale already
                    # computed by the brain (slot capacity). Encode both here.
                    _base_capital = float(brain_state.get("default_capital_usdt", 100) or 100)
                    try:
                        _balance_for_pct = float(
                            r.get(redis_keys.VIRTUAL_BALANCE)
                            or r.get(redis_keys.ACCOUNT_BALANCE) or 0
                        )
                    except (TypeError, ValueError):
                        _balance_for_pct = 0.0
                    if _balance_for_pct > 0:
                        # cont. 52b: 100 % capital deployment mode (user-mandated
                        # 2026-05-27). When `bot:full_deploy_mode` is "1" (the
                        # default — explicit "0" required to revert) the engine
                        # uses the brain's fair-share allocation
                        # (`balance / slots_remaining` after Kelly / Day-Agent
                        # caps) verbatim. The previous F12 upward-scaling path
                        # could push a single trade to 17-30 % of balance, so
                        # the first 3-5 trades consumed all available capital
                        # and later slots stayed empty — the bot ended up
                        # deploying ~40-60 % of balance instead of 100 %.
                        #
                        # In full-deploy mode we simply hand off the brain's
                        # per-slot ceiling. The brain already recomputes
                        # `balance / slots_remaining` each cycle so as earlier
                        # trades close (PnL ± fees) the next trade's budget
                        # adjusts. Sum across all max_open slots ≈ balance.
                        _flag = r.get("bot:full_deploy_mode")
                        _full_deploy = (_flag is None or _flag in ("1", b"1"))
                        if _full_deploy:
                            # Recompute fair share locally — `_base_capital` from
                            # brain has already passed through Kelly / Day-Agent
                            # down-caps (capable of pinning capital to 5 % of
                            # balance even when the slot share is much larger),
                            # so deferring to it would land at ~50 % deployment
                            # instead of 100 %.
                            #
                            # In full-deploy mode we explicitly bypass those
                            # caps: the user has asked for 100 % capital
                            # utilisation, accepting that Kelly's risk-managed
                            # sizing is overridden. Reverts the moment
                            # `bot:full_deploy_mode` is set to "0".
                            #
                            # cont. 52d — ALSO respect `bot:max_position_usdt`
                            # as an absolute hard cap. The user's per-trade
                            # ceiling is non-negotiable; full-deploy means
                            # "deploy up to 100 % subject to this cap", not
                            # "ignore the cap". Without this guard, the LAST
                            # slot tries to soak up all remaining free balance
                            # (e.g. $162 into one slot when cap is $50).
                            from memory.query import get_open_trades as _got
                            _open_n = len(_got())
                            _slots_remaining = max(1, max_open - _open_n)
                            _fair = _balance_for_pct / _slots_remaining
                            try:
                                _max_pos = float(r.get("bot:max_position_usdt") or 0) or 1e9
                            except (TypeError, ValueError):
                                _max_pos = 1e9
                            _fair_capped = min(_fair, _max_pos)
                            capital_usdt = max(5.0, round(_fair_capped, 2))
                            log.info("capital_full_deploy",
                                     pair=pair,
                                     capital=capital_usdt,
                                     fair_share=round(_fair, 2),
                                     max_pos_cap=round(_max_pos, 2),
                                     capped=(_fair > _max_pos),
                                     brain_default=round(_base_capital, 2),
                                     balance=round(_balance_for_pct, 2),
                                     open_trades=_open_n,
                                     slots_remaining=_slots_remaining)
                        else:
                            _potential_pct = max(0.0, min(100.0, float(
                                signal.get("trade_potential",
                                           signal.get("signal_strength", 50.0)) or 50.0
                            )))
                            _min_pct = float(config.capital.per_trade_min_pct)
                            _max_pct = float(config.capital.per_trade_max_pct)
                            _scaled_pct = _min_pct + (_max_pct - _min_pct) * (_potential_pct / 100.0)
                            _scaled_capital = _balance_for_pct * _scaled_pct / 100.0
                            # Absolute hard ceiling — never exceed 30% of balance.
                            _abs_cap = _balance_for_pct * _max_pct / 100.0
                            # DCA-reserve ceiling: this is what slot capacity allows.
                            # `_base_capital` from brain already factors in the
                            # available DCA reserve and Kelly. Treat it as a HARD
                            # ceiling only when it's larger than the Kelly-style
                            # average — i.e. only when balance has room. The brain
                            # computes `max(5.0, max_affordable)` so a very small
                            # `_base_capital` indicates real slot starvation;
                            # respect it then.
                            if _base_capital < _balance_for_pct * 0.04:
                                # slot-starved → use brain ceiling, ignore upward scaling
                                capital_usdt = min(_scaled_capital, _base_capital)
                            else:
                                # plenty of slot capacity → scale up to abs cap
                                capital_usdt = min(_scaled_capital, _abs_cap)
                            capital_usdt = max(5.0, round(capital_usdt, 2))
                            log.info("capital_scaled",
                                     pair=pair,
                                     potential=round(_potential_pct, 1),
                                     scaled_pct=round(_scaled_pct, 2),
                                     capital=capital_usdt,
                                     brain_default=round(_base_capital, 2),
                                     balance=round(_balance_for_pct, 2))
                    else:
                        capital_usdt = _base_capital
                    if debate_size_mult != 1.0:
                        capital_usdt = max(5.0, round(capital_usdt * debate_size_mult, 2))
                    # R4 — bot self-confidence sizing (cont. 55). No-op when
                    # F46 gated off or confidence=1.0. Drops capital
                    # proportionally during loss/miss streaks.
                    try:
                        from metacognition.confidence import get_confidence
                        _conf = get_confidence()
                        if _conf < 1.0:
                            capital_usdt = max(5.0, round(capital_usdt * _conf, 2))
                            try:
                                r.incr("bot:confidence:sizing_applied_count")
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # F8 router capital sizing: per-strategy capital_pct_mult,
                    # clipped to [0.25, 2.0] so an LLM-emitted mult can't blow
                    # the user's position-band. Added 2026-05-21 cont. 13.
                    if _routed_sid:
                        try:
                            from strategy.router import (
                                get_capital_overrides, record_routed_capital,
                            )
                            _cap_ov = get_capital_overrides(_routed_sid)
                            if _cap_ov and "capital_pct_mult" in _cap_ov:
                                _m = float(_cap_ov["capital_pct_mult"])
                                _m = max(0.25, min(2.0, _m))
                                if _m != 1.0:
                                    capital_usdt = max(
                                        5.0, round(capital_usdt * _m, 2)
                                    )
                                    record_routed_capital(_routed_sid, _m, capital_usdt)
                        except Exception as exc:
                            log.debug("capital_router_skipped", error=str(exc)[:120])
                    # cont. 70 Track 1b — archetype capital tilt (fade>momentum,
                    # cont.69v). Layered AFTER the F8 capital_pct_mult. Gate-not-
                    # kill: resizes capital only, never rejects an entry.
                    if _routed_sid:
                        _am = _archetype_capital_mult(_routed_sid, r)
                        if _am != 1.0:
                            capital_usdt = max(5.0, round(capital_usdt * _am, 2))
                    # cont.70f Track-4 — Chronos vol-uncertainty size multiplier.
                    # Wider near-term predictive band (more uncertain) -> size
                    # DOWN; tight band -> size up modestly. Soft (gate-not-kill),
                    # Redis-tunable, asymmetric clip [0.5,1.25].
                    try:
                        if r.get("vol_prior:size_enabled") != "0":
                            from ml.foundation_forecast import load_forecast as _fdn_vf
                            _vf = _fdn_vf(pair)
                            if _vf:
                                _sf = float(_vf.get("spread_frac", 0) or 0)
                                if _sf > 0:
                                    _ref = float(r.get("vol_prior:size_ref") or 0.03)
                                    _lo = float(r.get("vol_prior:size_min") or 0.5)
                                    _hi = float(r.get("vol_prior:size_max") or 1.25)
                                    _vm = max(_lo, min(_hi, _ref / _sf))
                                    if _vm != 1.0:
                                        capital_usdt = max(5.0, round(capital_usdt * _vm, 2))
                                        r.incr("vol_prior:size:down_count" if _vm < 1
                                               else "vol_prior:size:up_count")
                    except Exception as exc:
                        log.debug("vol_prior_sizing_skipped", error=str(exc)[:120])
                    # Seed gene pool D family: overlay piggy-back.
                    # When bandit picks vol_target_sizing_overlay or
                    # hmm_regime_gate_overlay, apply the overlay's behaviour
                    # on top of the existing capital + entry decisions.
                    # Honest scope (Rule 4): vol_target scales capital here;
                    # hmm_gate blocks NEW entries here. Force-closing OPEN
                    # positions on regime flip is handled by the existing
                    # cont. 60 frontier kill switches, not by this overlay.
                    if _routed_sid:
                        try:
                            from strategy.router import (
                                get_overlay_metadata, apply_vol_target_sizing,
                                check_hmm_regime_kill, record_overlay_piggyback,
                            )
                            _overlay = get_overlay_metadata(_routed_sid)
                            if _overlay:
                                _otype = _overlay.get("overlay_type")
                                if _otype == "sizing":
                                    _new_cap, _reason = apply_vol_target_sizing(
                                        capital_usdt, pair,
                                        target_vol_pct=float(_overlay.get(
                                            "target_realized_vol_pct", 0.20)),
                                        lookback_bars=int(_overlay.get(
                                            "vol_lookback_bars", 1440)),
                                    )
                                    if _new_cap != capital_usdt:
                                        log.info("overlay_vol_target_applied",
                                                 pair=pair,
                                                 capital_before=capital_usdt,
                                                 capital_after=_new_cap,
                                                 reason=_reason)
                                        capital_usdt = _new_cap
                                    record_overlay_piggyback(
                                        _routed_sid, _otype,
                                        "applied" if _reason == "applied" else "skipped")
                                elif _otype == "regime_gate":
                                    _block, _reason = check_hmm_regime_kill(
                                        hmm_confidence_min=float(_overlay.get(
                                            "hmm_confidence_min", 0.7)),
                                        kill_in_turbulent=bool(_overlay.get(
                                            "kill_in_turbulent", True)),
                                    )
                                    if _block:
                                        log.info("overlay_hmm_regime_blocked",
                                                 pair=pair, reason=_reason,
                                                 strategy_id=_routed_sid)
                                        record_overlay_piggyback(
                                            _routed_sid, _otype, "blocked")
                                        try:
                                            r.incr("signal:reject:hmm_regime_gate_overlay")
                                        except Exception:
                                            pass
                                        continue
                                    record_overlay_piggyback(
                                        _routed_sid, _otype, "applied")
                        except Exception as exc:
                            # Silent-rejection fix per [[feedback_silent_rejection]]:
                            # the overlay machinery failing was previously a
                            # debug-only log with no counter — invisible in
                            # feature_health. Surface it as a Redis counter so a
                            # broken overlay path can't hide.
                            log.debug("overlay_piggyback_skipped",
                                      error=str(exc)[:120])
                            try:
                                r.incr("strategy_router:overlay_piggyback_error_count")
                                r.set("strategy_router:overlay_piggyback_last_error",
                                      str(exc)[:160])
                            except Exception:
                                pass
                    # F16 (Blueprint Feature 16): Fractional Kelly upper bound.
                    # After all dynamic sizing (signal-strength scaling, debate
                    # mult, F8 router mult) cap capital_usdt at the half-Kelly
                    # allocation derived from rolling win-rate + payoff ratio.
                    # Kelly never EXPANDS capital — only caps it down to the
                    # Kelly-optimal level. Inactive (no-op) until ≥30 closed
                    # trades exist. Output already clamped to 5%–30% of balance
                    # by compute_kelly_capital. Activates at Phase 1 per blueprint.
                    #
                    # cont. 52b: skipped in full_deploy_mode. The user has
                    # explicitly mandated 100 % capital deployment, which
                    # conflicts with Kelly's down-capping toward 5–10 % of
                    # balance per trade. Reverts the moment `bot:full_deploy_mode`
                    # is set to "0" (Kelly re-engages immediately).
                    if _balance_for_pct > 0 and not _full_deploy:
                        try:
                            from risk.manager import compute_kelly_capital as _ckc
                            _kelly = _ckc(_balance_for_pct)
                            if _kelly["active"] and _kelly["kelly_capital_usdt"] is not None:
                                _kelly_cap = float(_kelly["kelly_capital_usdt"])
                                if _kelly_cap < capital_usdt:
                                    _before = capital_usdt
                                    capital_usdt = max(5.0, round(_kelly_cap, 2))
                                    log.info("kelly_capital_applied",
                                             pair=pair,
                                             capital_before=_before,
                                             capital_after=capital_usdt,
                                             kelly_fraction=_kelly["kelly_fraction"],
                                             win_rate=_kelly["win_rate"],
                                             payoff_ratio=_kelly["payoff_ratio"],
                                             n_trades=_kelly["n_trades"])
                                    try:
                                        r.incr("brain:kelly:capped_count")
                                    except Exception:
                                        pass
                                else:
                                    try:
                                        r.incr("brain:kelly:nonbinding_count")
                                    except Exception:
                                        pass
                        except Exception as exc:
                            log.debug("kelly_capital_skipped", pair=pair, error=str(exc)[:120])
                    # cont. 52d — final hard clamp on `bot:max_position_usdt`.
                    # Applied AFTER every up-multiplier (debate, F8 router) and
                    # any future scaler so the user's per-trade ceiling is
                    # absolute. F8's `capital_pct_mult ∈ [0.25, 2.0]` was the
                    # path that re-grew capital past the cap (cont. 52d bug:
                    # HYPEUSDT opened at $102 when cap was $50 because F8
                    # multiplied a $50 base by ~2×). This clamp closes that
                    # loophole regardless of which upstream multiplier fires.
                    try:
                        _max_pos_final = float(r.get("bot:max_position_usdt") or 0)
                    except (TypeError, ValueError):
                        _max_pos_final = 0.0
                    if _max_pos_final > 0 and capital_usdt > _max_pos_final:
                        _before = capital_usdt
                        capital_usdt = max(5.0, round(_max_pos_final, 2))
                        log.info("capital_max_pos_clamped",
                                 pair=pair,
                                 capital_before=_before,
                                 capital_after=capital_usdt,
                                 max_pos_cap=_max_pos_final)

                    # Per-trade MIN floor (bot:min_position_usdt). Mirrors the max clamp:
                    # each trade deploys AT LEAST the user's floor (no dust trades). Optional —
                    # unset/0 keeps the built-in $5 minimum. Clamped not to exceed the max cap.
                    try:
                        _min_pos_final = float(r.get("bot:min_position_usdt") or 0)
                    except (TypeError, ValueError):
                        _min_pos_final = 0.0
                    if _min_pos_final > 0:
                        if _max_pos_final and _max_pos_final > 0:
                            _min_pos_final = min(_min_pos_final, _max_pos_final)
                        if capital_usdt < _min_pos_final:
                            _before_min = capital_usdt
                            capital_usdt = round(_min_pos_final, 2)
                            log.info("capital_min_pos_floored",
                                     pair=pair,
                                     capital_before=_before_min,
                                     capital_after=capital_usdt,
                                     min_pos_floor=_min_pos_final)

                    # cont. 61 audit fix — Borderline liquidity → halve capital.
                    # The entry gate (accept_or_reject) flagged pairs with
                    # $10M-$50M 24h volume as borderline. Reduce capital here
                    # so the position is smaller on rug-prone pairs while
                    # still allowing the trade. Falls open if redis key absent.
                    try:
                        if r.get(f"signal:liquidity_borderline:{pair}") == "1":
                            _before = capital_usdt
                            capital_usdt = max(5.0, round(capital_usdt * 0.5, 2))
                            log.info("capital_liquidity_borderline_halved",
                                     pair=pair,
                                     capital_before=_before,
                                     capital_after=capital_usdt)
                            try: r.incr("signal:liquidity_borderline_capital_halved_count")
                            except Exception: pass
                    except Exception:
                        pass
                    # Blueprint F12: dynamic leverage by Trade Potential Score
                    # + pair volatility (5x–20x band). Dashboard `bot:leverage`
                    # acts as an optional user-imposed hard cap, not a fixed value.
                    _potential = float(signal.get("trade_potential",
                                                  signal.get("signal_strength", 50.0)) or 50.0)
                    _vol_unit = _volatility_unit(r, pair)
                    leverage = assign_leverage(_potential, _vol_unit)
                    _lev_cap_raw = r.get("bot:leverage")
                    if _lev_cap_raw:
                        try:
                            leverage = min(leverage, max(1, int(_lev_cap_raw)))
                        except (TypeError, ValueError):
                            pass
                    log.info("leverage_assigned", pair=pair, leverage=leverage,
                             potential=round(_potential, 2),
                             vol_unit=round(_vol_unit, 4))
                    mark_for_qty = float(signal.get("mark_price") or 0)
                    if mark_for_qty <= 0:
                        log.error("no_mark_price_for_qty", pair=pair)
                        continue
                    # cont. 62 — Capital-anchored SL floor (owner mandate
                    # 2026-05-29). compute_initial_sl ran earlier without
                    # capital/leverage in scope; widen now if 50%-of-capital
                    # implies a wider distance than the vol-driven raw_sl.
                    try:
                        from risk.manager import (apply_capital_sl_floor,
                                                  apply_capital_sl_ceiling)
                        initial_sl = apply_capital_sl_floor(
                            initial_sl, mark_for_qty, signal["direction"],
                            capital_usdt, leverage, r=r)
                        # cont. 65k Guard 3 — cap SL distance (≤80% capital) so it
                        # never places past the liquidation point.
                        initial_sl = apply_capital_sl_ceiling(
                            initial_sl, mark_for_qty, signal["direction"],
                            capital_usdt, leverage, r=r)
                    except Exception as _csl_exc:
                        log.debug("capital_sl_floor_skipped",
                                  pair=pair, error=str(_csl_exc)[:120])
                    # quantity = notional / mark_price = (capital * leverage) / mark_price
                    quantity = round((capital_usdt * leverage) / mark_for_qty, 8)
                    _open_params = {
                        "pair": pair,
                        "direction": signal["direction"],
                        "brain_stage": brain_state.get("stage", 1),
                        "capital_usdt": capital_usdt,
                        "leverage": leverage,
                        "quantity": quantity,
                        "strategy_id": brain_state.get("active_strategy_id"),
                        "timeframe": signal.get("timeframe"),
                        "market_regime": signal.get("market_regime"),
                        "trade_potential_score": signal.get("trade_potential", signal.get("signal_strength")),
                        "direction_confidence": signal.get("direction_confidence", signal.get("signal_strength")),
                        "trailing_sl_level": initial_sl,
                        "average_entry": None,
                        "feature_vector": signal.get("feature_vector"),
                    }
                    # cont. 69: predicted-entry-offset LIMIT entry (default OFF).
                    # When enabled AND the model emits a meaningful per-pair
                    # offset, queue a pending limit entry instead of market-
                    # opening; the execution.limit_entry loop fills it when the
                    # mark reaches the target (or markets/skips on TTL). Inert
                    # (returns None → market path) with the current degenerate
                    # offset model. signal_id is already set (write_signal above).
                    try:
                        from execution.limit_entry import (
                            compute_target_entry as _cte,
                            queue_pending_entry as _qpe)
                        _ltarget = _cte(r, pair, signal["direction"], mark_for_qty)
                    except Exception:
                        _ltarget = None
                    if _ltarget is not None:
                        try:
                            _qpe(r, _open_params, signal_id, _ltarget)
                        except Exception as _lq_exc:
                            log.warning("limit_entry_queue_failed", pair=pair,
                                        error=str(_lq_exc)[:150])
                            # fall through to market open on queue failure
                        else:
                            continue   # queued — skip immediate market open
                    trade_id = engine.open_trade({
                        "pair": pair,
                        "direction": signal["direction"],
                        "brain_stage": brain_state.get("stage", 1),
                        "capital_usdt": capital_usdt,
                        "leverage": leverage,
                        "quantity": quantity,
                        "strategy_id": brain_state.get("active_strategy_id"),
                        "timeframe": signal.get("timeframe"),
                        "market_regime": signal.get("market_regime"),
                        "trade_potential_score": signal.get("trade_potential", signal.get("signal_strength")),
                        "direction_confidence": signal.get("direction_confidence", signal.get("signal_strength")),
                        "trailing_sl_level": initial_sl,
                        "average_entry": None,
                        "feature_vector": signal.get("feature_vector"),
                    })
                    opened_trade_ids.append(trade_id)

                    # cont. 70 — Launch-Pad P5: retire the fired slot (→ history)
                    # + cooldown the symbol + bump open counter; the P4 maintainer
                    # refills the freed slot on its next tick. Best-effort.
                    if _lp_mode and pair in _lp_slot:
                        try:
                            _lp_gate.on_open(r, pair, _lp_slot[pair], trade_id)
                        except Exception as _lp_oe:
                            log.warning("launchpad_on_open_failed",
                                        pair=pair, error=str(_lp_oe)[:160])

                    # cont. 65 — back-link signals.trade_id so xgb_predictor
                    # training JOINs (s.trade_id = t.id) actually match. Prior
                    # to this fix, 0 of 14,565 signals in a 24h window had
                    # trade_id populated → training rows all zero-filled →
                    # degenerate predictor regardless of feature_vector being
                    # rich. This single UPDATE is what makes Option B (rich
                    # feature_vector snapshots) actually load through to the
                    # training set.
                    try:
                        from db import db_conn as _db_conn_bl
                        with _db_conn_bl() as _conn_bl:
                            with _conn_bl.cursor() as _cur_bl:
                                _cur_bl.execute(
                                    "UPDATE signals SET trade_id = %s "
                                    "WHERE id = %s",
                                    (trade_id, signal_id),
                                )
                    except Exception as _bl_exc:
                        log.warning("signal_trade_id_backlink_failed",
                                    signal_id=signal_id,
                                    trade_id=trade_id,
                                    error=str(_bl_exc)[:120])

                    # F48 §Idea B — Persist magnitude-driven TP1 / TP2 in Redis
                    # keyed by trade_id. monitor_trailing_sl reads these to close
                    # the trade when mark crosses either level.
                    try:
                        from risk.manager import compute_tp_targets
                        _tps = compute_tp_targets(
                            pair, signal["direction"],
                            entry_price=mark_for_qty,
                            leverage=leverage,   # cont. 65k Guard 2 — per-lev TP ceiling
                        )
                        if _tps:
                            r.set(f"trade:{trade_id}:tp1", _tps["tp1"])
                            r.set(f"trade:{trade_id}:tp2", _tps["tp2"])
                            # Phase A write-through (cont. 55).
                            r.set(f"trade:{trade_id}:tp",
                                  _tps.get("tp", _tps["tp1"]))
                            r.set(f"trade:{trade_id}:mag1_pct", _tps["mag1_pct"])
                            r.set(f"trade:{trade_id}:mag3_pct", _tps["mag3_pct"])
                            from memory.write import write_trade_update as _wtu
                            _wtu(trade_id, {
                                # cont. 65k-5 — write the CANONICAL tp1/tp2/tp
                                # columns (what the dashboard reads) in addition to
                                # the *_target columns. Without these, the dashboard
                                # showed NULL TP for every open trade even though
                                # the engine had them in Redis + *_target.
                                "tp1":        _tps["tp1"],
                                "tp2":        _tps["tp2"],
                                "tp":         _tps.get("tp", _tps["tp1"]),
                                "tp1_target": _tps["tp1"],
                                "tp2_target": _tps["tp2"],
                                "tp_target":  _tps.get("tp", _tps["tp1"]),
                                "mag1_pct":   _tps["mag1_pct"],
                                "mag3_pct":   _tps["mag3_pct"],
                            })
                            log.info("candlenet_tp_set",
                                     trade_id=trade_id, pair=pair,
                                     tp1=_tps["tp1"], tp2=_tps["tp2"])
                    except Exception as exc:
                        log.debug("candlenet_tp_skipped",
                                  trade_id=trade_id, error=str(exc)[:120])

                    # F48 §Idea C — Capture the 22-dim entry-timing state at
                    # trade open + signal_score + atr_norm, keyed by trade_id.
                    # execution/paper.close_trade and live.close_trade read
                    # this at close time, compute pnl_per_step from realised
                    # PnL, and call ml.entry_timing_agent.log_signal_event() so
                    # the PPO agent has training data. Without this producer,
                    # train_entry_timing() always returns insufficient_history.
                    try:
                        import json as _j
                        from ml.entry_timing_agent import build_state
                        _state = build_state(pair,
                                             signal_score=_potential,  # cont. 69: was `trade_potential` (undefined in this scope → NameError silently killed F48 entry-timing capture)
                                             signal_age_candles=0)
                        _mark_at_open = mark_for_qty
                        _atr_at_open  = _safe_float(r.get(f"{pair}:atr"))
                        _atr_norm     = (_atr_at_open / _mark_at_open) if _mark_at_open > 0 else 0.01
                        r.setex(f"trade:{trade_id}:signal_event_state", 86400,
                                _j.dumps({
                                    "pair":          pair,
                                    "direction":     signal["direction"],
                                    "signal_score":  float(_potential),  # cont. 69: was trade_potential (NameError)
                                    "atr_norm":      float(_atr_norm),
                                    "state":         _state.tolist(),
                                    "open_price":    float(_mark_at_open),
                                }))
                    except Exception as exc:
                        log.debug("entry_timing_state_capture_failed",
                                  trade_id=trade_id, error=str(exc)[:120])

                    # F49 §Component 2 — Log model predictions for the
                    # performance monitor. Each prediction is paired with the
                    # eventual trade-close outcome by execution/paper.close_trade
                    # and execution/live.close_trade.
                    try:
                        import json as _j
                        for _interval in ("1m", "5m", "15m"):
                            _raw = r.get(f"{pair}:{_interval}:candle_forecast")
                            if not _raw:
                                continue
                            _fc = _j.loads(_raw)
                            _pred = float(_fc.get("dir1", 0.5))
                            r.set(f"trade:{trade_id}:pred_candlenet_{_interval}", _pred)
                    except Exception:
                        pass
                    # Direction model's confidence (when available)
                    try:
                        _dc = signal.get("direction_confidence")
                        if _dc is not None:
                            # Normalise 0-100 → 0-1 P(up) style. For shorts, the
                            # outcome semantics are inverted at close time.
                            r.set(f"trade:{trade_id}:pred_direction_model",
                                  float(_dc) / 100.0)
                    except Exception:
                        pass
                except Exception as exc:
                    log.error("trade_open_failed", pair=pair, error=str(exc))
                # Blueprint F37 verbal reinforcement requires per-round, per-agent
                # arguments to be persisted so update_beliefs_on_close can compute
                # was_correct at trade close. Persist AFTER open_trade so trade_id
                # is linked — close-time JOIN goes through trade_id, not signal_id.
                if debate_log is not None and trade_id is not None:
                    try:
                        from debate.council import save_debate_arguments
                        save_debate_arguments(signal_id, trade_id, debate_log)
                    except Exception as exc:
                        log.warning("save_debate_args_failed", pair=pair,
                                    error=str(exc)[:120])
            else:
                _schedule_counterfactual(signal_id, pair)
                # Phase 1 producer (cont. 63, 2026-05-29) — push to replay
                # pool when the rejection reason is recoverable AND strength
                # is at/above the Bayesian-derived T_low floor. The producer
                # internally filters by recoverable-reason whitelist and the
                # T_low admission floor, so the call here is unconditional.
                try:
                    from signals.replay_pool import push as _replay_push
                    _replay_push(signal, rejection_reason or "", signal_id)
                except Exception as _replay_push_exc:
                    log.debug("replay_pool_push_call_failed",
                              pair=pair,
                              error=str(_replay_push_exc)[:200])
                # cont. 69s — F9/F12 §4.3 EV-override (SHADOW). For recoverable
                # rejects, evaluate at decision time whether taking at reduced
                # size is positive-EV (bayes p_win × CF peak/dd). In shadow mode
                # this only measures + audits — it takes NO trade. Live-take is
                # gated behind ev_override:live (default 0) pending the full-deploy
                # capital decision (predicted_profit_loop.md §7.5).
                try:
                    from signals.replay_pool import _is_recoverable as _ev_recov
                    if _ev_recov(rejection_reason or ""):
                        from signals.ev_override import evaluate as _ev_eval
                        _ev_lev = float(r.get("bot:leverage") or 20)
                        _ev_cap = float(brain_state.get("default_capital_usdt", 50) or 50)
                        _ev_eval(signal, rejection_reason or "",
                                 notional=_ev_cap * _ev_lev, signal_id=signal_id)
                except Exception as _ev_exc:
                    log.debug("ev_override_call_failed", pair=pair,
                              error=str(_ev_exc)[:160])
                # Persist debate args even on rejection — useful for the
                # counterfactual sweep to compare debate outcomes against
                # shadow performance. trade_id stays NULL.
                if debate_log is not None:
                    try:
                        from debate.council import save_debate_arguments
                        save_debate_arguments(signal_id, None, debate_log)
                    except Exception as exc:
                        log.warning("save_debate_args_rejected_failed", pair=pair,
                                    error=str(exc)[:120])

    return opened_trade_ids


def _schedule_counterfactual(signal_id: str, pair: str) -> None:
    """T-04: Counterfactual evaluation 72h after rejection.

    PREVIOUSLY: used `apply_async(countdown=72h)` — Celery stores ETAs in worker
    memory only, so every celery_worker restart dropped all pending evaluations.
    The signal-level rejection counter would go up but counterfactuals table
    stayed empty forever.

    NOW: rejection only writes the signal row; the periodic Celery beat task
    `sweep_pending_counterfactuals` (hourly) finds aged rejected signals via
    LEFT JOIN against counterfactuals and evaluates them. State lives in the DB,
    so restarts no longer drop work.

    This function is kept as a no-op for backward compat (callers can still call
    it) — actual evaluation happens via the sweeper. Logs a debug event so it's
    traceable.
    """
    log.debug("counterfactual_pending_sweep", signal_id=signal_id, pair=pair)


def get_shadow_win_rate() -> dict:
    """T-05: Return running shadow win rate from Redis."""
    r = redis_client.get()
    raw = r.get(redis_keys.SHADOW_WIN_RATE)
    if raw:
        return json.loads(raw)
    return {"total": 0, "won": 0, "rate": 0.0}
