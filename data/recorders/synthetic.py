"""Deterministic synthetic market day in the recorder's Parquet format. No network access.

This stands in for a recorded day until real recorders exist. The book is a random walk of
five price levels a side; thin best queues are more likely to be depleted, which gives queue
imbalance a small, known predictive effect. It is not a model of any venue.

    python -m data.recorders.synthetic --out data/raw/synthetic --seed 7 --events 50000
"""

import argparse
import datetime as dt
import random
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from data.schemas import BUY, L2_DELTA_V1, SCALE, SELL, TRADE_V1

TICK = SCALE // 10  # 0.1 price units
LOT = SCALE // 100  # 0.01 quantity units
DEPTH = 5
INSTRUMENT = 1


def generate(seed: int, n_events: int, date: str) -> tuple[pa.Table, pa.Table]:
    rng = random.Random(seed)
    start = dt.datetime.fromisoformat(date).replace(tzinfo=dt.UTC)
    exch = int(start.timestamp()) * 1_000_000_000
    local = exch
    mid = 50_000 * SCALE
    book: dict[int, dict[int, int]] = {BUY: {}, SELL: {}}
    deltas: dict[str, list[int]] = {f: [] for f in L2_DELTA_V1.names}
    trades: dict[str, list[int]] = {f: [] for f in TRADE_V1.names}
    seq = 0

    def best(side: int) -> int:
        return max(book[BUY]) if side == BUY else min(book[SELL])

    def worst(side: int) -> int:
        return min(book[BUY]) if side == BUY else max(book[SELL])

    def emit(side: int, price: int, qty: int) -> None:
        nonlocal seq
        seq += 1
        if qty == 0:
            del book[side][price]
        else:
            book[side][price] = qty
        for name, value in zip(
            L2_DELTA_V1.names, (INSTRUMENT, side, price, qty, seq, exch, local), strict=True
        ):
            deltas[name].append(value)

    # The day opens with the initial levels as deltas, so a replay starts from an empty book.
    for k in range(DEPTH):
        emit(BUY, mid - (k + 1) * TICK, rng.randint(1, 100) * LOT)
        emit(SELL, mid + k * TICK, rng.randint(1, 100) * LOT)
    while seq < n_events:
        exch += 1 + int(rng.expovariate(1 / 2_000_000))
        # One socket delivers in order, so local receipt time never goes backwards.
        local = max(local, exch + 200_000 + int(rng.expovariate(1 / 300_000)))
        r = rng.random()
        side = rng.choice((BUY, SELL))
        spread = best(SELL) - best(BUY)
        if r < 0.6:
            emit(side, rng.choice(sorted(book[side])), rng.randint(1, 100) * LOT)
        elif spread > TICK and (r < 0.75 or spread >= 3 * TICK):
            # Liquidity refills a wide spread, which keeps it near one or two ticks.
            emit(side, best(side) + (TICK if side == BUY else -TICK), rng.randint(1, 100) * LOT)
            if len(book[side]) > 2 * DEPTH:
                emit(side, worst(side), 0)
        elif r < 0.9:
            # Deplete the thinner best queue more often.
            qb, qa = book[BUY][best(BUY)], book[SELL][best(SELL)]
            side = BUY if rng.random() < qa / (qa + qb) else SELL
            if len(book[side]) == 1:
                continue
            emit(side, best(side), 0)
            if len(book[side]) < DEPTH:
                emit(
                    side, worst(side) + (-TICK if side == BUY else TICK), rng.randint(1, 100) * LOT
                )
        else:
            hit = SELL if side == BUY else BUY
            price, avail = best(hit), book[hit][best(hit)]
            qty = rng.randint(1, avail // LOT) * LOT
            if qty == avail and len(book[hit]) == 1:
                qty -= LOT
            if qty == 0:
                continue
            for name, value in zip(
                TRADE_V1.names, (INSTRUMENT, side, price, qty, exch, local), strict=True
            ):
                trades[name].append(value)
            emit(hit, price, avail - qty)
    return (
        pa.Table.from_pydict(deltas, schema=L2_DELTA_V1),
        pa.Table.from_pydict(trades, schema=TRADE_V1),
    )


def write_day(out: Path, seed: int, n_events: int, date: str) -> Path:
    """Writes `<out>/<date>/l2_delta.parquet` and `trades.parquet`; returns the day directory."""
    day = out / date
    day.mkdir(parents=True, exist_ok=True)
    deltas, trades = generate(seed, n_events, date)
    pq.write_table(deltas, day / "l2_delta.parquet")
    pq.write_table(trades, day / "trades.parquet")
    return day


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("data/raw/synthetic"))
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--events", type=int, default=50_000)
    p.add_argument("--date", default="2026-01-05")
    a = p.parse_args()
    print(write_day(a.out, a.seed, a.events, a.date))
