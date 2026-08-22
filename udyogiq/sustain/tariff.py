"""
Time-of-day tariffs, demand charges and power-factor penalties.

This is the file that turns kilowatt-hours into rupees, and it is the reason
the dispatch optimiser has anything to optimise.  If energy cost the same at
every hour there would be no point owning a battery.

The Indian regulatory hook
--------------------------
The Electricity (Rights of Consumers) Amendment Rules introduced mandatory
time-of-day tariffs for commercial and industrial consumers: energy drawn
during the designated solar window is billed at a discount, and energy drawn at
peak at a premium.  The exact windows and multipliers are set by each state
commission, so they are configuration here, not constants - the defaults below
are representative rather than authoritative and must be replaced with the
consumer's actual tariff schedule before any savings figure is quoted.

Why maximum demand deserves its own term
----------------------------------------
Small industrial consumers are billed not only for the energy they use but for
the highest average demand they hit in any 15- or 30-minute window during the
month, priced per kVA.  A single careless quarter-hour where three motors start
together can set a charge that is then paid every month.  This is often a large
fraction of an MSME's bill and it responds to exactly the kind of second-by-
second control an edge device can provide, which is why the optimiser treats it
as a hard objective rather than an afterthought.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config import CONFIG


@dataclass
class TariffWindow:
    """A contiguous stretch of the day billed at one multiplier."""

    name: str
    start_hour: float
    end_hour: float
    multiplier: float

    def covers(self, hour: float) -> bool:
        if self.start_hour <= self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour   # wraps midnight


#: Representative Indian C&I time-of-day structure. Replace with the actual
#: schedule from the consumer's tariff order before quoting any saving.
DEFAULT_WINDOWS: tuple[TariffWindow, ...] = (
    TariffWindow("Night off-peak", 22.0, 6.0, 0.90),
    TariffWindow("Morning peak", 6.0, 9.0, 1.15),
    TariffWindow("Solar window", 9.0, 17.0, 0.80),
    TariffWindow("Evening peak", 17.0, 22.0, 1.20),
)


@dataclass
class TariffSchedule:
    """Energy price by time of day, plus the standing charges."""

    base_rate_inr_per_kwh: float = 8.0
    windows: tuple[TariffWindow, ...] = DEFAULT_WINDOWS

    demand_charge_inr_per_kva: float = 350.0
    sanctioned_demand_kva: float = 5.0
    #: Billing window for maximum demand, in minutes. Indian utilities
    #: overwhelmingly use 15 or 30.
    demand_window_minutes: int = 30
    #: Multiplier applied to demand beyond the sanctioned load.
    excess_demand_penalty: float = 2.0

    pf_penalty_threshold: float = 0.90
    pf_penalty_pct_per_point: float = 1.0

    #: What the utility pays for exported energy. Almost always well below the
    #: import rate, which is precisely why storing surplus beats exporting it.
    export_rate_inr_per_kwh: float = 3.0

    @classmethod
    def from_config(cls) -> "TariffSchedule":
        s = CONFIG.sustainability
        return cls(
            base_rate_inr_per_kwh=s.tariff_inr_per_kwh,
            demand_charge_inr_per_kva=s.demand_charge_inr_per_kva,
            sanctioned_demand_kva=s.sanctioned_demand_kva,
            pf_penalty_threshold=s.pf_penalty_threshold,
            pf_penalty_pct_per_point=s.pf_penalty_pct_per_point,
        )

    # ------------------------------------------------------------------ #
    def window_at(self, timestamp: float) -> TariffWindow:
        lt = time.localtime(timestamp)
        hour = lt.tm_hour + lt.tm_min / 60.0
        for w in self.windows:
            if w.covers(hour):
                return w
        return TariffWindow("Standard", 0.0, 24.0, 1.0)

    def rate(self, timestamp: float) -> float:
        """Import price in INR/kWh at that moment."""
        return self.base_rate_inr_per_kwh * self.window_at(timestamp).multiplier

    def rate_profile(self, start_t: float, blocks: int,
                     block_minutes: int = 15) -> np.ndarray:
        """Import price for each of the next N blocks - the optimiser's input."""
        return np.array(
            [self.rate(start_t + i * block_minutes * 60) for i in range(blocks)],
            dtype=np.float64)

    def export_profile(self, start_t: float, blocks: int,
                       block_minutes: int = 15) -> np.ndarray:
        return np.full(blocks, self.export_rate_inr_per_kwh, dtype=np.float64)

    # ------------------------------------------------------------------ #
    def spread(self, start_t: float, hours: int = 24) -> dict[str, float]:
        """
        Cheapest and dearest rate over the horizon.

        The optimiser cannot beat battery degradation cost unless this spread
        is wide enough, so it is worth showing an operator directly: it is the
        single number that decides whether arbitrage is worth doing at all.
        """
        rates = self.rate_profile(start_t, hours * 4)
        return {
            "min_inr_per_kwh": float(rates.min()),
            "max_inr_per_kwh": float(rates.max()),
            "spread_inr_per_kwh": float(rates.max() - rates.min()),
        }

    # ------------------------------------------------------------------ #
    def energy_cost(self, kwh: float, timestamp: float) -> float:
        """Cost of importing (or credit for exporting) energy at one moment."""
        if kwh >= 0:
            return kwh * self.rate(timestamp)
        return kwh * self.export_rate_inr_per_kwh

    def demand_charge(self, peak_kva: float) -> float:
        """
        Monthly demand charge for a given billed peak.

        Demand above the sanctioned load is penalised, which is why shaving a
        peak is worth far more than the energy it contains.
        """
        if peak_kva <= self.sanctioned_demand_kva:
            return peak_kva * self.demand_charge_inr_per_kva
        within = self.sanctioned_demand_kva * self.demand_charge_inr_per_kva
        excess = ((peak_kva - self.sanctioned_demand_kva)
                  * self.demand_charge_inr_per_kva * self.excess_demand_penalty)
        return within + excess

    def pf_adjustment(self, average_pf: float, energy_bill_inr: float) -> float:
        """
        Penalty (positive) or incentive (negative) for average power factor.

        Most Indian utilities penalise below the threshold and a good few pay an
        incentive above it. This is a real line on an MSME bill and it responds
        to nothing more than switching order, so it is worth surfacing.
        """
        if average_pf <= 0 or average_pf > 1:
            return 0.0
        points = (self.pf_penalty_threshold - average_pf) * 100.0
        return energy_bill_inr * (self.pf_penalty_pct_per_point / 100.0) * points

    # ------------------------------------------------------------------ #
    def describe(self, timestamp: float | None = None) -> dict[str, Any]:
        now = timestamp if timestamp is not None else time.time()
        window = self.window_at(now)
        return {
            "now": {
                "window": window.name,
                "multiplier": window.multiplier,
                "rate_inr_per_kwh": round(self.rate(now), 3),
            },
            "windows": [
                {"name": w.name, "start_hour": w.start_hour,
                 "end_hour": w.end_hour, "multiplier": w.multiplier,
                 "rate_inr_per_kwh": round(self.base_rate_inr_per_kwh * w.multiplier, 3)}
                for w in self.windows
            ],
            "base_rate_inr_per_kwh": self.base_rate_inr_per_kwh,
            "export_rate_inr_per_kwh": self.export_rate_inr_per_kwh,
            "demand_charge_inr_per_kva": self.demand_charge_inr_per_kva,
            "sanctioned_demand_kva": self.sanctioned_demand_kva,
            "spread": self.spread(now),
        }


