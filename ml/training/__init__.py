"""Baseline trainer: L2-regularized multinomial logistic regression with a fixed seed."""

from dataclasses import asdict, dataclass

import numpy as np
from sklearn.linear_model import LogisticRegression


@dataclass(frozen=True)
class TrainConfig:
    C: float = 1.0
    max_iter: int = 1000
    seed: int = 7

    def as_dict(self) -> dict[str, object]:
        return {"model": "logistic_regression", "solver": "lbfgs", **asdict(self)}


def train(X: np.ndarray, y: np.ndarray, cfg: TrainConfig) -> LogisticRegression:
    return LogisticRegression(C=cfg.C, max_iter=cfg.max_iter, random_state=cfg.seed).fit(X, y)
