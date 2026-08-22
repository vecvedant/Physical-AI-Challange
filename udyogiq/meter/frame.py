"""
One sample from the energy meter.

Every layer above this - features, NILM, health, dispatch - consumes MeterFrame
and nothing else.  That is what lets the same pipeline run against the STM32
Modbus master, a USB-RS485 dongle, or the simulator without knowing which.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(slots=True)
class MeterFrame:
    """
    A single instantaneous reading of a single-phase supply.

    Power sign convention: positive = drawn from the source being measured.
    With the meter at the grid tie, negative active power means export.
    """

    timestamp: float = field(default_factory=time.time)

    # Instantaneous electrical quantities.
    voltage_v: float = 0.0
    current_a: float = 0.0
    active_power_w: float = 0.0
    reactive_power_var: float = 0.0
    apparent_power_va: float = 0.0
    power_factor: float = 1.0
    frequency_hz: float = 50.0

    # Cumulative energy counters straight off the meter, in kWh / kVArh / kVAh.
    # These are monotonic across power cycles, which makes them the trustworthy
    # basis for billing maths - we never integrate power ourselves when a real
    # counter is available.
    import_active_energy_kwh: float = 0.0
    export_active_energy_kwh: float = 0.0
    total_active_energy_kwh: float = 0.0
    total_reactive_energy_kvarh: float = 0.0
    apparent_energy_kvah: float = 0.0

    # Meter's own maximum-demand registers.
    max_demand_active_w: float = 0.0
    max_demand_apparent_va: float = 0.0

    # Provenance.  "sim" frames must never be mistaken for measured data when a
    # report is generated, so the source rides along with the sample.
    source: str = "unknown"
    slave_id: int = 0
    # False when the read failed and this frame is a hold-last-value stand-in.
    valid: bool = True

    # ------------------------------------------------------------------ #
    # Derived quantities
    # ------------------------------------------------------------------ #
    @property
    def is_off(self) -> bool:
        """True when the supply is drawing essentially nothing."""
        return abs(self.active_power_w) < 1.0

    @property
    def distortion_ratio(self) -> float:
        """
        How far apparent power exceeds what active and reactive power explain.

        For a clean sinusoid S^2 = P^2 + Q^2.  Any excess is distortion power,
        which is our cheap stand-in for harmonic content given that a 1 Hz
        Modbus poll can never see a waveform.  Rising distortion on a motor is
        an early hint of a failing drive or degrading windings.
        """
        if self.apparent_power_va <= 1.0:
            return 0.0
        explained = math.hypot(self.active_power_w, self.reactive_power_var)
        excess = max(0.0, self.apparent_power_va ** 2 - explained ** 2)
        return math.sqrt(excess) / self.apparent_power_va

    @property
    def signed_power_factor(self) -> float:
        """PF carrying the sign of reactive power: negative = leading."""
        pf = abs(self.power_factor)
        return -pf if self.reactive_power_var < 0 else pf

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, blob: dict[str, Any]) -> "MeterFrame":
        known = {f for f in cls.__slots__}
        return cls(**{k: v for k, v in blob.items() if k in known})

    def sanity_check(self) -> list[str]:
        """
        Return a list of physical implausibilities in this frame.

        Cheap defence against a mis-decoded register map: if the word order is
        wrong the floats come out as absurd magnitudes rather than as an error,
        and silently training a model on garbage is the worst failure mode
        available to us.
        """
        problems: list[str] = []
        if not (0.0 <= self.voltage_v <= 400.0):
            problems.append(f"voltage {self.voltage_v:.1f} V out of range")
        if not (-200.0 <= self.current_a <= 200.0):
            problems.append(f"current {self.current_a:.1f} A out of range")
        if not (0.0 <= abs(self.power_factor) <= 1.001):
            problems.append(f"power factor {self.power_factor:.3f} out of range")
        if self.frequency_hz and not (40.0 <= self.frequency_hz <= 70.0):
            problems.append(f"frequency {self.frequency_hz:.1f} Hz out of range")
        if self.apparent_power_va < abs(self.active_power_w) - 1.0:
            problems.append("apparent power below active power")
        # V * I should track S within a wide tolerance on a single phase.
        expected_va = self.voltage_v * abs(self.current_a)
        if expected_va > 20.0 and self.apparent_power_va > 20.0:
            ratio = self.apparent_power_va / expected_va
            if not (0.5 <= ratio <= 2.0):
                problems.append(
                    f"S={self.apparent_power_va:.0f} VA inconsistent with "
                    f"V*I={expected_va:.0f} VA"
                )
        return problems
