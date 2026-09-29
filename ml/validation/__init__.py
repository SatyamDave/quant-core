"""Walk-forward splits, deflated Sharpe ratio, and probability of backtest overfitting."""

import itertools
import math
from statistics import NormalDist

import numpy as np

EULER_GAMMA = 0.5772156649015329


def purged_walk_forward(
    n: int, n_splits: int, horizon: int, embargo: int, min_train: int
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window walk-forward folds over `n` time-ordered samples.

    The tail after `min_train` is cut into `n_splits` consecutive test blocks. Training uses only
    earlier samples, purged of any whose label window (`horizon` samples forward) reaches the
    test block, then embargoed by a further `embargo` samples (López de Prado 2018, Advances in
    Financial Machine Learning, ch. 7). So max(train) + horizon + embargo < min(test).
    """
    if min_train <= 0 or n_splits <= 0 or n - min_train < n_splits:
        raise ValueError("not enough samples for the requested folds")
    bounds = np.linspace(min_train, n, n_splits + 1).astype(int)
    folds = []
    for t0, t1 in itertools.pairwise(bounds):
        train_end = t0 - horizon - embargo
        if train_end <= 0:
            raise ValueError("purge and embargo leave no training data")
        folds.append((np.arange(train_end), np.arange(t0, t1)))
    return folds


def sharpe(returns: np.ndarray) -> float:
    """Per-period Sharpe ratio (not annualized); 0 when there is no variation."""
    if len(returns) < 2:
        return 0.0
    sd = float(np.std(returns, ddof=1))
    return float(np.mean(returns)) / sd if sd > 0 else 0.0


def deflated_sharpe(
    sr: float, n_obs: int, n_trials: int, trial_sr_var: float, skew: float, kurt: float
) -> float:
    """Deflated Sharpe ratio: probability the true Sharpe exceeds the best of `n_trials` by luck.

    Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Correcting for Selection Bias,
    Backtest Overfitting and Non-Normality", Journal of Portfolio Management 40(5), eqs. 1-2.
    `sr` and `trial_sr_var` are per-period (not annualized); `kurt` is raw kurtosis (normal = 3).
    `n_trials` must be the true count from the registry, failures included.
    """
    if n_trials < 1 or n_obs < 2:
        raise ValueError("need at least one trial and two observations")
    z = NormalDist()
    sr0 = 0.0
    if n_trials > 1:
        sr0 = math.sqrt(trial_sr_var) * (
            (1 - EULER_GAMMA) * z.inv_cdf(1 - 1 / n_trials)
            + EULER_GAMMA * z.inv_cdf(1 - 1 / (n_trials * math.e))
        )
    denom = math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr**2)
    return z.cdf((sr - sr0) * math.sqrt(n_obs - 1) / denom)


def pbo(perf: np.ndarray, n_blocks: int) -> float:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation (CSCV).

    Bailey, Borwein, López de Prado & Zhu (2017), "The Probability of Backtest Overfitting",
    Journal of Computational Finance 20(4). `perf` is T x N: per-period returns of N configs on
    the same T periods. Rows are cut into `n_blocks` (even) blocks; for every half/half split,
    the config with the best in-sample Sharpe is ranked out-of-sample. PBO is the share of
    splits where that config lands at or below the out-of-sample median (logit <= 0).
    """
    t, n = perf.shape
    if n_blocks % 2 or n_blocks < 2 or t < n_blocks or n < 2:
        raise ValueError("need an even block count, >= 2 configs, and a row per block")
    blocks = np.array_split(np.arange(t), n_blocks)
    overfit = total = 0
    for is_ids in itertools.combinations(range(n_blocks), n_blocks // 2):
        is_rows = np.concatenate([blocks[i] for i in is_ids])
        oos_rows = np.concatenate([blocks[i] for i in range(n_blocks) if i not in is_ids])
        is_sr = [sharpe(perf[is_rows, j]) for j in range(n)]
        oos_sr = np.array([sharpe(perf[oos_rows, j]) for j in range(n)])
        best = int(np.argmax(is_sr))
        rank = int(np.sum(oos_sr <= oos_sr[best]))  # 1 = worst, n = best
        omega = rank / (n + 1)
        overfit += math.log(omega / (1 - omega)) <= 0
        total += 1
    return overfit / total
