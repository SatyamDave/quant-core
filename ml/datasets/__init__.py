"""Point-in-time dataset builder and labels.

Rows are book events in local receipt order: a row's features use only what our recorder had
received by that row's `local_ts`. Labels look forward by design and are the only place that
does; they are never fed back as features. The dataset hash covers the built arrays and every
parameter, so two runs that train on the same hash trained on the same data.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from data.quality import check_deltas
from data.schemas import BUY, L2_DELTA_V1, check_schema
from ml.features import FEATURE_NAMES, FEATURE_VERSION, TopOfBook, compute

DOWN, FLAT, UP = -1, 0, 1


def replay_top_of_book(path: Path) -> list[TopOfBook]:
    """Applies L2 deltas in file (local receipt) order; one top-of-book per delta."""
    table = pq.read_table(path)
    check_schema(table, L2_DELTA_V1)
    report = check_deltas(table)
    if not report.ok:
        raise ValueError(f"refusing {path}: data-quality failures {report}")
    book: tuple[dict[int, int], dict[int, int]] = ({}, {})
    out = []
    empty = TopOfBook(0, 0, 0, 0)
    cols = [table.column(c).to_pylist() for c in ("side", "price", "qty")]
    for side, price, qty in zip(*cols, strict=True):
        levels = book[0] if side == BUY else book[1]
        if qty == 0:
            levels.pop(price, None)
        else:
            levels[price] = qty
        if not book[0] or not book[1]:
            out.append(empty)
            continue
        bid, ask = max(book[0]), min(book[1])
        out.append(TopOfBook(bid, book[0][bid], ask, book[1][ask]))
    return out


def ternary_labels(books: list[TopOfBook], horizon: int, extra_cost: int = 0) -> list[int | None]:
    """Forward mid move over `horizon` events, net of the half-spread at entry.

    UP when mid[t+h] - mid[t] > half_spread[t] + extra_cost, DOWN when it is below the negative
    of that, else FLAT. Everything is compared at 2x in raw integers, so there is no rounding.
    `extra_cost` (raw price units) is where fees and slippage go. Rows without a full horizon
    or with an invalid book have no label (None), never a guessed one.
    """
    labels: list[int | None] = []
    for t, cur in enumerate(books):
        if t + horizon >= len(books) or not cur.valid() or not books[t + horizon].valid():
            labels.append(None)
            continue
        fut = books[t + horizon]
        move2 = (fut.bid_px + fut.ask_px) - (cur.bid_px + cur.ask_px)
        cost2 = (cur.ask_px - cur.bid_px) + 2 * extra_cost
        labels.append(UP if move2 > cost2 else DOWN if move2 < -cost2 else FLAT)
    return labels


@dataclass(frozen=True)
class Dataset:
    X: np.ndarray  # float64, one row per labeled event, columns FEATURE_NAMES
    y: np.ndarray  # int8, DOWN/FLAT/UP
    rows: np.ndarray  # index of each sample in the source event stream
    mid2: np.ndarray  # raw bid+ask (2x mid) at every source event, for evaluation
    spread: np.ndarray  # raw ask-bid at every source event
    params: dict[str, object]
    hash: str


def build(books: list[TopOfBook], tick: int, horizon: int, source: str) -> Dataset:
    feats = compute(books, tick)
    labels = ternary_labels(books, horizon)
    keep = [i for i, (f, y) in enumerate(zip(feats, labels, strict=True)) if f and y is not None]
    X = np.array([feats[i] for i in keep], dtype=np.float64).reshape(-1, len(FEATURE_NAMES))
    y = np.array([labels[i] for i in keep], dtype=np.int8)
    params: dict[str, object] = {
        "source": source,
        "tick": tick,
        "horizon": horizon,
        "feature_version": FEATURE_VERSION,
    }
    h = hashlib.sha256(json.dumps(params, sort_keys=True).encode())
    for arr in (X, y):
        h.update(arr.tobytes())
    return Dataset(
        X=X,
        y=y,
        rows=np.array(keep, dtype=np.int64),
        mid2=np.array([b.bid_px + b.ask_px for b in books], dtype=np.int64),
        spread=np.array([b.ask_px - b.bid_px for b in books], dtype=np.int64),
        params=params,
        hash=h.hexdigest(),
    )
