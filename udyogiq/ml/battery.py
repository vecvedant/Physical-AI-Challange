"""
Battery model and, more importantly, what a cycle actually costs.

The degradation term is the whole point
---------------------------------------
An optimiser told only to minimise the electricity bill will cycle a battery as
hard as the rate limits allow, because in that objective storage is free.  It
is not.  Every kilowatt-hour pushed through consumes a fraction of a finite
cycle life, and that fraction has a price:

    cost per kWh cycled = installed cost per kWh
                          / (cycle life * usable depth * round-trip efficiency)

On representative Indian LFP economics - roughly Rs 15,000/kWh installed, 4,000
cycles, 75% usable depth, 90% round trip - that is about Rs 5.5 per kWh of
throughput.  Which means arbitrage only makes money if the time-of-day spread
beats Rs 5.5/kWh, and on many tariffs it simply does not.

Leaving this term out would produce a system that looks brilliant on a monthly
bill and quietly destroys an asset worth more than the savings.  Including it
means the optimiser sometimes correctly decides to do nothing, which is a
feature that is very easy to mistake for a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import CONFIG


@dataclass
class BatterySpec:
    """Physical and economic parameters of the installed storage."""

    capacity_wh: float = 5000.0
    max_charge_w: float = 1500.0
    max_discharge_w: float = 1500.0
    soc_floor: float = 0.20
    soc_ceiling: float = 0.95
    round_trip_efficiency: float = 0.90

    capex_inr_per_kwh: float = 15000.0
    cycle_life: float = 4000.0

    @classmethod
    def from_config(cls) -> "BatterySpec":
        s = CONFIG.site
        return cls(
            capacity_wh=s.battery_capacity_wh,
            max_charge_w=s.battery_max_charge_w,
            max_discharge_w=s.battery_max_discharge_w,
            soc_floor=s.battery_soc_floor,
            soc_ceiling=s.battery_soc_ceiling,
            round_trip_efficiency=s.battery_round_trip_efficiency,
            capex_inr_per_kwh=s.battery_capex_inr_per_kwh,
            cycle_life=s.battery_cycle_life,
        )

    # ------------------------------------------------------------------ #
    @property
    def usable_depth(self) -> float:
        return max(0.05, self.soc_ceiling - self.soc_floor)

    @property
    def usable_wh(self) -> float:
        return self.capacity_wh * self.usable_depth

    @property
    def one_way_efficiency(self) -> float:
        """Charge and discharge each take roughly half the round-trip loss."""
        return max(0.5, self.round_trip_efficiency) ** 0.5

    @property
    def degradation_inr_per_kwh(self) -> float:
        """
        What one kWh of throughput costs in consumed battery life.

        This is the number the optimiser weighs every arbitrage decision
        against, and it is why the correct answer is sometimes 'leave it alone'.
        """
        lifetime_kwh = (self.capacity_wh / 1000.0) * self.usable_depth * self.cycle_life
        if lifetime_kwh <= 0:
            return 0.0
        total_capex = self.capex_inr_per_kwh * (self.capacity_wh / 1000.0)
        return total_capex / (lifetime_kwh * max(self.round_trip_efficiency, 0.1))

    def breakeven_spread_inr_per_kwh(self) -> float:
        """
        Minimum tariff spread that makes a charge/discharge cycle worth doing.

        Buying low and selling high through a battery loses energy to the round
        trip as well as consuming cycle life, so the spread has to cover both.
        """
        eff = max(self.round_trip_efficiency, 0.1)
        return self.degradation_inr_per_kwh + (1.0 / eff - 1.0) * CONFIG.sustainability.tariff_inr_per_kwh

    def describe(self) -> dict[str, Any]:
        return {
            "capacity_wh": self.capacity_wh,
            "usable_wh": round(self.usable_wh, 1),
            "max_charge_w": self.max_charge_w,
            "max_discharge_w": self.max_discharge_w,
            "soc_floor": self.soc_floor,
            "soc_ceiling": self.soc_ceiling,
            "round_trip_efficiency": self.round_trip_efficiency,
            "degradation_inr_per_kwh": round(self.degradation_inr_per_kwh, 2),
            "breakeven_spread_inr_per_kwh": round(self.breakeven_spread_inr_per_kwh(), 2),
        }


class BatteryState:
    """Tracks state of charge and throughput as commands are applied."""

    def __init__(self, spec: BatterySpec | None = None, soc: float = 0.5) -> None:
        self.spec = spec or BatterySpec.from_config()
        self.soc = min(max(soc, 0.0), 1.0)
        self.throughput_wh = 0.0
        self.cycles_equivalent = 0.0

    # ------------------------------------------------------------------ #
    def limits(self, dt_s: float) -> tuple[float, float]:
        """
        (max_charge_w, max_discharge_w) actually available right now.

        Bounded by the rate limits and by how much room or charge is left, so a
        near-full battery reports a small charge limit rather than accepting a
        command it cannot honour.
        """
        s = self.spec
        hours = max(dt_s / 3600.0, 1e-9)

        headroom_wh = max(0.0, (s.soc_ceiling - self.soc) * s.capacity_wh)
        charge_w = min(s.max_charge_w, headroom_wh / hours / s.one_way_efficiency)

        available_wh = max(0.0, (self.soc - s.soc_floor) * s.capacity_wh)
        discharge_w = min(s.max_discharge_w, available_wh * s.one_way_efficiency / hours)

        return max(0.0, charge_w), max(0.0, discharge_w)

    def apply(self, command_w: float, dt_s: float) -> float:
        """
        Apply a power command. Positive discharges, negative charges.

        Returns what actually happened, which differs from the command whenever
        a limit binds - the dispatch loop must be robust to that rather than
        assuming its plan was executed.
        """
        s = self.spec
        charge_limit, discharge_limit = self.limits(dt_s)
        hours = dt_s / 3600.0

        if command_w >= 0:
            actual = min(command_w, discharge_limit)
            drawn_wh = actual * hours / s.one_way_efficiency
            self.soc -= drawn_wh / s.capacity_wh
        else:
            actual = -min(-command_w, charge_limit)
            stored_wh = -actual * hours * s.one_way_efficiency
            self.soc += stored_wh / s.capacity_wh

        self.soc = min(max(self.soc, 0.0), 1.0)
        moved = abs(actual) * hours
        self.throughput_wh += moved
        self.cycles_equivalent = self.throughput_wh / max(s.usable_wh, 1.0)
        return actual

    # ------------------------------------------------------------------ #
    @property
    def energy_wh(self) -> float:
        return self.soc * self.spec.capacity_wh

    @property
    def usable_energy_wh(self) -> float:
        return max(0.0, (self.soc - self.spec.soc_floor) * self.spec.capacity_wh)

    def degradation_cost(self, kwh_moved: float) -> float:
        return abs(kwh_moved) * self.spec.degradation_inr_per_kwh

    def status(self) -> dict[str, Any]:
        return {
            "soc": round(self.soc, 4),
            "soc_pct": round(self.soc * 100.0, 1),
            "energy_wh": round(self.energy_wh, 1),
            "usable_energy_wh": round(self.usable_energy_wh, 1),
            "throughput_kwh": round(self.throughput_wh / 1000.0, 2),
            "cycles_equivalent": round(self.cycles_equivalent, 2),
            "spec": self.spec.describe(),
        }
