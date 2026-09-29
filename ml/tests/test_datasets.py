import random

from ml.datasets import DOWN, FLAT, UP, ternary_labels
from ml.features import TopOfBook, compute


def _books(n: int, seed: int = 3) -> list[TopOfBook]:
    rng = random.Random(seed)
    bid, out = 1000, []
    for _ in range(n):
        bid += rng.choice((-1, 0, 1))
        out.append(TopOfBook(bid, rng.randint(1, 50), bid + rng.randint(1, 3), rng.randint(1, 50)))
    return out


def test_labels_are_net_of_half_spread() -> None:
    b = [
        TopOfBook(100, 1, 102, 1),  # mid 101, half-spread 1
        TopOfBook(101, 1, 103, 1),  # mid 102: move 1, not above half-spread -> FLAT
        TopOfBook(102, 1, 104, 1),  # mid 103
        TopOfBook(98, 1, 100, 1),  # mid 99
    ]
    assert ternary_labels(b, horizon=1) == [FLAT, FLAT, DOWN, None]
    assert ternary_labels(b, horizon=2) == [UP, DOWN, None, None]
    assert ternary_labels(b, horizon=2, extra_cost=1) == [FLAT, DOWN, None, None]


def test_invalid_books_get_no_label() -> None:
    b = [TopOfBook(100, 1, 102, 1), TopOfBook(0, 0, 0, 0)]
    assert ternary_labels(b, horizon=1) == [None, None]


def test_features_never_use_future_rows() -> None:
    """Truncating the future must not change any past feature value."""
    books = _books(400)
    full = compute(books, tick=1)
    assert sum(f is not None for f in full) > 300
    for cut in (15, 50, 199, 399):
        assert compute(books[:cut], tick=1) == full[:cut]
