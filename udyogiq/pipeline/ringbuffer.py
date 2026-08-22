"""
Fixed-size circular history of meter samples.

The node runs for weeks on a board with finite RAM, so history has to be
bounded and allocation has to stop after startup.  Samples land in
pre-allocated numpy columns rather than a list of objects: the feature
extractor asks for "the last 300 seconds of active power" hundreds of times a
minute, and that has to be a slice, not a rebuild.
"""

from __future__ import annotations

import numpy as np

from ..meter.frame import MeterFrame

#: Columns kept as parallel arrays.  Order is fixed and referenced by name.
COLUMNS: tuple[str, ...] = (
    "timestamp",
    "active_power_w",
    "reactive_power_var",
    "apparent_power_va",
    "voltage_v",
    "current_a",
    "power_factor",
    "frequency_hz",
)


class RingBuffer:
    """Circular buffer of meter samples with cheap windowed access."""

    def __init__(self, capacity: int = 3600) -> None:
        if capacity < 8:
            raise ValueError("capacity must be at least 8 samples")
        self.capacity = capacity
        self._data = np.zeros((len(COLUMNS), capacity), dtype=np.float64)
        self._valid = np.zeros(capacity, dtype=bool)
        self._idx = 0            # next write position
        self._count = 0          # samples written, saturating at capacity
        self._col = {name: i for i, name in enumerate(COLUMNS)}

    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return self._count

    @property
    def full(self) -> bool:
        return self._count >= self.capacity

    def append(self, frame: MeterFrame) -> None:
        i = self._idx
        for name, row in self._col.items():
            self._data[row, i] = getattr(frame, name)
        self._valid[i] = frame.valid
        self._idx = (i + 1) % self.capacity
        self._count = min(self._count + 1, self.capacity)

    # ------------------------------------------------------------------ #
    def _ordered(self, n: int | None = None) -> np.ndarray:
        """
        Return the buffer unrolled oldest-to-newest.

        A wrapped buffer needs one concatenate; an unwrapped one is a plain
        view, which is the common case once you ask for a window much shorter
        than capacity.
        """
        count = self._count
        if count == 0:
            return np.zeros((len(COLUMNS), 0), dtype=np.float64)

        if count < self.capacity:
            block = self._data[:, :count]
            valid = self._valid[:count]
        else:
            block = np.concatenate(
                (self._data[:, self._idx:], self._data[:, :self._idx]), axis=1)
            valid = np.concatenate(
                (self._valid[self._idx:], self._valid[:self._idx]))

        if n is not None and n < block.shape[1]:
            block = block[:, -n:]
            valid = valid[-n:]

        self._last_valid = valid
        return block

    def column(self, name: str, n: int | None = None,
               valid_only: bool = True) -> np.ndarray:
        """Most recent ``n`` values of one column, oldest first."""
        block = self._ordered(n)
        if block.shape[1] == 0:
            return np.zeros(0, dtype=np.float64)
        series = block[self._col[name]]
        if valid_only:
            return series[self._last_valid]
        return series

    def window(self, seconds: float, name: str,
               valid_only: bool = True) -> np.ndarray:
        """
        Values from the last ``seconds`` of wall time.

        Selecting by time rather than by sample count matters because the poll
        rate is not guaranteed: a busy Modbus bus or a stalled MCU stretches the
        interval, and a fixed sample count would silently widen the window.
        """
        block = self._ordered()
        if block.shape[1] == 0:
            return np.zeros(0, dtype=np.float64)
        ts = block[self._col["timestamp"]]
        mask = ts >= (ts[-1] - seconds)
        if valid_only:
            mask &= self._last_valid
        return block[self._col[name]][mask]

    def latest(self) -> dict[str, float] | None:
        if self._count == 0:
            return None
        i = (self._idx - 1) % self.capacity
        return {name: float(self._data[row, i]) for name, row in self._col.items()}

    def span_seconds(self) -> float:
        """Wall time covered by the buffer right now."""
        ts = self.column("timestamp", valid_only=False)
        return float(ts[-1] - ts[0]) if ts.size >= 2 else 0.0

    def as_arrays(self) -> dict[str, np.ndarray]:
        """Whole buffer, oldest first, for model training."""
        block = self._ordered()
        return {name: block[row].copy() for name, row in self._col.items()}
