from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import log1p
import re
from statistics import mean, median, pstdev

FEATURES = (
    "log_requests", "error_4xx_rate", "error_5xx_rate", "latency_p50", "latency_p90", "latency_p95",
    "latency_p99", "latency_stddev", "latency_skew", "traffic_growth", "read_write_ratio",
    "avg_request_size", "avg_response_size", "rolling_5m_error_rate", "rolling_15m_error_rate",
    "rolling_5m_p95", "rolling_15m_p95",
)
MIN_LABEL_EVENTS = 5


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(int((len(values) - 1) * q), len(values) - 1)]


def make_feature_windows(events) -> list[dict]:
    buckets = defaultdict(list)
    for event in events:
        stamp = event.timestamp.astimezone(timezone.utc).replace(second=0, microsecond=0)
        buckets[stamp].append(event)
    ordered = sorted(buckets)
    basic = {}
    for stamp in ordered:
        rows = buckets[stamp]
        latencies = [row.latency_ms for row in rows]
        reads = sum(row.method.upper() in {"GET", "HEAD", "OPTIONS"} for row in rows)
        writes = len(rows) - reads
        basic[stamp] = {
            "request_count": len(rows),
            "error_4xx_rate": sum(400 <= row.status_code < 500 for row in rows) / len(rows),
            "error_5xx_rate": sum(row.status_code >= 500 for row in rows) / len(rows),
            "p50": percentile(latencies, .50), "p90": percentile(latencies, .90), "p95": percentile(latencies, .95),
            "p99": percentile(latencies, .99), "latency_stddev": pstdev(latencies) if len(latencies) > 1 else 0,
            "latency_skew": ((sum((v - mean(latencies)) ** 3 for v in latencies) / len(latencies)) / (pstdev(latencies) ** 3)) if len(latencies) > 2 and pstdev(latencies) > 0 else 0,
            "read_write_ratio": reads / max(writes, 1),
            "avg_request_size": mean([row.request_size or 0 for row in rows]),
            "avg_response_size": mean([row.response_size or 0 for row in rows]),
        }
    windows = []
    for stamp in ordered:
        current = basic[stamp]
        prior = [basic[t] for t in ordered if stamp - timedelta(minutes=5) <= t < stamp]
        prior_traffic = mean([item["request_count"] for item in prior]) if prior else current["request_count"]
        recent = [basic[t] for t in ordered if stamp - timedelta(minutes=4) <= t <= stamp]
        rolling_15m = [basic[t] for t in ordered if stamp - timedelta(minutes=14) <= t <= stamp]
        latency_values = [item["p95"] for item in recent]
        feature_values = {
            "log_requests": log1p(current["request_count"]),
            "error_4xx_rate": current["error_4xx_rate"], "error_5xx_rate": current["error_5xx_rate"],
            "latency_p50": current["p50"], "latency_p90": current["p90"], "latency_p95": current["p95"], "latency_p99": current["p99"],
            "latency_stddev": current["latency_stddev"],
            "latency_skew": current["latency_skew"],
            "traffic_growth": (current["request_count"] - prior_traffic) / max(prior_traffic, 1),
            "read_write_ratio": current["read_write_ratio"],
            "avg_request_size": current["avg_request_size"], "avg_response_size": current["avg_response_size"],
            "rolling_5m_error_rate": mean([item["error_5xx_rate"] for item in recent]),
            "rolling_15m_error_rate": mean([item["error_5xx_rate"] for item in rolling_15m]),
            "rolling_5m_p95": mean([item["p95"] for item in recent]),
            "rolling_15m_p95": mean([item["p95"] for item in rolling_15m]),
        }
        windows.append({"window_start": stamp, "features": feature_values, "request_count": current["request_count"], "latency_trend": _slope(latency_values)})
    return windows


def _slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    x_mean = (len(values) - 1) / 2
    y_mean = mean(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    return sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)) / denominator if denominator else 0.0


def future_failure_label(window_start: datetime, events) -> int | None:
    future = [event for event in events if window_start < event.timestamp <= window_start + timedelta(minutes=10)]
    if len(future) < MIN_LABEL_EVENTS:
        return None
    errors = sum(event.status_code >= 500 for event in future) / len(future)
    p95 = percentile([event.latency_ms for event in future], .95)
    return int(errors >= .05 or p95 >= 1500)


def future_latency_label(window_start: datetime, events) -> float | None:
    future = [event.latency_ms for event in events if window_start < event.timestamp <= window_start + timedelta(minutes=10)]
    return percentile(future, .95) if len(future) >= MIN_LABEL_EVENTS else None


def predict_failure(features: dict, artifact: dict) -> float:
    import xgboost as xgb
    import numpy as np
    model = xgb.XGBClassifier()
    model.load_model(bytearray(artifact["classifier_json"], "utf-8"))
    values = np.array([[features.get(name, 0) for name in artifact["features"]]])
    return float(model.predict_proba(values)[0, 1])


