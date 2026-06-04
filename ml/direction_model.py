"""F13 / X-09 / Blueprint §10.9: Direction Prediction Model — bidirectional.

Rewritten 2026-05-22 (cont. 25) from a unidirectional "did the brain get
direction right?" confidence scorer into a true Stage 3 direction PICKER.

The previous model only learned `P(brain_was_right | features)` — it could
say "I'm not confident in this trade" but had no way to suggest the brain
should have gone the OTHER way. That's why the audit found 1002/1555 (64%)
direction failures even with the model loaded: the model only *modulated
confidence*, never *picked direction*.

New formulation (Blueprint §10.9 "Two Separate Prediction Problems"):

  Target = P(win | direction, features)

The model takes `direction_bit` ∈ {0=short, 1=long} as an input feature
alongside the 7 microstructure/macro features. To predict the best direction
for a new signal, the caller queries the model twice — once with each bit —
and picks argmax.

Training data multiplier: every losing trade also contributes a synthetic
"counterfactual" training row using the stored `counterfactual_result`
(what the OPPOSITE direction would have netted). So a single losing trade
yields two labeled rows: one for the direction taken (label=loss), one for
the direction not taken (label = whether the cf_result was positive).

Lifecycle:
- train_from_closed_trades(): fit + persist to models/direction_model.pkl
  Triggered every 50 trades at ≥100 via Celery retrain_direction_model task.
- predict_direction_confidence(pair, direction): P(win) ∈ [0,100] for the
  given direction. Backward-compat: legacy callers passing only `pair`
  receive None and fall back to heuristic.
- predict_best_direction(pair): ("long"|"short", confidence) | None. Returns
  None when both P(win) < 0.5 or model unavailable. Used by signals/engine
  to OVERRIDE the OFI-rule direction picker when the model strongly
  disagrees.

Backwards compatibility: feature_vector pre-expansion only has ofi/vpin/
sentiment/mark — those rows are silently dropped from training. After cont.
25 the on-disk .pkl format is (model, scaler, feature_list); old (model,
scaler) tuples are detected on load and force a fresh retrain.
"""
import json
import pickle
from pathlib import Path
import structlog
import redis_client
import redis_keys
from db import db_conn

log = structlog.get_logger()

_MODEL_PATH = Path("models/direction_model.pkl")
_MIN_TRAIN_SAMPLES = 60
_BOOSTING_MIN_TRADES = 300
# Feature order MUST match training and prediction.
_FEATURES = [
    "ofi", "sentiment", "funding_rate", "change_24h",
    "volume_24h", "amihud", "dir_acc_pair",
]
_DIRECTION_BIT_NAME = "direction_bit"  # 1=long, 0=short
# Minimum P(win) for predict_best_direction to return a non-None pick.
# Both directions must score < this for "no opinion".
_MIN_PICK_CONFIDENCE = 0.50
# Margin required to OVERRIDE a caller's existing direction choice (used by
# signals/engine.py via predict_best_direction). Below this margin, model
# is "ambivalent" and caller's choice stands.
_OVERRIDE_MARGIN = 0.10

# cont. 37 validation gates. Refuse to save a newly-trained model that
# fails any of these on the held-out 20% test split. Set deliberately low
# (just barely-above-random) so a real signal lands but the kind of
# overfit pattern that produced cont. 36's anti-calibration is rejected.
#
# Why these specific thresholds:
#   AUC ≥ 0.55     — any discrimination above coin-flip on a balanced metric.
#   top_decile_lift ≥ 1.05  — the top-10% of predictions must win at least
#                              5% above base rate (the OLD model's top decile
#                              was BELOW base rate — anti-calibrated; this
#                              gate would have rejected it).
_VALIDATION_GATES = {
    "min_test_auc":          0.55,
    "min_top_decile_lift":   1.05,   # multiple of pos_rate
}
_TEST_FRACTION = 0.20
_TEST_SPLIT_SEED = 42

# (model, scaler, feature_list, file_mtime) — feature_list lets us detect
# old-format .pkl files written before cont. 25 and force a retrain.
_cached: tuple | None = None