class DemandTracker:
    """
    Rolling maximum-demand meter, mirroring how a utility actually bills.

    Demand is the *average* over a fixed billing window, not an instantaneous
    peak, which is exactly why a battery can shave it: a two-minute motor start
    barely moves a thirty-minute average, but four machines running together for
    twenty minutes sets the month's charge.
    """

    def __init__(self, schedule: TariffSchedule | None = None) -> None:
        self.schedule = schedule or TariffSchedule.from_config()
        self._window: list[tuple[float, float]] = []   # (t, kVA)
        self.billing_peak_kva = 0.0
        self.peak_at = 0.0

    @property
    def window_seconds(self) -> float:
        return self.schedule.demand_window_minutes * 60.0

    def push(self, timestamp: float, apparent_va: float) -> float:
        """Add a sample; returns the current windowed average in kVA."""
        kva = max(0.0, apparent_va) / 1000.0
        self._window.append((timestamp, kva))
        cutoff = timestamp - self.window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.pop(0)

        average = sum(v for _, v in self._window) / max(1, len(self._window))
        # Only count a window we actually filled, or the first few samples of a
        # cold start would each look like a complete billing window.
        span = self._window[-1][0] - self._window[0][0] if len(self._window) > 1 else 0.0
        if span >= self.window_seconds * 0.8 and average > self.billing_peak_kva:
            self.billing_peak_kva = average
            self.peak_at = timestamp
        return average

    def current_average_kva(self) -> float:
        if not self._window:
            return 0.0
        return sum(v for _, v in self._window) / len(self._window)

    def headroom_kva(self) -> float:
        """How much more we can draw before setting a new billing peak."""
        return max(0.0, self.schedule.sanctioned_demand_kva - self.current_average_kva())

    def reset_month(self) -> None:
        self.billing_peak_kva = 0.0
        self.peak_at = 0.0

    def status(self) -> dict[str, Any]:
        return {
            "current_kva": round(self.current_average_kva(), 3),
            "billing_peak_kva": round(self.billing_peak_kva, 3),
            "sanctioned_kva": self.schedule.sanctioned_demand_kva,
            "headroom_kva": round(self.headroom_kva(), 3),
            "window_minutes": self.schedule.demand_window_minutes,
            "projected_charge_inr": round(
                self.schedule.demand_charge(self.billing_peak_kva), 2),
        }