def predict_latency(features: dict, artifact: dict) -> float | None:
    if not artifact.get("latency_json") or not artifact.get("latency_enabled"):
        return None
    import xgboost as xgb
    import numpy as np
    model = xgb.XGBRegressor()
    model.load_model(bytearray(artifact["latency_json"], "utf-8"))
    values = np.array([[features.get(name, 0) for name in artifact["features"]]])
    return max(0.0, float(model.predict(values)[0]))


def anomaly_scores(windows: list[dict]) -> list[float]:
    """Isolation Forest scores, with deterministic baseline fallback for short histories."""
    matrix = [[row["features"].get(key, 0) for key in FEATURES] for row in windows]
    if len(matrix) >= 30:
        from sklearn.ensemble import IsolationForest
        detector = IsolationForest(n_estimators=100, contamination="auto", random_state=17)
        detector.fit(matrix)
        return [float(-score) for score in detector.score_samples(matrix)]
    scores = []
    for idx, row in enumerate(windows):
        baseline = matrix[max(0, idx - 14):idx]
        if len(baseline) < 3:
            scores.append(0.0)
            continue
        deviations = []
        for column, value in enumerate(matrix[idx]):
            values = [sample[column] for sample in baseline]
            center = median(values)
            mad = median([abs(sample - center) for sample in values])
            deviations.append(abs(value - center) / max(1.4826 * mad, abs(center) * .05, 1.0))
        scores.append(min(1.0, max(deviations, default=0) / 10))
    return scores


def retrieve_runbooks(query: str, runbooks: list[dict], limit: int = 3) -> list[dict]:
    tokenize = lambda text: set(re.findall(r"[a-z0-9_]+", text.lower()))
    terms = {word for word in tokenize(query) if len(word) > 2}
    ranked = []
    for runbook in runbooks:
        words = tokenize(runbook["title"] + " " + runbook["body"])
        overlap = terms & words
        score = len(overlap) / max(len(terms), 1)
        if score:
            ranked.append({"id": runbook["id"], "title": runbook["title"], "excerpt": runbook["body"][:600], "score": round(score, 3)})
    return sorted(ranked, key=lambda row: row["score"], reverse=True)[:limit]


def explain_model(features: dict, artifact: dict) -> list[dict]:
    import numpy as np
    import shap
    import xgboost as xgb
    model = xgb.XGBClassifier()
    model.load_model(bytearray(artifact["classifier_json"], "utf-8"))
    values = np.array([[features.get(name, 0) for name in artifact["features"]]])
    explanation = shap.TreeExplainer(model)(values)
    contributions = explanation.values
    if contributions.ndim == 3:
        contributions = contributions[0, :, 1]
    else:
        contributions = contributions[0]
    items = [{"feature": name, "contribution_log_odds": round(float(value), 4)}
             for name, value in zip(artifact["features"], contributions)]
    return sorted(items, key=lambda item: abs(item["contribution_log_odds"]), reverse=True)[:5]


def assess_training_readiness(windows: list[dict], labels: list[int]) -> dict:
    usable = [(window, label) for window, label in zip(windows, labels) if label is not None]
    usable.sort(key=lambda pair: pair[0]["window_start"])
    positives = sum(label == 1 for _, label in usable)
    negatives = len(usable) - positives
    result = {"eligible": False, "reason": "need_more_labeled_windows",
              "labeled_windows": len(usable), "positive_windows": positives,
              "negative_windows": negatives, "minimum_labeled_windows": 30,
              "training_windows": 0, "holdout_windows": 0}
    if len(usable) < 30:
        return result
    if positives == 0 or negatives == 0:
        result["reason"] = "need_healthy_and_failure_examples"
        return result
    split = max(1, int(len(usable) * .8))
    holdout_start = usable[split][0]["window_start"]
    train = [item for item in usable[:split] if item[0]["window_start"] < holdout_start - timedelta(minutes=10)]
    holdout = usable[split:]
    result.update({"training_windows": len(train), "holdout_windows": len(holdout)})
    if len(train) < 20 or not holdout:
        result["reason"] = "need_more_time_history"
        return result
    if len({label for _, label in train}) < 2:
        result["reason"] = "need_healthy_and_failure_examples_in_training_period"
        return result
    result.update({"eligible": True, "reason": "ready"})
    return result


