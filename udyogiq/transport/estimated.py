"""
Source-side estimation for sites whose inverter tells us nothing.

Most small installations run an inverter with no accessible telemetry, so we
cannot measure solar generation and battery flow independently.  We can still
recover them, because the three quantities are not independent - they are tied
by an energy balance at the point of common coupling:

    P_load = P_solar + P_battery + P_grid

The meter gives us one term exactly.  The solar model gives us a second with
useful accuracy.  Battery flow is then whatever is left over, and state of
charge follows by integration.

This is dead reckoning, and it drifts.  Two things keep it honest:

  * SoC is re-anchored whenever the battery is obviously idle at a rail - a
    long spell of zero flow at high SoC means full, and the inverter's own
    low-voltage cutout means empty.
  * Every estimate is reported with a confidence, and the dashboard and the
    report both show estimated series in a different style from measured ones.
    Nothing here is ever presented as a measurement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .base import InverterAdapter


@dataclass
class _SocAnchor:
    """A moment we are confident about the true state of charge."""

    t: float
    soc: float
    reason: str


class EstimatedInverter(InverterAdapter):
    """Infers solar, battery flow and SoC from the meter plus a solar model."""

    name = "estimated"
    controllable = False

    def __init__(self, *, capacity_wh: float = 5000.0,
                 initial_soc: float = 0.5,
                 solar_model=None,
                 charge_efficiency: float = 0.95,
                 discharge_efficiency: float = 0.95,
                 meter_position: str = "grid_tie") -> None:
        self.capacity_wh = capacity_wh
        self.soc = initial_soc
        self.solar_model = solar_model
        self.charge_efficiency = charge_efficiency
        self.discharge_efficiency = discharge_efficiency
        self.meter_position = meter_position

        self._last_t: float | None = None
        self._last_grid_w = 0.0
        self._solar_w = 0.0
        self._battery_w = 0.0
        self._anchors: list[_SocAnchor] = []
        # Grows while we dead reckon, resets at each anchor.
        self._drift_hours = 0.0

    # ------------------------------------------------------------------ #
    def update(self, *, grid_w: float, timestamp: float | None = None,
               solar_w: float | None = None) -> None:
        """
        Fold in one meter sample.

        ``grid_w`` is signed: positive imports, negative exports.
        """
        now = timestamp if timestamp is not None else time.time()
        dt_h = 0.0 if self._last_t is None else max(0.0, (now - self._last_t)) / 3600.0
        self._last_t = now
        self._last_grid_w = grid_w

        if solar_w is None and self.solar_model is not None:
            solar_w = self.solar_model.instantaneous(now)
        self._solar_w = max(0.0, solar_w or 0.0)

        # With the meter at the grid tie we know import/export but not the load,
        # so battery flow can only be separated from load by assuming the
        # battery is the term that moves when nothing else explains a change.
        # We take the conservative reading: attribute nothing to the battery
        # unless the site is exporting while the array cannot account for it,
        # or importing far less than the array is producing.
        if self.meter_position == "grid_tie":
            residual = -grid_w - self._solar_w   # what a battery would have to supply
            self._battery_w = residual if abs(residual) > 50.0 else 0.0
        else:
            self._battery_w = 0.0

        if dt_h > 0.0:
            if self._battery_w >= 0:
                drawn = self._battery_w * dt_h / max(self.discharge_efficiency, 1e-3)
                self.soc -= drawn / self.capacity_wh
            else:
                stored = -self._battery_w * dt_h * self.charge_efficiency
                self.soc += stored / self.capacity_wh
            self.soc = min(1.0, max(0.0, self.soc))
            self._drift_hours += dt_h

        self._maybe_anchor(now)

    def _maybe_anchor(self, now: float) -> None:
        """Re-peg SoC when the physics tells us where we actually are."""
        # Sustained zero battery flow while the sun is strong: the battery has
        # stopped accepting charge, so it is full.
        if (self._solar_w > 0.3 * max(self.capacity_wh / 4.0, 1.0)
                and abs(self._battery_w) < 30.0 and self.soc > 0.75):
            self._set_anchor(now, 1.0, "absorbed: no charge accepted in strong sun")
        # Battery refuses to discharge into a real load: it has hit the floor.
        elif self._last_grid_w > 200.0 and abs(self._battery_w) < 30.0 and self.soc < 0.30:
            self._set_anchor(now, 0.20, "cutout: no discharge under load")

    def _set_anchor(self, t: float, soc: float, reason: str) -> None:
        if self._anchors and (t - self._anchors[-1].t) < 600.0:
            return
        self.soc = soc
        self._drift_hours = 0.0
        self._anchors.append(_SocAnchor(t, soc, reason))
        del self._anchors[:-20]

    # ------------------------------------------------------------------ #
    @property
    def confidence(self) -> float:
        """
        0..1, decaying with time since the last anchor.

        Twelve hours of unanchored dead reckoning is treated as no better than
        a guess, which is roughly where the arithmetic actually lands.
        """
        if self.solar_model is None:
            return 0.25
        return max(0.05, min(1.0, 1.0 - self._drift_hours / 12.0))

    def read(self) -> dict[str, float]:
        load_w = self._last_grid_w + self._solar_w + self._battery_w
        return {
            "solar_w": self._solar_w,
            "battery_w": self._battery_w,
            "soc": self.soc,
            "load_w": max(0.0, load_w),
            "grid_w": self._last_grid_w,
            "capacity_wh": self.capacity_wh,
            "estimated": 1.0,
            "confidence": self.confidence,
        }
