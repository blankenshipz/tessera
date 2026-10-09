"""Reproducible public synthetic example, not a scientific benchmark."""

import random


def example():
    rng = random.Random(1729)
    lines = ["id,x,target,split"]
    for i in range(120):
        x = rng.randrange(-100, 101) / 10
        y = 2 * x + 3 + rng.randrange(-5, 6) / 10
        split = "train" if i < 80 else "validation" if i < 100 else "test"
        lines.append(f"row-{i},{x:.1f},{y:.1f},{split}")
    declaration = {
        "version": "tessera-dataset-snapshot-v1",
        "profile": "bounded-tabular-regression-v1",
        "columns": [
            {"name": "id", "type": "string", "role": "id"},
            {"name": "x", "type": "number", "role": "feature"},
            {"name": "target", "type": "number", "role": "target"},
            {"name": "split", "type": "split", "role": "split"},
        ],
        "source": {"kind": "synthetic", "reference": "tessera-starter-seed-1729", "license": "MIT"},
    }
    return ("\n".join(lines) + "\n").encode(), declaration
