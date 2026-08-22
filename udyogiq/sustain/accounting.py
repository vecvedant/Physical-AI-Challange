"""
Turning measurements into the three numbers a factory owner actually cares
about: rupees, kilograms of CO2, and where the waste is.

The counterfactual is the important idea here
---------------------------------------------
Any device can display a kWh total.  What an owner wants to know is whether the
box on the wall is worth what it cost, and that requires comparing what
happened against what *would* have happened without it.  So the node runs a
shadow accounting model in parallel - the same load, the same solar, the same
tariff, but no optimisation and no idle cutoff - and reports the difference.

This is a genuine counterfactual rather than a marketing figure because the
baseline is not a guess: it is the site's own measured demand replayed under
the rules it was operating under before the device was installed.  Where the
device could not have made a difference, the difference is zero, and it says so.

Emission factor
---------------
The Indian grid's average operating margin, from the CEA CO2 baseline database,
is around 0.71 kg CO2 per kWh.  That is a national annual average and the real
figure moves by state, by season and by hour, so anything derived from it is an
estimate to one significant figure and is labelled as such.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any

from ..config import CONFIG
from .tariff import TariffSchedule


@dataclass
class EnergyLedger:
    """Running energy, cost and emissions totals for one accounting period."""

    period_start_t: float = 0.0

    import_kwh: float = 0.0
    export_kwh: float = 0.0
    solar_kwh: float = 0.0
    battery_charged_kwh: float = 0.0
    battery_discharged_kwh: float = 0.0
    load_kwh: float = 0.0

    energy_cost_inr: float = 0.0
    export_credit_inr: float = 0.0
    degradation_cost_inr: float = 0.0

    #: Energy burned by machines that were on but doing no useful work.
    idle_kwh: float = 0.0
    idle_cost_inr: float = 0.0
    #: Energy the idle cutoff actually prevented from being burned.
    idle_avoided_kwh: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        blob = asdict(self)
        blob["net_cost_inr"] = self.net_cost_inr
        blob["co2_kg"] = self.co2_kg
        blob["self_consumption_pct"] = self.self_consumption_pct
        return blob

    @property
    def net_cost_inr(self) -> float:
        return self.energy_cost_inr - self.export_credit_inr + self.degradation_cost_inr

    @property
    def co2_kg(self) -> float:
        """
        Emissions attributable to imported grid energy.

        Solar and battery-discharged energy are treated as zero-carbon at the
        point of use, which is the standard operational convention. Embodied
        emissions in the panels and cells are out of scope and would need a
        life-cycle assessment we have no basis to perform.
        """
        return self.import_kwh * CONFIG.sustainability.grid_emission_factor_kg_per_kwh

    @property
    def self_consumption_pct(self) -> float:
        """Share of the plant's demand met without touching the grid."""
        if self.load_kwh <= 0:
            return 0.0
        on_site = min(self.load_kwh, self.solar_kwh + self.battery_discharged_kwh)
        return 100.0 * on_site / self.load_kwh


