# ml/datasets

Point-in-time dataset builder (`build`): replays L2 deltas in local receipt order into top-of-book states, computes `ml.features`, and attaches ternary labels. The dataset hash covers the arrays and every build parameter.

Labels (`ternary_labels`): forward mid move over N events, UP above the entry half-spread plus `extra_cost`, DOWN below its negative, otherwise FLAT; rows without a full horizon get no label. Tests in `ml/tests/test_datasets.py`, including a lookahead test that fails if any feature changes when future rows are removed. Reading from `data/catalog` is not wired yet; the builder takes a Parquet path.
