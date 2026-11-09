"""A real, trained anomaly-detection model backing up the diagnosis rule pack.

The diagnosis rule pack (cores/ai/diagnosis_rules.json) is keyword matching, not a
model — and turning it into a genuine supervised classifier would need labeled fault
data (symptom -> confirmed root cause) that this system doesn't collect anywhere yet.

What CAN be trained without labels: an IsolationForest fitted directly on a device's
own measurement history for a component, which learns what "normal" looks like for
THAT device and flags statistically unusual recent readings. This is a real,
persisted (joblib) scikit-learn model — not a config file — and its verdict is
blended into the rule pack's diagnosis/confidence rather than replacing it.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("aims.anomaly")

MIN_TRAINING_POINTS = 20


def _model_path(device_id: int, component_name: str) -> Path:
    from app.core.config import settings

    safe = "".join(c if c.isalnum() else "_" for c in (component_name or "unknown"))
    d = settings.models_dir_path / "diagnosis"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"device{device_id}_{safe}.joblib"


def is_trained(device_id: int, component_name: str) -> bool:
    return _model_path(device_id, component_name).is_file()


def train(device_id: int, component_name: str, values: list[float]) -> Optional[dict]:
    """Fit and persist an IsolationForest on this device+component's own history."""
    if len(values) < MIN_TRAINING_POINTS:
        return None
    try:
        import numpy as np
        import joblib
        from sklearn.ensemble import IsolationForest

        X = np.asarray(values, dtype=float).reshape(-1, 1)
        model = IsolationForest(n_estimators=150, contamination="auto", random_state=42)
        model.fit(X)
        path = _model_path(device_id, component_name)
        joblib.dump(model, path)
        logger.info("Trained anomaly model for device=%s component=%s on %d points", device_id, component_name, len(values))
        return {"trained_on": len(values), "path": str(path)}
    except Exception:
        logger.exception("Failed to train anomaly model for device=%s component=%s", device_id, component_name)
        return None


def score(device_id: int, component_name: str, recent_values: list[float]) -> Optional[dict]:
    """Returns anomaly severity for recent_values against the trained model for this
    device+component, or None if no model has been trained yet / scoring failed."""
    path = _model_path(device_id, component_name)
    if not path.is_file() or not recent_values:
        return None
    try:
        import numpy as np
        import joblib

        model = joblib.load(path)
        X = np.asarray(recent_values, dtype=float).reshape(-1, 1)
        preds = model.predict(X)  # -1 = anomaly, 1 = normal
        anomaly_ratio = float((preds == -1).mean())
        return {
            "anomaly_ratio": round(anomaly_ratio, 3),
            "is_anomaly": anomaly_ratio > 0.3,
            "n_points": len(recent_values),
        }
    except Exception:
        logger.exception("Failed to score anomaly for device=%s component=%s", device_id, component_name)
        return None