def _load():
    """Return (model, scaler, feature_list) or None. Reloads when on-disk
    file is newer than cache.

    Format history:
      - pre-cont.25:  (model, scaler)            REJECTED
      - cont.25:      (model, scaler, features)  REJECTED post-cont.37 —
                       no validation metrics on disk, can't trust it.
      - cont.37+:     (model, scaler, features, metrics_dict)  ACCEPTED
                       iff metrics_dict shows the validation gates passed.

    Refusing the cont.25 format on load means re-enabling F13 after
    cont.37 requires a fresh training pass under the new validation gate.
    A model that *would* pass the gates if retrained but happens to be
    sitting on disk in the old format gets ignored — intentional, the
    point is that on-disk artifacts must carry their proof of fitness."""
    global _cached
    if not _MODEL_PATH.exists():
        return None
    current_mtime = _MODEL_PATH.stat().st_mtime
    if _cached is None or _cached[3] != current_mtime:
        try:
            with open(_MODEL_PATH, "rb") as f:
                blob = pickle.load(f)
            if not isinstance(blob, tuple) or len(blob) != 4:
                log.warning("direction_model_old_format_ignored",
                            note="pre-cont.37 pkl — needs retrain under "
                                 "validation gates before it can be loaded")
                return None
            model, scaler, feature_list, metrics = blob
            if feature_list != _FEATURES + [_DIRECTION_BIT_NAME]:
                log.warning("direction_model_feature_mismatch",
                            on_disk=feature_list,
                            expected=_FEATURES + [_DIRECTION_BIT_NAME])
                return None
            # Defense in depth: re-check the on-disk model passed the
            # gates. A future change to gate thresholds will invalidate
            # older saved models without needing a manual file delete.
            if (metrics.get("test_auc", 0.0)
                    < _VALIDATION_GATES["min_test_auc"]
                or metrics.get("top_decile_lift", 0.0)
                    < _VALIDATION_GATES["min_top_decile_lift"]):
                log.warning("direction_model_load_rejected_by_gates",
                            test_auc=metrics.get("test_auc"),
                            top_decile_lift=metrics.get("top_decile_lift"),
                            gates=_VALIDATION_GATES)
                return None
            _cached = (model, scaler, feature_list, current_mtime)
            log.info("direction_model_loaded", mtime=current_mtime,
                     features=feature_list)
        except Exception as exc:
            log.warning("direction_model_load_failed", error=str(exc)[:200])
            return None
    return (_cached[0], _cached[1], _cached[2])


