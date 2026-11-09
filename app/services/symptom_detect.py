"""Infer a diagnosis symptom (Drift / Noise) directly from recent measurement values,
for when the caller wants an AI diagnosis but doesn't already know what to call the
problem. This lets /ai/diagnose work with just device_id + component_name — the
`symptom` field becomes an optional override rather than a required manual input.

Purely statistical (no external model / no network): fits a straight line through the
recent window and looks at what's left over. The line's slope over the window spots a
directional shift ("drift"); the scatter of points *around that line* (not around the
flat mean) spots elevated noise. Returns None when the window is too short or nothing
stands out, so callers fall back to the rule pack's generic default entry instead of
guessing.

Using the residual scatter (post-detrend) instead of the raw stdev-around-the-mean
matters: a cleanly rising or falling series naturally has a large spread around its
*flat* mean simply because it moved — that spread is the drift itself, not noise, and
scoring it against NOISE_REL_THRESHOLD would misclassify an obvious drift as noise.
"""
from __future__ import annotations

import statistics
from typing import Optional

MIN_POINTS = 6
NOISE_REL_THRESHOLD = 0.12  # residual stdev (post-detrend) / |mean| beyond this looks like elevated noise
DRIFT_REL_THRESHOLD = 0.06  # total change over the window / |mean| beyond this looks like drift


def _fit_line(values: list[float]) -> tuple[float, float]:
    """Least-squares slope/intercept of `values` against index 0..n-1."""
    n = len(values)
    xs = range(n)
    xbar = (n - 1) / 2.0
    ybar = statistics.fmean(values)
    num = sum((x - xbar) * (y - ybar) for x, y in zip(xs, values))
    den = sum((x - xbar) ** 2 for x in xs)
    slope = num / den if den else 0.0
    intercept = ybar - slope * xbar
    return slope, intercept


def detect_symptom(values: list[float]) -> tuple[Optional[str], dict]:
    """Returns (symptom_text, info). symptom_text is a lowercase phrase built from the
    same keywords the rule pack (diagnosis_rules.json) already matches on — e.g.
    "drift high" / "drift low" / "noise unstable" — or None if no pattern is detected.
    """
    n = len(values)
    if n < MIN_POINTS:
        return None, {"reason": "not_enough_points", "n_points": n}

    mean = statistics.fmean(values)
    scale = abs(mean) + 1e-9

    slope, intercept = _fit_line(values)
    residuals = [y - (intercept + slope * x) for x, y in enumerate(values)]
    resid_stdev = statistics.pstdev(residuals) if n > 1 else 0.0
    rel_noise = resid_stdev / scale

    total_shift = slope * (n - 1)  # trend's net movement across the whole window
    rel_shift = total_shift / scale

    info = {
        "n_points": n,
        "mean": round(mean, 6),
        "resid_stdev": round(resid_stdev, 6),
        "rel_shift": round(rel_shift, 4),
        "rel_noise": round(rel_noise, 4),
    }

    # Noise (scatter left over *after* removing the trend) takes priority over a mild
    # shift, since a noisy signal makes any apparent drift unreliable to call anyway.
    if rel_noise > NOISE_REL_THRESHOLD:
        return "noise unstable spike", info
    if rel_shift > DRIFT_REL_THRESHOLD:
        return "drift high_reading high", info
    if rel_shift < -DRIFT_REL_THRESHOLD:
        return "drift low_reading low", info
    return None, info
