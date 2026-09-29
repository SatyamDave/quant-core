import numpy as np
import pytest

from ml.validation import deflated_sharpe, pbo, purged_walk_forward, sharpe


def test_no_train_index_within_horizon_plus_embargo_of_test() -> None:
    horizon, embargo = 20, 7
    folds = purged_walk_forward(1000, n_splits=5, horizon=horizon, embargo=embargo, min_train=300)
    assert len(folds) == 5
    covered = np.concatenate([test for _, test in folds])
    assert np.array_equal(covered, np.arange(300, 1000))
    for train, test in folds:
        assert train.max() + horizon + embargo < test.min()
        assert not np.intersect1d(train, test).size


def test_splitter_refuses_when_purge_eats_training_data() -> None:
    with pytest.raises(ValueError, match="no training data"):
        purged_walk_forward(100, n_splits=2, horizon=30, embargo=30, min_train=50)


def test_deflated_sharpe_matches_paper_example() -> None:
    # Bailey & López de Prado (2014), section 5: annualized SR 2.5 over 1250 daily
    # observations, 100 trials with annualized SR variance 0.5, skew -3, kurtosis 10.
    dsr = deflated_sharpe(
        sr=2.5 / np.sqrt(250), n_obs=1250, n_trials=100, trial_sr_var=0.5 / 250, skew=-3, kurt=10
    )
    assert dsr == pytest.approx(0.9004, abs=5e-4)


def test_one_trial_deflated_sharpe_is_probabilistic_sharpe_against_zero() -> None:
    assert deflated_sharpe(0.0, 100, 1, 0.0, 0.0, 3.0) == pytest.approx(0.5)


def test_pbo_is_zero_when_one_config_dominates_everywhere() -> None:
    rng = np.random.default_rng(0)
    perf = rng.normal(0, 1, size=(160, 4))
    perf[:, 2] += 5.0
    assert pbo(perf, n_blocks=8) == 0.0


def test_pbo_is_one_when_in_sample_winner_always_loses_out_of_sample() -> None:
    # Config A wins only in the first half, B only in the second; every CSCV split
    # with 2 blocks picks the in-sample winner that is worst out-of-sample.
    wobble = np.tile([0.1, -0.1], 10)
    a = np.concatenate([1 + wobble, -1 + wobble])
    b = np.concatenate([-1 + wobble, 1 + wobble])
    assert pbo(np.column_stack([a, b]), n_blocks=2) == 1.0


def test_sharpe_of_constant_series_is_zero() -> None:
    assert sharpe(np.ones(10)) == 0.0