def train_failure_model(windows: list[dict], labels: list[int], latency_targets: list[float | None] | None = None):
    """Time-ordered XGBoost classification and optional p95 regression."""
    readiness = assess_training_readiness(windows, labels)
    if not readiness["eligible"]:
        return None, {"status": "insufficient_data", **readiness}
    import numpy as np
    import xgboost as xgb

    latency_targets = latency_targets or [None] * len(windows)
    usable = [(window, label, latency_targets[idx])
              for idx, (window, label) in enumerate(zip(windows, labels)) if label is not None]
    usable.sort(key=lambda pair: pair[0]["window_start"])
    split = max(1, int(len(usable) * .8))
    holdout_start = usable[split][0]["window_start"]
    train = [item for item in usable[:split] if item[0]["window_start"] < holdout_start - timedelta(minutes=10)]
    holdout = usable[split:]
    if len(train) < 20 or not holdout:
        return None, {"status": "insufficient_data", "reason": "need_more_time_history",
                      "labeled_windows": len(usable), "training_windows": len(train), "holdout_windows": len(holdout)}
    x_train = [[row["features"].get(key, 0) for key in FEATURES] for row, _, _ in train]
    y_train = [label for _, label, _ in train]
    if len(set(y_train)) < 2:
        return None, {"status": "insufficient_data", "reason": "need_healthy_and_failure_examples_in_training_period",
                      "labeled_windows": len(usable), "positive_windows": sum(label == 1 for _, label, _ in usable)}
    positives = sum(y_train)
    classifier = xgb.XGBClassifier(n_estimators=100, max_depth=3, learning_rate=.04, subsample=.85,
                                   colsample_bytree=.85, reg_lambda=2, scale_pos_weight=(len(y_train)-positives)/max(positives, 1),
                                   eval_metric="logloss", random_state=17, n_jobs=2)
    classifier.fit(np.array(x_train), np.array(y_train))
    classifier_json = bytes(classifier.get_booster().save_raw(raw_format="json")).decode("utf-8")
    latency_rows = [(row, latency) for row, _, latency in train if latency is not None]
    latency_model = None
    if len(latency_rows) >= 30:
        latency_model = xgb.XGBRegressor(n_estimators=100, max_depth=3, learning_rate=.04, subsample=.85,
                                         colsample_bytree=.85, reg_lambda=2, random_state=17, n_jobs=2)
        latency_model.fit(np.array([[row["features"].get(key, 0) for key in FEATURES] for row, _ in latency_rows]),
                          np.array([latency for _, latency in latency_rows]))
    artifact = {"features": list(FEATURES), "classifier_json": classifier_json,
                "latency_json": bytes(latency_model.get_booster().save_raw(raw_format="json")).decode("utf-8") if latency_model else None,
                "latency_enabled": False}
    metrics = {"status": "trained", "algorithm": "XGBoost", "labeled_windows": len(usable), "positive_windows": sum(label == 1 for _, label, _ in usable), "training_windows": len(train), "holdout_windows": len(holdout), "latency_model": "trained" if latency_model else "insufficient_data"}
    if holdout:
        from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
        x_holdout = np.array([[row["features"].get(key, 0) for key in FEATURES] for row, _, _ in holdout])
        y_holdout = [label for _, label, _ in holdout]
        probabilities = classifier.predict_proba(x_holdout)[:, 1]
        metrics["holdout_brier"] = float(brier_score_loss(y_holdout, probabilities))
        metrics["holdout_pr_auc"] = float(average_precision_score(y_holdout, probabilities))
        metrics["holdout_roc_auc"] = float(roc_auc_score(y_holdout, probabilities)) if len(set(y_holdout)) > 1 else None
        prevalence = sum(y_holdout) / len(y_holdout)
        baseline_probabilities = np.full(len(y_holdout), sum(y_train) / len(y_train))
        baseline_brier = float(brier_score_loss(y_holdout, baseline_probabilities))
        metrics["holdout_positive_rate"] = round(prevalence, 4)
        metrics["baseline_brier"] = baseline_brier
        metrics["brier_improvement"] = round(baseline_brier - metrics["holdout_brier"], 6)
        holdout_positive = sum(y_holdout)
        holdout_negative = len(y_holdout) - holdout_positive
        enough_validation = len(y_holdout) >= 20 and holdout_positive >= 5 and holdout_negative >= 5
        metrics["quality_status"] = "needs_more_validation_data"
        if enough_validation:
            metrics["quality_status"] = "validated" if metrics["brier_improvement"] > 0 and metrics["holdout_pr_auc"] > prevalence and (metrics["holdout_roc_auc"] is None or metrics["holdout_roc_auc"] >= .5) else "below_baseline"
        regression_holdout = [(row, latency) for row, _, latency in holdout if latency is not None]
        if latency_model and regression_holdout:
            from sklearn.metrics import mean_absolute_error
            x_latency = np.array([[row["features"].get(key, 0) for key in FEATURES] for row, _ in regression_holdout])
            y_latency = [latency for _, latency in regression_holdout]
            model_mae = float(mean_absolute_error(y_latency, latency_model.predict(x_latency)))
            baseline = float(np.mean([latency for _, _, latency in train if latency is not None]))
            baseline_mae = float(mean_absolute_error(y_latency, np.full(len(y_latency), baseline)))
            metrics["latency_holdout_mae_ms"] = round(model_mae, 3)
            metrics["latency_baseline_mae_ms"] = round(baseline_mae, 3)
            latency_validated = model_mae < baseline_mae and len(regression_holdout) >= 20
            metrics["latency_quality_status"] = "validated" if latency_validated else "needs_more_validation_data_or_below_baseline"
            artifact["latency_enabled"] = latency_validated
    return artifact, metrics
