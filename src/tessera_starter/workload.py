"""One preregistered variant per invocation; immutable splits, no downloads."""

import csv
import hashlib
import io
import math
import platform
import time

from tessera_control.dataset_snapshots import canonical_bytes, load_dataset_snapshot

VARIANTS = ("constant", "linear", "half-slope", "zero-slope")


def code_digest():
    from pathlib import Path

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def run(root, manifest_digest, variant, *, cancelled=lambda: False, seconds=120):
    if variant not in VARIANTS or type(seconds) is not int or not 1 <= seconds <= 120:
        raise ValueError("Unsupported variant or deadline")
    start = time.monotonic()

    def check():
        if cancelled():
            raise InterruptedError("cancelled")
        if time.monotonic() - start >= seconds:
            raise TimeoutError("deadline")

    check()
    manifest, content = load_dataset_snapshot(root, expected_manifest_digest=manifest_digest)
    check()
    columns = manifest.columns
    feature = next(i for i, c in enumerate(columns) if c.role == "feature")
    target = next(i for i, c in enumerate(columns) if c.role == "target")
    split = next(i for i, c in enumerate(columns) if c.role == "split")
    groups = {s: [] for s in ("train", "validation", "test")}
    rows = csv.reader(io.StringIO(content.decode("utf-8"), newline=""))
    next(rows)
    for row in rows:
        check()
        x, y = float(row[feature]), float(row[target])
        if max(abs(x), abs(y)) > 1e100:
            raise ValueError("Numeric magnitude exceeds starter arithmetic bound")
        groups[row[split]].append((x, y))
    train = groups["train"]
    mx = math.fsum(x for x, _ in train) / len(train)
    my = math.fsum(y for _, y in train) / len(train)
    variance = math.fsum((x - mx) ** 2 for x, _ in train)
    slope = 0.0
    fallback = None
    if variant in {"linear", "half-slope"}:
        if variance <= 1e-24:
            fallback = "singular-training-feature:constant-train-mean"
        else:
            slope = math.fsum((x - mx) * (y - my) for x, y in train) / variance
            if variant == "half-slope":
                slope *= 0.5
    metrics = {}
    for label, values in groups.items():
        check()
        errors = [my + slope * (x - mx) - y for x, y in values]
        metrics[label] = {
            "mse": math.fsum(e * e for e in errors) / len(errors),
            "mae": math.fsum(abs(e) for e in errors) / len(errors),
            "rows": len(values),
        }
    result = {
        "version": "tessera-tabular-result-v1",
        "status": "succeeded",
        "variant": variant,
        "parameters": {"seed": 1729, "feature": columns[feature].name},
        "parameter_digest": hashlib.sha256(
            canonical_bytes({"seed": 1729, "feature": columns[feature].name, "variant": variant})
        ).hexdigest(),
        "model": {"train_x_mean": mx, "train_y_mean": my, "slope": slope, "fallback": fallback},
        "metrics": metrics,
        "provenance": {
            "dataset_manifest_digest": manifest_digest,
            "data_sha256": manifest.data_sha256,
            "code_sha256": code_digest(),
            "splits": [s.model_dump() for s in manifest.splits],
            "source": manifest.source.model_dump(),
            "python": platform.python_version(),
            "platform": platform.system(),
        },
        "scientific_validation": "not-established",
        "disposition": "quarantine-human-review-required",
    }
    canonical_bytes(result)  # fail closed on nonfinite arithmetic
    check()
    return result