class SustainabilityAccountant:
    """
    Integrates instantaneous power into energy, cost and carbon.

    Fed one sample at a time, so it works identically on live hardware and on a
    replayed recording.
    """

    def __init__(self, schedule: TariffSchedule | None = None) -> None:
        self.schedule = schedule or TariffSchedule.from_config()
        self.today = EnergyLedger(period_start_t=time.time())
        self.month = EnergyLedger(period_start_t=time.time())
        self.lifetime = EnergyLedger(period_start_t=time.time())
        self._last_t: float | None = None
        self._last_day = time.localtime().tm_yday
        self._last_month = time.localtime().tm_mon

    # ------------------------------------------------------------------ #
    def _ledgers(self) -> tuple[EnergyLedger, ...]:
        return (self.today, self.month, self.lifetime)

    def _roll_periods(self, now: float) -> None:
        lt = time.localtime(now)
        if lt.tm_yday != self._last_day:
            self.today = EnergyLedger(period_start_t=now)
            self._last_day = lt.tm_yday
        if lt.tm_mon != self._last_month:
            self.month = EnergyLedger(period_start_t=now)
            self._last_month = lt.tm_mon

    # ------------------------------------------------------------------ #
    def update(self, *, timestamp: float, grid_w: float, load_w: float,
               solar_w: float = 0.0, battery_w: float = 0.0,
               idle_w: float = 0.0, degradation_inr_per_kwh: float = 0.0) -> None:
        """
        Fold in one sample.

        ``grid_w`` is signed, positive importing. ``battery_w`` is positive
        discharging. ``idle_w`` is the share of load attributable to machines
        that are on but not doing useful work.
        """
        self._roll_periods(timestamp)

        if self._last_t is None:
            self._last_t = timestamp
            return
        dt_h = (timestamp - self._last_t) / 3600.0
        self._last_t = timestamp
        # A long gap means the node was off, not that the plant drew that power
        # continuously the whole time. Integrating across it would invent
        # energy that was never used.
        if dt_h <= 0 or dt_h > 0.25:
            return

        rate = self.schedule.rate(timestamp)
        import_kwh = max(0.0, grid_w) * dt_h / 1000.0
        export_kwh = max(0.0, -grid_w) * dt_h / 1000.0

        for led in self._ledgers():
            led.import_kwh += import_kwh
            led.export_kwh += export_kwh
            led.load_kwh += max(0.0, load_w) * dt_h / 1000.0
            led.solar_kwh += max(0.0, solar_w) * dt_h / 1000.0
            if battery_w >= 0:
                led.battery_discharged_kwh += battery_w * dt_h / 1000.0
            else:
                led.battery_charged_kwh += -battery_w * dt_h / 1000.0

            led.energy_cost_inr += import_kwh * rate
            led.export_credit_inr += export_kwh * self.schedule.export_rate_inr_per_kwh
            led.degradation_cost_inr += (abs(battery_w) * dt_h / 1000.0
                                         * degradation_inr_per_kwh)

            idle_kwh = max(0.0, idle_w) * dt_h / 1000.0
            led.idle_kwh += idle_kwh
            led.idle_cost_inr += idle_kwh * rate

    def record_idle_avoided(self, kwh: float) -> None:
        """Credit energy the idle cutoff prevented from being burned."""
        for led in self._ledgers():
            led.idle_avoided_kwh += max(0.0, kwh)

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        ef = CONFIG.sustainability.grid_emission_factor_kg_per_kwh
        return {
            "today": self.today.to_dict(),
            "month": self.month.to_dict(),
            "lifetime": self.lifetime.to_dict(),
            "emission_factor_kg_per_kwh": ef,
        }


