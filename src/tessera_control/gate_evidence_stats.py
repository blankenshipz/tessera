"""Deterministic multi-seed aggregation for gate evidence (stdlib only, no imputation)."""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from typing import Any

STATISTICS_VERSION = 1
BOOTSTRAP_RESAMPLES = 10_000

# Two-sided 97.5% Student t quantiles, df -> t.
_T975: dict[int, float] = {
    1: 12.706205,
    2: 4.302653,
    3: 3.182446,
    4: 2.776445,
    5: 2.570582,
    6: 2.446912,
    7: 2.364624,
    8: 2.306004,
    9: 2.262157,
    10: 2.228139,
    11: 2.200985,
    12: 2.178813,
    13: 2.160369,
    14: 2.144787,
    15: 2.131450,
    16: 2.119905,
    17: 2.109816,
    18: 2.100922,
    19: 2.093024,
    20: 2.085963,
    21: 2.079614,
    22: 2.073873,
    23: 2.068658,
    24: 2.063899,
    25: 2.059539,
    26: 2.055529,
    27: 2.051831,
    28: 2.048407,
    29: 2.045230,
    30: 2.042272,
    40: 2.021075,
    60: 2.000298,
    120: 1.979930,
}
_NORMAL_975 = 1.959964


def t_critical_975(df: int) -> float:
    """Tabulated t quantile; a df between table entries uses the next LOWER tabulated
    df (larger t, hence a wider and conservative interval). Beyond 120, the normal."""
    if df < 1:
        raise ValueError("degrees of freedom must be >= 1")
    if df > 120:
        return _NORMAL_975
    return _T975[max(k for k in _T975 if k <= df)]


def t_interval_95(values: list[float]) -> tuple[float, float]:
    """mean +/- t_{0.975,n-1} * sd/sqrt(n), using the conservative table above."""
    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    half = t_critical_975(n - 1) * sd / math.sqrt(n)
    return mean - half, mean + half


def percentile_bootstrap_95(values: list[float], *, seed_material: str) -> tuple[float, float]:
    """Percentile bootstrap of the mean: B=10000 resamples with replacement from a PRNG
    seeded by sha256(seed_material); the interval is the sorted resample means at index
    floor(0.025*B) and ceil(0.975*B)-1. With few seeds this interval is anti-conservative
    (too narrow); prefer t_interval_95 for small n."""
    n = len(values)
    rng = random.Random(int(hashlib.sha256(seed_material.encode()).hexdigest()[:16], 16))
    means = sorted(
        math.fsum(values[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    return (
        means[math.floor(0.025 * BOOTSTRAP_RESAMPLES)],
        means[math.ceil(0.975 * BOOTSTRAP_RESAMPLES) - 1],
    )


def aggregate(values: list[float], method: str, *, seed_material: str) -> dict[str, Any]:
    """n, mean, sample SD (ddof=1) and a 95% CI. Needs n>=2 finite values; never imputes."""
    if len(values) < 2:
        raise ValueError("an aggregate needs at least two values")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError("all values must be finite numbers")
    values = [float(v) for v in values]
    mean = math.fsum(values) / len(values)
    std = statistics.stdev(values)
    if method == "t_interval_95":
        low, high = t_interval_95(values)
    elif method == "percentile_bootstrap_95":
        low, high = percentile_bootstrap_95(values, seed_material=seed_material)
    else:
        raise ValueError(f"unknown CI method {method!r}")
    if std == 0:
        low = high = mean
    low, high = min(low, mean), max(high, mean)
    return {
        "n": len(values),
        "mean": mean,
        "std": std,
        "ci_low": low,
        "ci_high": high,
        "method": method,
        "statistics_version": STATISTICS_VERSION,
    }