def train_from_closed_trades() -> dict:
    """Bidirectional fit. Builds (X, y) where X = [..., direction_bit] and
    y = 1 if that direction would have won on this trade.

    For each closed paper trade:
      - Row A (actual direction taken):
          direction_bit = 1 if long else 0
          y = 1 if net_pnl > 0 else 0
      - Row B (counterfactual — losing trades only, when cf_result available):
          direction_bit = opposite of taken
          y = 1 if counterfactual_result > 0 else 0

    Counterfactual rows double the loss-side training data and give the model
    direct evidence of "what the other side would have done" — essential for
    learning to PICK direction, not just rate it.
    """
    # cont. 37 — collect raw trade rows first, then split at the TRADE
    # level, THEN augment with counterfactuals on the training side only.
    # The cont. 25 augmentation creates two rows per losing trade (actual
    # + opposite-direction-cf) with the same feature vector. If a trade's
    # actual row lands in train and its cf row lands in test, the model
    # learns the joint pattern in train and the test set is no longer
    # independent. Honest evaluation requires splitting BEFORE
    # augmentation so the test set is purely held-out trades.
    # cont. 40 — blueprint §F13 diagnosis-tag filter. The Direction Predictor
    # is supposed to learn "which side wins given the signal", but losing
    # trades whose loss had nothing to do with direction (sl_too_tight,
    # regime_flip mid-trade, wrong_pair) inject noise into the gradient.
    # Keep wins + signal failures + clean direction failures only.
    #
    # SQL filter logic:
    #   - failure_type IN ('win', 'signal') OR NULL → keep unconditionally
    #     (no diagnosis required; either correct direction or opposite would
    #     have also lost)
    #   - failure_type = 'direction' → keep only when none of the
    #     confounding tags are set
    #   - failure_type = 'unclassified' → drop (not enough info)
    raw_trades: list[dict] = []
    _CONFOUNDING_TAGS = ("sl_too_tight", "regime_flip", "wrong_pair")
    confounding_jsonb = json.dumps(list(_CONFOUNDING_TAGS))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT feature_vector, direction, net_pnl_usdt,
                       counterfactual_result
                FROM trades
                WHERE status='closed' AND is_paper=true
                  AND feature_vector IS NOT NULL
                  AND direction IS NOT NULL
                  AND (
                        failure_type IS NULL
                     OR failure_type IN ('win', 'signal')
                     OR (failure_type = 'direction'
                         AND NOT (diagnosis_tags ?| %s::text[]))
                  )
            """, (list(_CONFOUNDING_TAGS),))
            for fv, direction, net_pnl, cf_raw in cur.fetchall():
                fv_dict = fv if isinstance(fv, dict) else json.loads(fv)
                feats = [fv_dict.get(k) for k in _FEATURES]
                if any(v is None for v in feats):
                    continue
                raw_trades.append({
                    "feats": [float(v) for v in feats],
                    "direction_bit": 1.0 if direction == "long" else 0.0,
                    "net_pnl": float(net_pnl or 0),
                    "cf_raw": cf_raw,
                })

    if len(raw_trades) < _MIN_TRAIN_SAMPLES:
        log.warning("direction_model_insufficient_data",
                    samples=len(raw_trades), need=_MIN_TRAIN_SAMPLES)
        return {"status": "insufficient_data", "samples": len(raw_trades)}

    import numpy as np
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import brier_score_loss, roc_auc_score

    # Split trades by win/loss BEFORE augmentation so no cf leakage.
    trade_labels = [1 if t["net_pnl"] > 0 else 0 for t in raw_trades]
    if len(set(trade_labels)) < 2:
        log.warning("direction_model_single_class",
                    samples=len(raw_trades),
                    pos=int(sum(trade_labels)))
        return {"status": "single_class", "samples": len(raw_trades)}

    train_trades, test_trades, _, _ = train_test_split(
        raw_trades, trade_labels,
        test_size=_TEST_FRACTION, stratify=trade_labels,
        random_state=_TEST_SPLIT_SEED,
    )

    def _expand(trades, *, augment_cf: bool):
        """Build (X, y, n_actual, n_cf) from a list of raw trade dicts.
        Counterfactual rows are only emitted when augment_cf=True — we
        want them in train (more data) but NOT in test (would inflate
        AUC with structurally-correlated rows)."""
        rows: list[tuple[list[float], int]] = []
        n_actual = 0
        n_cf = 0
        for t in trades:
            rows.append((t["feats"] + [t["direction_bit"]],
                         1 if t["net_pnl"] > 0 else 0))
            n_actual += 1
            if augment_cf and t["cf_raw"] is not None:
                try:
                    cf = t["cf_raw"]
                    cf_val = (float(cf) if not isinstance(cf, dict)
                              else float(cf.get("net_pnl", 0)))
                    rows.append((t["feats"] + [1.0 - t["direction_bit"]],
                                 1 if cf_val > 0 else 0))
                    n_cf += 1
                except (TypeError, ValueError):
                    pass
        X_arr = np.array([r[0] for r in rows], dtype=float)
        y_arr = np.array([r[1] for r in rows], dtype=int)
        return X_arr, y_arr, n_actual, n_cf

    X_train, y_train, n_actual_tr, n_cf_tr = _expand(
        train_trades, augment_cf=True)
    X_test, y_test, n_actual_te, _ = _expand(
        test_trades, augment_cf=False)
    n_actual = n_actual_tr + n_actual_te
    n_counterfactual = n_cf_tr
    rows = list(range(len(X_train) + len(X_test)))   # for len(rows) below

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled  = scaler.transform(X_test)

    # Tree boosting at ≥300 total samples (counts both actual + cf rows).
    if len(rows) >= _BOOSTING_MIN_TRADES:
        from sklearn.ensemble import GradientBoostingClassifier
        model = GradientBoostingClassifier(
            n_estimators=120, max_depth=3, learning_rate=0.05,
            subsample=0.8, random_state=42,
        )
        model_kind = "gboost"
    else:
        from sklearn.linear_model import LogisticRegression
        model = LogisticRegression(max_iter=500, class_weight="balanced")
        model_kind = "logreg"

    model.fit(X_train_scaled, y_train)
    train_acc = float(model.score(X_train_scaled, y_train))
    test_acc  = float(model.score(X_test_scaled,  y_test))
    # pos_rate is reported on TEST data (true held-out baseline). Train
    # pos_rate would be inflated by cf augmentation and isn't what the
    # top-decile-lift calculation compares against.
    pos_rate  = float(y_test.mean())

    # Held-out probability calibration. Brier (lower=better; 0.25 is the
    # baseline of always predicting pos_rate at 50/50; <0.22 = useful).
    p_test = model.predict_proba(X_test_scaled)[:, 1]
    test_brier = float(brier_score_loss(y_test, p_test))
    try:
        test_auc = float(roc_auc_score(y_test, p_test))
    except Exception:
        test_auc = 0.5

    # Top-decile lift — the critical calibration check that would have
    # rejected cont. 36's anti-calibrated model. We sort predictions and
    # take the top 10% bucket; its actual win rate should beat the base
    # rate. If it doesn't, the model's "high confidence" predictions are
    # worse than picking randomly, exactly the failure cont. 36 found.
    order = np.argsort(-p_test)
    decile_n = max(1, len(order) // 10)
    top_decile_idx = order[:decile_n]
    top_decile_winrate = float(y_test[top_decile_idx].mean())
    top_decile_lift = top_decile_winrate / pos_rate if pos_rate > 0 else 0.0

    # Validation gates — refuse to save if any fail.
    gate_failures: list[str] = []
    if test_auc < _VALIDATION_GATES["min_test_auc"]:
        gate_failures.append(f"test_auc {test_auc:.3f} < "
                             f"{_VALIDATION_GATES['min_test_auc']}")
    if top_decile_lift < _VALIDATION_GATES["min_top_decile_lift"]:
        gate_failures.append(
            f"top_decile_lift {top_decile_lift:.3f} < "
            f"{_VALIDATION_GATES['min_top_decile_lift']}")

    if hasattr(model, "feature_importances_"):
        importances = [round(float(v), 4) for v in model.feature_importances_]
    else:
        importances = [round(float(c), 4) for c in model.coef_[0]]

    metrics = {
        "train_acc": round(train_acc, 4),
        "test_acc":  round(test_acc, 4),
        "test_brier": round(test_brier, 4),
        "test_auc":  round(test_auc, 4),
        "top_decile_winrate": round(top_decile_winrate, 4),
        "top_decile_lift":    round(top_decile_lift, 4),
        "pos_rate":  round(pos_rate, 4),
        "n_train":   int(len(y_train)),
        "n_test":    int(len(y_test)),
        "feature_importances": dict(zip(_FEATURES + [_DIRECTION_BIT_NAME],
                                        importances)),
        "kind": model_kind,
    }

    # Always publish metrics — accepted or rejected — so the dashboard can
    # see the model failed validation rather than silently doing nothing.
    try:
        r = redis_client.get()
        # Pre-cont.37 dashboard key kept for backward compat.
        r.set("brain:direction_model_train_acc", round(train_acc, 4))
        r.set("brain:direction_model_test_acc",  round(test_acc, 4))
        r.set("brain:direction_model_test_brier", round(test_brier, 4))
        r.set("brain:direction_model_test_auc",  round(test_auc, 4))
        r.set("brain:direction_model_top_decile_winrate",
              round(top_decile_winrate, 4))
        r.set("brain:direction_model_top_decile_lift",
              round(top_decile_lift, 4))
        r.set("brain:direction_model_pos_rate", round(pos_rate, 4))
        r.set("brain:direction_model_samples", len(rows))
        r.set("brain:direction_model_n_actual", n_actual)
        r.set("brain:direction_model_n_counterfactual", n_counterfactual)
        r.set("brain:direction_model_kind", model_kind)
        r.set("brain:direction_model_last_train_ts", int(__import__("time").time()))
        r.set("brain:direction_model_accepted", "1" if not gate_failures else "0")
        if gate_failures:
            r.set("brain:direction_model_rejection_reason",
                  "; ".join(gate_failures))
        else:
            r.delete("brain:direction_model_rejection_reason")
    except Exception:
        pass

    if gate_failures:
        log.warning("direction_model_rejected_by_validation",
                    samples=len(rows), reasons=gate_failures, **metrics)
        return {"status": "rejected_validation",
                "samples": len(rows),
                "reasons": gate_failures, **metrics}

    # ── ACCEPTED — write to disk in cont.37 4-tuple format ───────────────
    _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    feature_list = _FEATURES + [_DIRECTION_BIT_NAME]
    with open(_MODEL_PATH, "wb") as f:
        pickle.dump((model, scaler, feature_list, metrics), f)

    global _cached
    _cached = None  # force reload in this process on next predict

    # F49 §Component 7 — reset the SGD online sidecar. The fresh batched
    # model already contains the signal the sidecar was accumulating; we
    # don't want stale online weights fighting the new canonical model.
    try:
        reset_online_sidecar()
    except Exception as exc:
        log.debug("online_sidecar_reset_skipped", error=str(exc)[:120])

    log.info("direction_model_trained_bidirectional",
             samples=len(rows), n_actual=n_actual,
             n_counterfactual=n_counterfactual,
             features=feature_list, importances=importances,
             **{k: v for k, v in metrics.items()
                if k != "feature_importances"})
    return {"status": "trained", "samples": len(rows),
            "n_actual": n_actual, "n_counterfactual": n_counterfactual,
            **metrics}


def _build_live_features(pair: str) -> list[float] | None:
    """Pull the 7 base features from Redis at prediction time. Returns None
    if essential features (ofi, sentiment) are missing."""
    r = redis_client.get()

    def _f(v, default=0.0):
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    ofi = r.get(redis_keys.OFI.replace("{pair}", pair))
    sent = (r.get(redis_keys.SENTIMENT_PAIR.replace("{pair}", pair))
            or r.get(redis_keys.SENTIMENT_GLOBAL))
    if ofi is None or sent is None:
        return None

    dir_acc = 50.0
    try:
        acc_raw = r.get(f"brain:directional_accuracy:{pair}")
        if acc_raw:
            d = json.loads(acc_raw)
            rate = d.get("rate")
            if rate is None and d.get("total"):
                rate = 100.0 * d.get("correct", 0) / d["total"]
            if rate is not None:
                dir_acc = float(rate)
    except Exception:
        pass

    return [
        _f(ofi),
        _f(sent),
        _f(r.get(f"{pair}:funding_rate")),
        _f(r.get(f"{pair}:change_24h")),
        _f(r.get(f"{pair}:volume_24h")),
        _f(r.get(f"{pair}:amihud")),
        dir_acc,
    ]


def _predict_p_win(pair: str, direction: str) -> float | None:
    """Internal: return P(win) ∈ [0, 1] for (pair, direction). None on miss."""
    cached = _load()
    if cached is None:
        return None
    model, scaler, _ = cached
    base = _build_live_features(pair)
    if base is None:
        return None
    direction_bit = 1.0 if direction == "long" else 0.0
    try:
        import numpy as np
        x_scaled = scaler.transform(np.array([base + [direction_bit]], dtype=float))
        return float(model.predict_proba(x_scaled)[0][1])
    except Exception as exc:
        log.error("direction_predict_failed", pair=pair,
                  direction=direction, error=str(exc)[:200])
        return None


def predict_direction_confidence(pair: str, direction: str | None = None) -> float | None:
    """0-100 confidence that `direction` wins on `pair` right now.

    If `direction` is None (legacy callers pre-cont.25), returns None to
    signal the caller should fall back to its heuristic confidence —
    bidirectional model has no opinion without a direction.
    """
    if direction not in ("long", "short"):
        return None
    p = _predict_p_win(pair, direction)
    if p is None:
        return None
    return round(p * 100, 2)


def predict_best_direction(pair: str) -> tuple[str, float] | None:
    """Return (best_direction, confidence_pct) or None.

    None semantics:
      - Model unavailable / features missing → caller falls back.
      - Both directions score < _MIN_PICK_CONFIDENCE (0.5) → no opinion.
      - |P(long) - P(short)| < _OVERRIDE_MARGIN (0.10) → ambivalent;
        caller's existing direction choice stands.

    Otherwise returns the direction with higher P(win) and its confidence
    rescaled to 0-100. Caller can compare to its own choice to decide
    whether to flip.
    """
    p_long = _predict_p_win(pair, "long")
    p_short = _predict_p_win(pair, "short")
    if p_long is None or p_short is None:
        return None
    if max(p_long, p_short) < _MIN_PICK_CONFIDENCE:
        return None
    if abs(p_long - p_short) < _OVERRIDE_MARGIN:
        return None
    if p_long > p_short:
        return ("long", round(p_long * 100, 2))
    else:
        return ("short", round(p_short * 100, 2))


# ── F49 §Component 7 — Online learning sidecar (SGDClassifier partial_fit) ──
# The main model is LogReg/GBC — neither supports partial_fit. An SGD sidecar
# is kept at `models/direction_model_online.pkl` and updated on every closed
# trade via `online_update()`. The sidecar uses the SAME scaler + feature
# layout as the main model so its predictions are commensurate. At the next
# full retrain the sidecar is reset (the main model captures the same data
# in batched form, refit from scratch with cf-augmentation).
_ONLINE_MODEL_PATH = Path("models/direction_model_online.pkl")
_ONLINE_LR        = 0.01           # SGD constant learning rate
_ONLINE_L2        = 1e-4           # weight decay (acts like a soft EWC anchor)
import threading
_online_lock = threading.Lock()


def _load_online_model():
    """Return (sgd, scaler, feature_list) or (None, None, None)."""
    if not _ONLINE_MODEL_PATH.exists():
        return (None, None, None)
    try:
        with open(_ONLINE_MODEL_PATH, "rb") as f:
            blob = pickle.load(f)
        if isinstance(blob, tuple) and len(blob) == 3:
            return blob
    except Exception as exc:
        log.warning("online_model_load_failed", error=str(exc)[:200])
    return (None, None, None)


def _save_online_model(sgd, scaler, feature_list) -> None:
    _ONLINE_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _ONLINE_MODEL_PATH.with_suffix(".pkl.tmp")
    with open(tmp, "wb") as f:
        pickle.dump((sgd, scaler, feature_list), f)
    tmp.replace(_ONLINE_MODEL_PATH)


def _build_features_from_trade(trade: dict) -> tuple[list[float], int] | None:
    """Extract the (features + direction_bit, label) row from a closed trade.

    Returns None if the trade lacks the feature_vector or the necessary
    fields. Mirrors the row construction in `train_from_closed_trades._expand`.
    """
    fv = trade.get("feature_vector")
    if not fv:
        return None
    if isinstance(fv, str):
        try:
            fv = json.loads(fv)
        except Exception:
            return None
    if not isinstance(fv, dict):
        return None

    feats = [fv.get(k) for k in _FEATURES]
    if any(v is None for v in feats):
        return None

    direction = trade.get("direction")
    if direction not in ("long", "short"):
        return None
    direction_bit = 1.0 if direction == "long" else 0.0

    try:
        net_pnl = float(trade.get("net_pnl_usdt") or 0)
    except (TypeError, ValueError):
        return None
    label = 1 if net_pnl > 0 else 0

    row = [float(v) for v in feats] + [direction_bit]
    return (row, label)


def online_update(payload: dict) -> dict:
    """F49 §Component 7 — incremental SGD step on the direction model.

    Called by `ml.online_learner._update_direction_model` after every
    `CH_TRADE_CLOSED` pub/sub event. Maintains an SGDClassifier sidecar
    that supports `partial_fit`, complementing the every-50-trades full
    retrain of the main LogReg/GBC model.

    Payload shape (from online_learner.py):
      {
        "new_trade":    dict,    # full row from `trades` table
        "replay_batch": list,    # F17 replay buffer entries (metadata only,
                                 # no feature_vector — not used here)
        "ewc_lambda":   float,   # ignored — sklearn SGD doesn't expose
                                 # per-parameter Fisher penalty. The L2
                                 # weight decay in SGDClassifier acts as a
                                 # soft anchor analogous to EWC.
        "triggered_at": int,
      }

    Returns dict with status + counters for the audit log.
    """
    new_trade = payload.get("new_trade") or {}
    row = _build_features_from_trade(new_trade)
    if row is None:
        return {"status": "skipped",
                "reason": "no_features_or_direction_or_pnl"}
    x_raw, y = row

    # The sidecar must use the SAME scaler the main model was fit with so
    # its predictions are comparable. If the main model isn't on disk yet,
    # we can't honestly do a scaled SGD step — skip and let the first full
    # retrain bootstrap the scaler.
    main_cached = _load()
    if main_cached is None:
        return {"status": "skipped",
                "reason": "main_model_not_trained_yet"}
    _main_model, main_scaler, main_features = main_cached[0], main_cached[1], main_cached[2]
    expected_features = _FEATURES + [_DIRECTION_BIT_NAME]
    if list(main_features) != list(expected_features):
        return {"status": "skipped",
                "reason": "feature_mismatch",
                "expected": expected_features, "found": list(main_features)}

    import numpy as np
    try:
        x_scaled = main_scaler.transform(np.array([x_raw], dtype=float))
    except Exception as exc:
        return {"status": "scaler_failed", "error": str(exc)[:200]}

    with _online_lock:
        sgd, scaler_stored, _feats_stored = _load_online_model()

        if sgd is None:
            from sklearn.linear_model import SGDClassifier
            sgd = SGDClassifier(
                loss="log_loss",
                learning_rate="constant",
                eta0=_ONLINE_LR,
                alpha=_ONLINE_L2,
                penalty="l2",
                random_state=42,
                warm_start=True,
            )
            # First call MUST specify both classes so partial_fit knows the
            # binary problem even if the first sample is single-class.
            try:
                sgd.partial_fit(x_scaled, np.array([y]),
                                classes=np.array([0, 1]))
            except Exception as exc:
                return {"status": "init_failed", "error": str(exc)[:200]}
        else:
            try:
                sgd.partial_fit(x_scaled, np.array([y]))
            except Exception as exc:
                return {"status": "partial_fit_failed",
                        "error": str(exc)[:200]}

        try:
            _save_online_model(sgd, main_scaler, expected_features)
        except Exception as exc:
            return {"status": "persist_failed", "error": str(exc)[:200]}

        try:
            r = redis_client.get()
            n = r.incr("model:direction_model:online_updates")
            r.set("model:direction_model:online_last_y", int(y))
            r.set("model:direction_model:online_last_at",
                  int(__import__("time").time()))
        except Exception:
            n = None

    return {"status": "updated", "n": n, "label": int(y)}


def reset_online_sidecar() -> dict:
    """Wipe the SGD sidecar — called from the full retrain pipeline so the
    sidecar's accumulated drift doesn't fight the freshly batched model."""
    with _online_lock:
        try:
            if _ONLINE_MODEL_PATH.exists():
                _ONLINE_MODEL_PATH.unlink()
        except Exception as exc:
            return {"status": "delete_failed", "error": str(exc)[:200]}
        try:
            r = redis_client.get()
            r.delete("model:direction_model:online_updates")
            r.delete("model:direction_model:online_last_y")
            r.delete("model:direction_model:online_last_at")
        except Exception:
            pass
    return {"status": "reset"}
