"""Real forecasting for /ai/predict — replaces the old "slope between the last two
points" placeholder with an actual fitted statistical model:

  - >=10 points: Holt's linear-trend exponential smoothing (statsmodels) — a genuine
    fitted time-series model that accounts for the whole history and trend, not just
    the last two samples.
  - 3-9 points: ordinary least-squares linear regression over all available points.
  - <3 points: flat / naive fallback (not enough data to fit anything meaningful).

Alongside the point forecast, this also returns:
  - a 95% confidence band per horizon step, widening with the forecast horizon (more
    uncertainty further out), derived from the fitted model's in-sample residual
    spread; and
  - a coarse trend label ("rising" / "falling" / "stable") so callers (health scoring,
    the AI Center UI) don't have to re-derive it from the raw points.

No network access and no large model download needed — this is a lightweight
algorithm, not a downloadable weights file, so it always works once numpy /
statsmodels are installed.
"""
from __future__ import annotations

import logging
import statistics

logger = logging.getLogger("aims.forecasting")

# Relative delta (vs the mean of the input series) below which the forecast is
# considered flat rather than trending, so tiny noise doesn't get labeled a trend.
_STABLE_REL_THRESHOLD = 0.02


def _trend_label(values: list[float], preds: list[float]) -> str:
    if not preds:
        return "stable"
    scale = abs(statistics.fmean(values)) + 1e-9
    delta = preds[-1] - preds[0]
    if abs(delta) / scale < _STABLE_REL_THRESHOLD:
        return "stable"
    return "rising" if delta > 0 else "falling"


def _band(preds: list[float], resid_std: float) -> tuple[list[float], list[float]]:
    """95% confidence band, widening as sqrt(step) with the forecast horizon to
    reflect growing uncertainty further into the future."""
    import math

    lower, upper = [], []
    for h, p in enumerate(preds, start=1):
        half_width = 1.96 * resid_std * math.sqrt(h)
        lower.append(p - half_width)
        upper.append(p + half_width)
    return lower, upper


def forecast(values: list[float], horizon: int) -> tuple[list[float], str, dict]:
    """Returns (predicted_values, model_name, meta).

    meta = {"trend": "rising"|"falling"|"stable", "lower": [...], "upper": [...]}
    lower/upper are 95% confidence bounds per horizon step, same length as
    predicted_values.
    """
    n = len(values)
    if n == 0:
        preds = [0.0] * horizon
        return preds, "no-data-fallback", {"trend": "stable", "lower": preds[:], "upper": preds[:]}
    if n == 1:
        preds = [values[0]] * horizon
        return preds, "flat-fallback", {"trend": "stable", "lower": preds[:], "upper": preds[:]}

    if n >= 10:
        try:
            import numpy as np
            from statsmodels.tsa.holtwinters import Holt

            arr = np.asarray(values, dtype=float)
            model = Holt(arr, initialization_method="estimated")
            fit = model.fit(optimized=True)
            preds = [float(v) for v in fit.forecast(horizon)]
            resid_std = float(np.std(fit.fittedvalues - arr)) if len(fit.fittedvalues) else 0.0
            lower, upper = _band(preds, resid_std)
            trend = _trend_label(values, preds)
            return preds, "holt-linear-trend-v1", {"trend": trend, "lower": lower, "upper": upper}
        except Exception:
            logger.exception("Holt forecasting failed, falling back to OLS regression")

    try:
        import numpy as np

        x = np.arange(n, dtype=float)
        y = np.asarray(values, dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        fitted = intercept + slope * x
        resid_std = float(np.std(y - fitted))
        preds = [float(intercept + slope * (n - 1 + h)) for h in range(1, horizon + 1)]
        lower, upper = _band(preds, resid_std)
        trend = _trend_label(values, preds)
        return preds, "ols-linear-regression-v1", {"trend": trend, "lower": lower, "upper": upper}
    except Exception:
        logger.exception("OLS regression failed, falling back to naive slope")

    # last-resort: old naive 2-point slope, only reached if numpy itself is missing
    slope = values[-1] - values[-2]
    last = values[-1]
    preds = [last + slope * h for h in range(1, horizon + 1)]
    trend = _trend_label(values, preds)
    return preds, "naive-slope-fallback", {"trend": trend, "lower": preds[:], "upper": preds[:]}
