"""
A synthetic single-phase workshop, good enough to train against.

The point of this module is not to look pretty on a chart.  It is to produce an
aggregate power signal that has the *same structure* the NILM and health models
will meet on real hardware:

  * loads that switch in discrete steps with a distinct P and Q signature,
  * motors that draw a brief inrush before settling,
  * duty cycling that is periodic but not perfectly regular,
  * measurement noise and mains voltage wander,
  * and slow degradation that a health model is supposed to notice before a
    human would.

If a model can recover the machine list from this, it has learned something
about steps and signatures rather than memorising a specific waveform.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum


class Duty(str, Enum):
    CONTINUOUS = "continuous"   # lighting: on for the whole shift
    CYCLIC = "cyclic"           # compressor, fridge: thermostat driven
    SPORADIC = "sporadic"       # bench tools: operator driven, bursty
    SCHEDULED = "scheduled"     # production machine: runs during shift hours


@dataclass
class Appliance:
    """One switchable load with a recognisable electrical signature."""

    name: str
    rated_w: float
    power_factor: float
    duty: Duty

    # Cyclic / sporadic timing, in seconds.
    on_duration_s: float = 300.0
    off_duration_s: float = 900.0
    timing_jitter: float = 0.25          # +/- fraction applied to each interval

    # Motors pull a multiple of rated power for a moment at start.
    inrush_factor: float = 1.0
    inrush_s: float = 0.0

    # Shift window for SCHEDULED / CONTINUOUS loads, in hours past midnight.
    shift_start_h: float = 9.0
    shift_end_h: float = 18.0

    # Steady-state ripple as a fraction of rated power.
    ripple: float = 0.02

    # Health.  1.0 is factory-fresh.  Degradation makes the machine draw more
    # real power for the same work, pull worse power factor, and take longer to
    # get up to speed - which is exactly what a worn bearing or a slipping belt
    # does in the real world.
    health: float = 1.0
    critical: bool = False               # never shed by the dispatch optimiser

    # --- runtime state -------------------------------------------------- #
    _on: bool = field(default=False, repr=False)
    _next_switch_t: float = field(default=0.0, repr=False)
    _started_at: float = field(default=-1e9, repr=False)

    # ------------------------------------------------------------------ #
    def _interval(self, base: float, rng: random.Random) -> float:
        return max(5.0, base * (1.0 + rng.uniform(-self.timing_jitter,
                                                  self.timing_jitter)))

    def _in_shift(self, hour: float) -> bool:
        if self.shift_start_h <= self.shift_end_h:
            return self.shift_start_h <= hour < self.shift_end_h
        # Shift wraps past midnight.
        return hour >= self.shift_start_h or hour < self.shift_end_h

    def step(self, t: float, hour: float, rng: random.Random) -> tuple[float, float]:
        """
        Advance to absolute time ``t`` and return (active_W, reactive_VAr).
        """
        in_shift = self._in_shift(hour)

        if self.duty is Duty.CONTINUOUS:
            want_on = in_shift
            if want_on != self._on:
                self._on = want_on
                if want_on:
                    self._started_at = t

        elif self.duty in (Duty.CYCLIC, Duty.SPORADIC):
            # A sporadic load only runs during the shift; a cyclic one (a
            # fridge, a compressor holding pressure) keeps going overnight.
            allowed = in_shift if self.duty is Duty.SPORADIC else True
            if not allowed:
                self._on = False
            elif t >= self._next_switch_t:
                self._on = not self._on
                if self._on:
                    self._started_at = t
                base = self.on_duration_s if self._on else self.off_duration_s
                self._next_switch_t = t + self._interval(base, rng)

        elif self.duty is Duty.SCHEDULED:
            if not in_shift:
                self._on = False
            elif t >= self._next_switch_t:
                self._on = not self._on
                if self._on:
                    self._started_at = t
                base = self.on_duration_s if self._on else self.off_duration_s
                self._next_switch_t = t + self._interval(base, rng)

        if not self._on:
            return 0.0, 0.0

        # Degradation costs efficiency: more real power drawn for the same job.
        wear = 2.0 - self.health                       # 1.0 fresh -> 2.0 dead
        power = self.rated_w * (1.0 + 0.18 * (wear - 1.0))

        # Inrush envelope, stretched as the machine wears - a tired motor takes
        # longer to reach speed, which is one of the few genuinely predictive
        # signals available at this sampling rate.
        since_start = t - self._started_at
        inrush_window = self.inrush_s * (1.0 + 1.5 * (wear - 1.0))
        if inrush_window > 0 and since_start < inrush_window:
            decay = math.exp(-3.0 * since_start / inrush_window)
            power *= 1.0 + (self.inrush_factor - 1.0) * decay

        power *= 1.0 + rng.gauss(0.0, self.ripple)
        power = max(0.0, power)

        # Power factor sags as the machine degrades.
        pf = min(0.999, max(0.30, self.power_factor - 0.16 * (wear - 1.0)))
        # Q = P * tan(acos(pf))
        tan_phi = math.sqrt(max(0.0, 1.0 - pf * pf)) / max(pf, 1e-3)
        return power, power * tan_phi


# --------------------------------------------------------------------------- #
# Solar and battery
# --------------------------------------------------------------------------- #
@dataclass
class SolarArray:
    """
    Clear-sky output shaped by a slowly wandering cloud factor.

    Deliberately simple: the forecasting model must not be able to invert this
    exactly, or the evaluation would be meaningless.
    """

    peak_w: float = 3000.0
    sunrise_h: float = 6.3
    sunset_h: float = 18.7
    # Persistent cloud state, evolved as a random walk.
    _cloud: float = field(default=0.15, repr=False)

    def step(self, hour: float, rng: random.Random) -> float:
        # Cloud cover drifts rather than jumping, so forecast error is
        # autocorrelated the way it is in reality.
        self._cloud += rng.gauss(0.0, 0.02)
        self._cloud = min(0.95, max(0.0, self._cloud))

        if not (self.sunrise_h < hour < self.sunset_h):
            return 0.0
        span = self.sunset_h - self.sunrise_h
        phase = math.pi * (hour - self.sunrise_h) / span
        clear_sky = self.peak_w * math.sin(phase) ** 1.3

        # Fast scattered-cloud flicker on top of the slow state.
        flicker = max(0.0, 1.0 - self._cloud - abs(rng.gauss(0.0, 0.05)))
        return max(0.0, clear_sky * flicker)


@dataclass
class Battery:
    """Coulomb-counted store with efficiency and rate limits."""

    capacity_wh: float = 5000.0
    soc: float = 0.55                    # state of charge, 0..1
    max_charge_w: float = 1500.0
    max_discharge_w: float = 1500.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    soc_floor: float = 0.20
    soc_ceiling: float = 0.95

    def apply(self, command_w: float, dt_s: float) -> float:
        """
        Apply a power command and return what actually happened.

        Positive command discharges (battery supplies the plant), negative
        charges.  The return value differs from the command whenever a rate or
        SoC limit binds, which is exactly the behaviour the dispatch optimiser
        must be robust to.
        """
        if command_w >= 0:
            allowed = min(command_w, self.max_discharge_w)
            drawn_wh = allowed * dt_s / 3600.0 / self.discharge_efficiency
            available_wh = (self.soc - self.soc_floor) * self.capacity_wh
            if drawn_wh > available_wh:
                drawn_wh = max(0.0, available_wh)
                allowed = drawn_wh * self.discharge_efficiency * 3600.0 / max(dt_s, 1e-6)
            self.soc -= drawn_wh / self.capacity_wh
            return allowed

        allowed = max(command_w, -self.max_charge_w)
        stored_wh = -allowed * dt_s / 3600.0 * self.charge_efficiency
        headroom_wh = (self.soc_ceiling - self.soc) * self.capacity_wh
        if stored_wh > headroom_wh:
            stored_wh = max(0.0, headroom_wh)
            allowed = -stored_wh / max(self.charge_efficiency, 1e-6) * 3600.0 / max(dt_s, 1e-6)
        self.soc += stored_wh / self.capacity_wh
        return allowed


# --------------------------------------------------------------------------- #
# The plant
# --------------------------------------------------------------------------- #
def default_workshop() -> list[Appliance]:
    """
    A believable small single-phase workshop.

    Sized so that the loads are separable in (P, Q) space but not trivially so:
    the pump and the compressor sit close enough in real power that reactive
    power is what tells them apart, which is the case NILM has to get right.
    """
    return [
        Appliance("Air compressor", rated_w=1500, power_factor=0.82,
                  duty=Duty.CYCLIC, on_duration_s=240, off_duration_s=780,
                  inrush_factor=3.2, inrush_s=1.8, shift_start_h=0, shift_end_h=24),
        Appliance("Coolant pump", rated_w=1350, power_factor=0.74,
                  duty=Duty.SCHEDULED, on_duration_s=900, off_duration_s=420,
                  inrush_factor=2.6, inrush_s=1.2),
        Appliance("Lathe", rated_w=2200, power_factor=0.86,
                  duty=Duty.SCHEDULED, on_duration_s=600, off_duration_s=900,
                  inrush_factor=2.9, inrush_s=2.2, ripple=0.06),
        Appliance("Bench grinder", rated_w=750, power_factor=0.88,
                  duty=Duty.SPORADIC, on_duration_s=90, off_duration_s=1500,
                  inrush_factor=2.2, inrush_s=0.8),
        Appliance("Exhaust fan", rated_w=340, power_factor=0.79,
                  duty=Duty.CONTINUOUS, shift_start_h=8.5, shift_end_h=19.0),
        Appliance("Shop lighting", rated_w=420, power_factor=0.96,
                  duty=Duty.CONTINUOUS, shift_start_h=8.0, shift_end_h=20.0,
                  ripple=0.005, critical=True),
        Appliance("Office / server", rated_w=180, power_factor=0.93,
                  duty=Duty.CYCLIC, on_duration_s=3600, off_duration_s=120,
                  shift_start_h=0, shift_end_h=24, ripple=0.03, critical=True),
    ]


@dataclass
class Plant:
    """
    Drives every appliance, the array and the battery on one clock, and reports
    the aggregate the meter would see.
    """

    appliances: list[Appliance] = field(default_factory=default_workshop)
    solar: SolarArray = field(default_factory=SolarArray)
    battery: Battery = field(default_factory=Battery)

    nominal_voltage: float = 230.0
    nominal_frequency: float = 50.0

    # Where the meter sits.  "grid_tie" sees import/export after solar and
    # battery net off; "load_side" sees the raw plant consumption.
    meter_position: str = "grid_tie"

    seed: int = 7
    start_epoch: float = 0.0
    time_scale: float = 1.0              # >1 runs the plant faster than realtime

    # Cumulative counters, mirroring the meter's own registers.
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    reactive_kvarh: float = 0.0
    apparent_kvah: float = 0.0
    max_demand_w: float = 0.0

    _rng: random.Random = field(default=None, repr=False)
    _last_t: float = field(default=None, repr=False)
    _battery_command_w: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # ------------------------------------------------------------------ #
    def set_battery_command(self, watts: float) -> None:
        """Positive discharges, negative charges.  Set by the dispatch loop."""
        self._battery_command_w = watts

    def degrade(self, appliance_name: str, health: float) -> None:
        """Inject a fault, for demonstrating predictive maintenance."""
        for a in self.appliances:
            if a.name.lower() == appliance_name.lower():
                a.health = max(0.0, min(1.0, health))
                return
        raise KeyError(f"no appliance named {appliance_name!r}")

    def breakdown(self, t: float) -> dict[str, float]:
        """Ground-truth per-appliance power, for scoring NILM."""
        hour = ((t - self.start_epoch) / 3600.0) % 24.0
        return {a.name: a.step(t, hour, self._rng)[0] for a in self.appliances}

    # ------------------------------------------------------------------ #
    def sample(self, t: float) -> dict[str, float]:
        """
        Advance the plant to absolute time ``t`` and return the quantities a
        meter would report, plus ground truth for evaluation.
        """
        dt = 1.0 if self._last_t is None else max(1e-3, t - self._last_t)
        self._last_t = t
        hour = ((t - self.start_epoch) / 3600.0) % 24.0

        load_p = 0.0
        load_q = 0.0
        truth: dict[str, float] = {}
        for a in self.appliances:
            p, q = a.step(t, hour, self._rng)
            truth[a.name] = p
            load_p += p
            load_q += q

        solar_p = self.solar.step(hour, self._rng)
        battery_p = self.battery.apply(self._battery_command_w, dt)

        # Net at the grid tie: what the plant needs, less what solar and the
        # battery supply.  Negative means we are exporting.
        grid_p = load_p - solar_p - battery_p

        measured_p = grid_p if self.meter_position == "grid_tie" else load_p
        # Solar inverters and batteries are close to unity PF, so essentially
        # all reactive demand still lands on the grid.
        measured_q = load_q

        voltage = self.nominal_voltage + self._rng.gauss(0.0, 1.4)
        # Loaded feeders sag; this coupling is a real signal the health model
        # can pick up on.
        voltage -= 0.0016 * max(0.0, measured_p)
        frequency = self.nominal_frequency + self._rng.gauss(0.0, 0.03)

        apparent = math.hypot(measured_p, measured_q)
        current = apparent / max(voltage, 1.0)
        pf = abs(measured_p) / apparent if apparent > 1.0 else 1.0

        # Counters.
        hours = dt / 3600.0
        if measured_p >= 0:
            self.import_kwh += measured_p * hours / 1000.0
        else:
            self.export_kwh += -measured_p * hours / 1000.0
        self.reactive_kvarh += abs(measured_q) * hours / 1000.0
        self.apparent_kvah += apparent * hours / 1000.0
        self.max_demand_w = max(self.max_demand_w, measured_p)

        return {
            "timestamp": t,
            "voltage_v": voltage,
            "current_a": current,
            "active_power_w": measured_p,
            "reactive_power_var": measured_q,
            "apparent_power_va": apparent,
            "power_factor": min(1.0, pf),
            "frequency_hz": frequency,
            "import_active_energy_kwh": self.import_kwh,
            "export_active_energy_kwh": self.export_kwh,
            "total_active_energy_kwh": self.import_kwh + self.export_kwh,
            "total_reactive_energy_kvarh": self.reactive_kvarh,
            "apparent_energy_kvah": self.apparent_kvah,
            "max_demand_active_w": self.max_demand_w,
            "max_demand_apparent_va": self.max_demand_w / max(pf, 0.5),
            # Ground truth, stripped before anything reaches a model.
            "_truth": truth,
            "_solar_w": solar_p,
            "_battery_w": battery_p,
            "_load_w": load_p,
            "_soc": self.battery.soc,
        }
