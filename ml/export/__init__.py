"""Exports a linear model as the JSON artifact `engine/crates/inference` loads.

The engine loads an artifact only when its sha256 equals the hash in the registry record, so
the artifact bytes are the unit of identity: `export` returns them together with their hash.
Floats are written with Python's shortest round-trip repr, which Rust parses back exactly.
"""

import hashlib
import json

from sklearn.linear_model import LogisticRegression

from ml.datasets import DOWN, FLAT, UP
from ml.features import FEATURE_NAMES, FEATURE_VERSION

FORMAT = "qc-linear-v1"


def export(model: LogisticRegression) -> tuple[bytes, str]:
    classes = [int(c) for c in model.classes_]
    if classes != [DOWN, FLAT, UP]:
        raise ValueError(f"expected classes down/flat/up, got {classes}")
    artifact = {
        "format": FORMAT,
        "feature_version": FEATURE_VERSION,
        "features": list(FEATURE_NAMES),
        "classes": ["down", "flat", "up"],
        "coef": [[float(w) for w in row] for row in model.coef_],
        "intercept": [float(b) for b in model.intercept_],
    }
    data = (json.dumps(artifact, indent=1) + "\n").encode()
    return data, hashlib.sha256(data).hexdigest()
