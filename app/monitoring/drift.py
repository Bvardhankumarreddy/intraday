"""Drift statistics (Phase 15), numpy only.

Kinds (never conflated):
- DATA_DRIFT: an INPUT distribution changed. Two-sample Kolmogorov-Smirnov
  test, recent window vs the earlier reference, with at least
  ``data_drift_min_samples`` values on each side.
- PERFORMANCE_DRIFT: measured results deteriorated. The bootstrap CI of
  recent net expectancy per trade lies entirely below the reference CI,
  with at least ``min_trades_for_drift`` closed trades on each side.
- POSSIBLE_CONCEPT_DRIFT: performance drifted while no tested input
  distribution changed. It is reported as a hypothesis only: a changed
  input-to-outcome relationship is never "proven" by this.
- INSUFFICIENT_EVIDENCE: too few observations to say anything.
- NO_DRIFT_DETECTED: enough data and no criterion met. This is lack of
  evidence of drift, not proof of stability.

A losing streak alone is never drift. It is reported for information only.
"""

from __future__ import annotations

import math

import numpy as np


def ks_2samp(a, b) -> tuple[float, float]:
    """(D statistic, asymptotic two-sided p-value)."""
    a, b = np.sort(np.asarray(a, float)), np.sort(np.asarray(b, float))
    n, m = len(a), len(b)
    allv = np.concatenate([a, b])
    cdf_a = np.searchsorted(a, allv, side="right") / n
    cdf_b = np.searchsorted(b, allv, side="right") / m
    d = float(np.max(np.abs(cdf_a - cdf_b)))
    ne = n * m / (n + m)
    lam = (math.sqrt(ne) + 0.12 + 0.11 / math.sqrt(ne)) * d
    if lam < 0.3:  # the Kolmogorov series does not converge near 0; Q(lambda) ~ 1 there
        return round(d, 4), 1.0
    p = 2 * sum((-1) ** (k - 1) * math.exp(-2 * k * k * lam * lam) for k in range(1, 101))
    return round(d, 4), round(min(max(p, 0.0), 1.0), 6)


def boot_ci(values, confidence: float, seed: int = 15, resamples: int = 2000):
    arr = np.asarray(values, float)
    if len(arr) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    means = arr[rng.integers(0, len(arr), (resamples, len(arr)))].mean(axis=1)
    lo, hi = np.quantile(means, [(1 - confidence) / 2, 1 - (1 - confidence) / 2])
    return round(float(lo), 4), round(float(hi), 4)


def performance_drift(reference: list[float], recent: list[float], cfg) -> dict:
    n_ref, n_rec = len(reference), len(recent)
    base = {"reference_trades": n_ref, "recent_trades": n_rec,
            "min_trades": cfg.min_trades_for_drift}
    if min(n_ref, n_rec) < cfg.min_trades_for_drift:
        return {**base, "kind": "INSUFFICIENT_EVIDENCE",
                "reason": "too few closed trades in one window to judge drift"}
    r_lo, r_hi = boot_ci(reference, cfg.drift_confidence)
    c_lo, c_hi = boot_ci(recent, cfg.drift_confidence)
    out = {**base, "reference_expectancy": round(float(np.mean(reference)), 4),
           "recent_expectancy": round(float(np.mean(recent)), 4),
           "reference_ci": [r_lo, r_hi], "recent_ci": [c_lo, c_hi]}
    if c_hi < r_lo:
        return {**out, "kind": "PERFORMANCE_DRIFT",
                "reason": "recent net expectancy CI lies entirely below the reference CI"}
    return {**out, "kind": "NO_DRIFT_DETECTED",
            "reason": "the intervals overlap: no evidence of deterioration (not proof of "
                      "stability)"}


def data_drift(reference: dict[str, list], recent: dict[str, list], cfg) -> list[dict]:
    rows = []
    for feat in sorted(set(reference) & set(recent)):
        a = [x for x in reference[feat] if x is not None]
        b = [x for x in recent[feat] if x is not None]
        if min(len(a), len(b)) < cfg.data_drift_min_samples:
            rows.append({"feature": feat, "kind": "INSUFFICIENT_EVIDENCE",
                         "n": [len(a), len(b)]})
            continue
        d, p = ks_2samp(a, b)
        rows.append({"feature": feat, "n": [len(a), len(b)], "ks_d": d, "p_value": p,
                     "kind": "DATA_DRIFT" if p < cfg.data_drift_p_value else "NO_DRIFT_DETECTED"})
    return rows


def classify(perf: dict, data: list[dict]) -> str:
    if perf["kind"] == "PERFORMANCE_DRIFT":
        tested = [d for d in data if d["kind"] != "INSUFFICIENT_EVIDENCE"]
        if tested and not any(d["kind"] == "DATA_DRIFT" for d in tested):
            return "PERFORMANCE_DRIFT; POSSIBLE_CONCEPT_DRIFT (hypothesis only)"
        return "PERFORMANCE_DRIFT" + ("; with DATA_DRIFT in inputs (cause not established)"
                                      if any(d["kind"] == "DATA_DRIFT" for d in data) else "")
    if any(d["kind"] == "DATA_DRIFT" for d in data):
        return "DATA_DRIFT (inputs changed; no performance deterioration shown)"
    return perf["kind"]


def longest_losing_streak(nets: list[float]) -> int:
    best = run = 0
    for x in nets:
        run = run + 1 if x < 0 else 0
        best = max(best, run)
    return best