# --------------------------------------------------------------------------- #
class CounterfactualLedger:
    """
    What the same day would have cost without the device.

    Two worlds are integrated side by side from identical measured load and
    solar: the real one, with whatever the optimiser and the cutoff actually
    did, and a shadow one where the battery never moves and idle machines are
    never switched off. The difference is the device's contribution, and it is
    zero on days when there was nothing to gain - which is the point of doing
    it this way rather than quoting a brochure figure.
    """

    def __init__(self, schedule: TariffSchedule | None = None) -> None:
        self.schedule = schedule or TariffSchedule.from_config()
        self.actual_cost_inr = 0.0
        self.baseline_cost_inr = 0.0
        self.actual_import_kwh = 0.0
        self.baseline_import_kwh = 0.0
        self.actual_peak_kva = 0.0
        self.baseline_peak_kva = 0.0
        self.started_t = time.time()
        self._last_t: float | None = None
        # Span is measured from the sample timestamps, never from the wall
        # clock. Replaying a recording or running against the simulator covers
        # hours of plant time in seconds of real time, and dividing a real
        # saving by a near-zero elapsed wall time projected Rs 6,030,686 a
        # month from Rs 5.82 of actual saving.
        self._first_sample_t: float | None = None
        self._observed_span_s: float = 0.0
        # Demand is billed on the average over a fixed window, not on an
        # instantaneous peak, so both worlds are averaged the same way before
        # being compared. Using instantaneous power here would let a two-second
        # motor start set a "peak" the utility would never have billed, and
        # would flatter or damn the device depending on nothing but luck.
        self._actual_window: list[tuple[float, float]] = []
        self._baseline_window: list[tuple[float, float]] = []

    # ------------------------------------------------------------------ #
    def update(self, *, timestamp: float, load_w: float, solar_w: float,
               battery_w: float, idle_w: float = 0.0,
               power_factor: float = 1.0) -> None:
        """
        Advance both worlds by one sample.

        ``load_w`` is the plant's actual demand. In the shadow world any idle
        load the cutoff removed is added back, because without the device it
        would still have been running.
        """
        if self._last_t is None:
            self._last_t = timestamp
            self._first_sample_t = timestamp
            return
        dt_h = (timestamp - self._last_t) / 3600.0
        self._last_t = timestamp
        if dt_h <= 0 or dt_h > 0.25:
            return
        self._observed_span_s += dt_h * 3600.0

        rate = self.schedule.rate(timestamp)
        export_rate = self.schedule.export_rate_inr_per_kwh

        # Real world: solar and battery both offset the grid.
        actual_grid_w = load_w - solar_w - battery_w
        # Shadow world: solar still helps - it is on the roof either way - but
        # the battery never moves and the idle load was never cut.
        baseline_grid_w = (load_w + idle_w) - solar_w

        for grid_w, attr_cost, attr_kwh in (
                (actual_grid_w, "actual_cost_inr", "actual_import_kwh"),
                (baseline_grid_w, "baseline_cost_inr", "baseline_import_kwh")):
            kwh = grid_w * dt_h / 1000.0
            cost = kwh * rate if kwh >= 0 else kwh * export_rate
            setattr(self, attr_cost, getattr(self, attr_cost) + cost)
            setattr(self, attr_kwh, getattr(self, attr_kwh) + max(0.0, kwh))

        pf = max(0.3, min(1.0, abs(power_factor) or 1.0))
        window_s = self.schedule.demand_window_minutes * 60.0
        self.actual_peak_kva = self._roll_peak(
            self._actual_window, timestamp, abs(actual_grid_w) / pf / 1000.0,
            window_s, self.actual_peak_kva)
        self.baseline_peak_kva = self._roll_peak(
            self._baseline_window, timestamp, abs(baseline_grid_w) / pf / 1000.0,
            window_s, self.baseline_peak_kva)

    @staticmethod
    def _roll_peak(window: list[tuple[float, float]], t: float, kva: float,
                   window_s: float, current_peak: float) -> float:
        """Windowed-average demand, mirroring how the utility meters it."""
        window.append((t, kva))
        cutoff = t - window_s
        while window and window[0][0] < cutoff:
            window.pop(0)
        average = sum(v for _, v in window) / max(1, len(window))
        span = window[-1][0] - window[0][0] if len(window) > 1 else 0.0
        # Only a window we actually filled counts, or the first samples after a
        # restart would each look like a complete billing period.
        if span >= window_s * 0.8:
            return max(current_peak, average)
        return current_peak

    # ------------------------------------------------------------------ #
    @property
    def energy_saving_inr(self) -> float:
        return self.baseline_cost_inr - self.actual_cost_inr

    @property
    def demand_saving_inr(self) -> float:
        """
        Saving on the demand charge, which is usually the larger half.

        Both peaks are priced through the same tariff function, so the excess
        penalty above sanctioned load is reflected properly rather than being
        treated as a flat rate.
        """
        return (self.schedule.demand_charge(self.baseline_peak_kva)
                - self.schedule.demand_charge(self.actual_peak_kva))

    @property
    def total_saving_inr(self) -> float:
        return self.energy_saving_inr + self.demand_saving_inr

    @property
    def co2_saved_kg(self) -> float:
        ef = CONFIG.sustainability.grid_emission_factor_kg_per_kwh
        return max(0.0, self.baseline_import_kwh - self.actual_import_kwh) * ef

    @property
    def days_observed(self) -> float:
        """Plant time actually covered by samples, not wall time elapsed."""
        return max(self._observed_span_s / 86400.0, 1e-6)

    def snapshot(self) -> dict[str, Any]:
        return {
            "since_t": self._first_sample_t or self.started_t,
            "days_observed": round(self.days_observed, 3),
            "actual_cost_inr": round(self.actual_cost_inr, 2),
            "baseline_cost_inr": round(self.baseline_cost_inr, 2),
            "energy_saving_inr": round(self.energy_saving_inr, 2),
            "actual_peak_kva": round(self.actual_peak_kva, 3),
            "baseline_peak_kva": round(self.baseline_peak_kva, 3),
            "demand_saving_inr": round(self.demand_saving_inr, 2),
            "total_saving_inr": round(self.total_saving_inr, 2),
            "co2_saved_kg": round(self.co2_saved_kg, 2),
            "projected_monthly_inr": round(self.projected_monthly_inr, 2),
        }

    @property
    def projected_monthly_inr(self) -> float:
        """
        Expected saving over a full month.

        Only the energy half is scaled. The demand charge is levied once a
        month against the highest window in it, so multiplying an observed
        demand saving by thirty would overstate it thirtyfold - which, on an
        hour of data, produced a projection of -Rs 95 from a real saving of
        Rs 1.80. Energy scales with time; demand does not.
        """
        return (self.energy_saving_inr / self.days_observed * 30.0
                + self.demand_saving_inr)


# --------------------------------------------------------------------------- #
def specific_energy_consumption(energy_kwh: float, productive_hours: float,
                                units_produced: float = 0.0) -> dict[str, float]:
    """
    Energy per unit of useful work - the number that actually tracks efficiency.

    Total consumption tells an owner how busy they were, not how efficient they
    were: a good month and a wasteful month can consume the same kWh. SEC
    normalises that away. Per productive hour is always available because NILM
    knows how long each machine actually ran; per unit produced needs a count
    from the operator and is reported only when supplied.
    """
    out = {"kwh_per_productive_hour": 0.0, "kwh_per_unit": 0.0}
    if productive_hours > 0:
        out["kwh_per_productive_hour"] = energy_kwh / productive_hours
    if units_produced > 0:
        out["kwh_per_unit"] = energy_kwh / units_produced
    return out
