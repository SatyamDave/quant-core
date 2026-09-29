"""Feature set `tob-v1`: the one definition shared with `engine/crates/inference/src/features.rs`.

Inputs are top-of-book states as raw fixed-point integers (price and qty at SCALE 1e8). Integer
arithmetic is exact; floats appear only in the final ratios, computed in the same order as the
Rust code; the parity tests hold both sides within 1e-9 relative. Changing any formula or
`OFI_WINDOW` means a new `FEATURE_VERSION`, a regenerated parity fixture, and a new model.

Order-flow imbalance follows Cont, Kukanov & Stoikov (2014), "The Price Impact of Order Book
Events", Journal of Financial Econometrics 12(1).
"""

from collections import deque
from dataclasses import dataclass

FEATURE_VERSION = "tob-v1"
FEATURE_NAMES = ("ofi_norm", "queue_imbalance", "microprice_dev_ticks", "spread_ticks")
OFI_WINDOW = 10


@dataclass(frozen=True)
class TopOfBook:
    bid_px: int
    bid_qty: int
    ask_px: int
    ask_qty: int

    def valid(self) -> bool:
        return self.bid_qty > 0 and self.ask_qty > 0 and self.bid_px < self.ask_px


def ofi_contribution(prev: TopOfBook, cur: TopOfBook) -> int:
    """One event's order-flow imbalance in raw qty units (Cont et al. 2014, eq. 2)."""
    e = 0
    if cur.bid_px >= prev.bid_px:
        e += cur.bid_qty
    if cur.bid_px <= prev.bid_px:
        e -= prev.bid_qty
    if cur.ask_px <= prev.ask_px:
        e -= cur.ask_qty
    if cur.ask_px >= prev.ask_px:
        e += prev.ask_qty
    return e


class FeatureState:
    """Streaming features; `update` sees only the current and past books, never future ones."""

    def __init__(self, tick: int) -> None:
        self.tick = tick
        self.prev: TopOfBook | None = None
        self.ofi: deque[int] = deque(maxlen=OFI_WINDOW)

    def update(self, tob: TopOfBook) -> tuple[float, float, float, float] | None:
        """Returns features for this book, or None while warming up or on an invalid book."""
        if not tob.valid():
            self.prev = None
            self.ofi.clear()
            return None
        if self.prev is not None:
            self.ofi.append(ofi_contribution(self.prev, tob))
        self.prev = tob
        if len(self.ofi) < OFI_WINDOW:
            return None
        depth = float(tob.bid_qty + tob.ask_qty)
        ofi_norm = float(sum(self.ofi)) / depth
        qi = float(tob.bid_qty - tob.ask_qty) / depth
        spread_ticks = float(tob.ask_px - tob.bid_px) / float(self.tick)
        microprice_dev = 0.5 * spread_ticks * qi
        return (ofi_norm, qi, microprice_dev, spread_ticks)


def compute(books: list[TopOfBook], tick: int) -> list[tuple[float, float, float, float] | None]:
    state = FeatureState(tick)
    return [state.update(b) for b in books]
