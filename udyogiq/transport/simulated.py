"""
Transport backed by the synthetic plant in ``sim.plant``.

This is the default so that a fresh clone runs end to end with no hardware,
which matters more than it sounds: it means the models, the dashboard and the
dispatch optimiser were all developed and tested against a known ground truth
before the RS485 was ever wired, and a bench problem on demo day cannot take
the whole project down with it.

Simulated frames carry ``source="sim"`` all the way into the historian so that
nothing measured and nothing invented ever get mixed up in a report.
"""

from __future__ import annotations

import time

from ..config import CONFIG
from ..meter.frame import MeterFrame
from .base import InverterAdapter, MeterTransport


class SimulatedMeter(MeterTransport):
    """Reads from a :class:`sim.plant.Plant` instead of a serial port."""

    name = "sim"

    def __init__(self, plant=None, *, time_scale: float = 1.0,
                 start_epoch: float | None = None) -> None:
        super().__init__()
        from sim.plant import Plant  # imported lazily; sim/ is not a runtime dep

        self.plant = plant if plant is not None else Plant(
            meter_position="grid_tie")
        # Compressing time lets a training run cover days in seconds while the
        # live dashboard still runs at wall-clock rate.
        self.time_scale = time_scale
        self._wall_start = time.time()
        self._sim_start = start_epoch if start_epoch is not None else self._wall_start
        self._virtual_t: float | None = None
        self._latest: dict[str, float] = {}

    def open(self) -> None:
        # Re-anchor both clocks to now. The object may have been constructed
        # long before acquisition starts - a warm-up replay takes the better
        # part of a minute - and leaving the simulated origin back at
        # construction time makes every live frame arrive already stale, which
        # the health endpoint correctly refuses to call healthy.
        self._wall_start = time.time()
        self._sim_start = self._wall_start

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    def advance_to(self, virtual_t: float) -> None:
        """Pin the simulation clock, for deterministic offline dataset runs."""
        self._virtual_t = virtual_t

    def _now(self) -> float:
        if self._virtual_t is not None:
            return self._virtual_t
        elapsed = (time.time() - self._wall_start) * self.time_scale
        return self._sim_start + elapsed

    def _read(self) -> MeterFrame:
        sample = self.plant.sample(self._now())
        self._latest = sample
        payload = {k: v for k, v in sample.items() if not k.startswith("_")}
        frame = MeterFrame(**payload, source="sim", slave_id=CONFIG.meter.slave_id)
        # The plant reports the grid tie, which can legitimately go negative
        # when solar exceeds load; sanity_check() understands that.
        return frame

    # ------------------------------------------------------------------ #
    @property
    def ground_truth(self) -> dict[str, float]:
        """Per-appliance power for the last sample.  Evaluation only."""
        return dict(self._latest.get("_truth", {}))


class SimulatedInverter(InverterAdapter):
    """Perfect telemetry from the simulated array and battery."""

    name = "sim"
    controllable = True

    def __init__(self, meter: SimulatedMeter) -> None:
        self._meter = meter

    def read(self) -> dict[str, float]:
        s = self._meter._latest
        return {
            "solar_w": s.get("_solar_w", 0.0),
            "battery_w": s.get("_battery_w", 0.0),
            "soc": s.get("_soc", 0.0),
            "load_w": s.get("_load_w", 0.0),
            "capacity_wh": self._meter.plant.battery.capacity_wh,
        }

    def command_battery(self, watts: float) -> bool:
        self._meter.plant.set_battery_command(watts)
        return True
