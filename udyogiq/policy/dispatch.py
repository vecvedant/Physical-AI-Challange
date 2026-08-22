"""
Battery dispatch: deciding when to charge, discharge or do nothing.

The problem
-----------
Given a forecast of what the plant will draw, a forecast of what the roof will
generate, a tariff that changes through the day, and a battery with limits and
a finite life, choose a charge/discharge power for each of the next 96 quarter
hours so that the day costs as little as possible.

Why dynamic programming rather than an LP solver
------------------------------------------------
This is a sequential decision problem with one continuous state - state of
charge - and it is small.  Discretising SoC into a few dozen levels turns it
into a shortest-path problem over a grid of 96 stages, which backward induction
solves *exactly* in a few milliseconds of numpy.

An MILP would give the same answer and cost more: a solver dependency (PuLP,
CBC) that has to install and behave on the board's aarch64 Debian, more
memory, and a failure mode where the solver returns "infeasible" at three in
the morning and the plant has no plan at all.  Dynamic programming has no
external dependency, cannot fail to return a schedule, and every decision it
makes can be traced back to the stage cost that produced it - which matters
because an operator is going to ask why the battery did something.

The discretisation is the honest cost.  With 41 SoC levels the grid resolution
is about 2.5% of capacity, so the schedule is optimal to within roughly that.
Interpolating the value function between levels recovers most of it.

Receding horizon
----------------
The plan is re-solved every block from the current measured SoC using updated
forecasts, and only the first action is ever executed.  That is textbook model
predictive control, and it is what makes the whole thing robust to forecast
error: a wrong forecast costs one block of suboptimality, not a wrong day.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config import CONFIG
from ..ml.battery import BatterySpec, BatteryState
from ..sustain.tariff import TariffSchedule


@dataclass
class DispatchAction:
    """One block of the plan."""

    block: int
    timestamp: float
    #: Positive discharges the battery, negative charges it.
    battery_w: float
    expected_load_w: float
    expected_solar_w: float
    expected_grid_w: float
    soc_before: float
    soc_after: float
    import_rate: float
    stage_cost_inr: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "timestamp": self.timestamp,
            "battery_w": round(self.battery_w, 1),
            "expected_load_w": round(self.expected_load_w, 1),
            "expected_solar_w": round(self.expected_solar_w, 1),
            "expected_grid_w": round(self.expected_grid_w, 1),
            "soc_before": round(self.soc_before, 4),
            "soc_after": round(self.soc_after, 4),
            "import_rate": round(self.import_rate, 3),
            "stage_cost_inr": round(self.stage_cost_inr, 4),
            "reason": self.reason,
        }


@dataclass
class DispatchPlan:
    """A full horizon of actions, plus what it is expected to cost."""

    actions: list[DispatchAction] = field(default_factory=list)
    total_cost_inr: float = 0.0
    baseline_cost_inr: float = 0.0
    #: Worth of the net energy left in the battery at the end of the horizon,
    #: valued at what it would otherwise cost to buy.
    stored_value_inr: float = 0.0
    solve_ms: float = 0.0
    created_at: float = 0.0
    horizon_blocks: int = 0

    @property
    def effective_cost_inr(self) -> float:
        """
        Cash spent, less the value of energy banked in the battery.

        Without this correction the comparison is rigged against the optimiser.
        The baseline finishes the horizon at the same state of charge it
        started at; an optimised plan often finishes higher, having deliberately
        bought cheap energy it has not spent yet. Counting only cash made that
        look like a loss - a measured -23% "saving" on a plan that was in fact
        ahead, because it ended holding 2.7 kWh it had paid off-peak prices for.
        """
        return self.total_cost_inr - self.stored_value_inr

    @property
    def saving_inr(self) -> float:
        """
        Expected saving against grid-only operation over the same horizon.

        The baseline is the same load, the same solar, and no battery at all -
        exactly what the site would do without this device - so the difference
        is attributable to the optimiser and to nothing else.
        """
        return self.baseline_cost_inr - self.effective_cost_inr

    @property
    def first(self) -> DispatchAction | None:
        return self.actions[0] if self.actions else None

    def to_dict(self, limit: int = 96) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "horizon_blocks": self.horizon_blocks,
            "total_cost_inr": round(self.total_cost_inr, 2),
            "baseline_cost_inr": round(self.baseline_cost_inr, 2),
            "stored_value_inr": round(self.stored_value_inr, 2),
            "effective_cost_inr": round(self.effective_cost_inr, 2),
            "saving_inr": round(self.saving_inr, 2),
            "solve_ms": round(self.solve_ms, 1),
            "actions": [a.to_dict() for a in self.actions[:limit]],
        }


class DispatchOptimiser:
    """Backward-induction optimiser over a discretised state of charge."""

    def __init__(self, spec: BatterySpec | None = None,
                 schedule: TariffSchedule | None = None,
                 soc_levels: int = 41,
                 power_levels: int = 21,
                 block_minutes: int | None = None) -> None:
        self.spec = spec or BatterySpec.from_config()
        self.schedule = schedule or TariffSchedule.from_config()
        self.block_minutes = block_minutes or CONFIG.learning.forecast_block_minutes
        self.soc_levels = soc_levels
        self.power_levels = power_levels

        self._soc_grid = np.linspace(self.spec.soc_floor, self.spec.soc_ceiling,
                                     soc_levels)
        # Actions span full charge to full discharge, always including exactly
        # zero so "do nothing" is representable rather than approximated.
        half = power_levels // 2
        self._actions = np.concatenate([
            np.linspace(-self.spec.max_charge_w, 0.0, half + 1)[:-1],
            [0.0],
            np.linspace(0.0, self.spec.max_discharge_w, half + 1)[1:],
        ])

    # ------------------------------------------------------------------ #
    def _stage_cost(self, load_w: np.ndarray, solar_w: np.ndarray,
                    rate: float, export_rate: float,
                    demand_limit_w: float,
                    demand_penalty_per_w: float) -> np.ndarray:
        """
        Cost in rupees for every (soc, action) pair at one block.

        Shape is (soc_levels, power_levels). Broadcasting does the work: the
        grid import depends only on the action, but the feasibility mask
        depends on the SoC, so both dimensions have to be carried.
        """
        hours = self.block_minutes / 60.0
        battery = self._actions[None, :]                       # (1, A)

        # What the grid has to supply once solar and battery have contributed.
        grid_w = load_w[None, :] - solar_w[None, :] - battery   # (1, A)
        grid_kwh = grid_w * hours / 1000.0

        energy_cost = np.where(grid_kwh >= 0,
                               grid_kwh * rate,
                               grid_kwh * export_rate)

        # Cycle life consumed. Charging and discharging both count: throughput
        # is throughput, and pretending otherwise would halve the true cost.
        throughput_kwh = np.abs(battery) * hours / 1000.0
        degradation = throughput_kwh * self.spec.degradation_inr_per_kwh

        # Maximum demand. A soft penalty rather than a hard constraint: making
        # it hard can render the problem infeasible on a day when the load
        # genuinely exceeds the sanctioned limit and the battery is empty, and
        # a plan that costs too much beats no plan at all.
        excess_w = np.maximum(0.0, grid_w - demand_limit_w)
        demand = excess_w * demand_penalty_per_w

        return energy_cost + degradation + demand

    # ------------------------------------------------------------------ #
    def solve(self, *, soc_now: float, load_profile: np.ndarray,
              solar_profile: np.ndarray, start_t: float,
              demand_limit_w: float | None = None) -> DispatchPlan:
        """
        Solve the whole horizon and return the plan.

        ``load_profile`` and ``solar_profile`` are expected watts per block.
        """
        t0 = time.perf_counter()
        blocks = int(min(len(load_profile), len(solar_profile)))
        if blocks == 0:
            return DispatchPlan(created_at=time.time())

        load = np.asarray(load_profile[:blocks], dtype=np.float64)
        solar = np.asarray(solar_profile[:blocks], dtype=np.float64)
        hours = self.block_minutes / 60.0
        span_s = self.block_minutes * 60

        rates = self.schedule.rate_profile(start_t, blocks, self.block_minutes)
        export_rates = self.schedule.export_profile(start_t, blocks, self.block_minutes)

        if demand_limit_w is None:
            demand_limit_w = self.schedule.sanctioned_demand_kva * 1000.0
        # Priced so that a kW of excess demand hurts far more than the energy
        # in it, which is what actually happens on the bill.
        demand_penalty_per_w = (self.schedule.demand_charge_inr_per_kva
                                * self.schedule.excess_demand_penalty / 1000.0)

        n_soc, n_act = self._soc_grid.size, self._actions.size
        eff = self.spec.one_way_efficiency
        cap = self.spec.capacity_wh

        # --- next-state map, identical for every stage ------------------- #
        # Discharging drains more than it delivers; charging stores less than
        # it draws. Both losses are on the same one-way efficiency.
        delta_wh = np.where(self._actions >= 0,
                            -self._actions * hours / eff,
                            -self._actions * hours * eff)
        next_soc = self._soc_grid[:, None] + delta_wh[None, :] / cap
        feasible = ((next_soc >= self.spec.soc_floor - 1e-9)
                    & (next_soc <= self.spec.soc_ceiling + 1e-9))
        next_soc = np.clip(next_soc, self.spec.soc_floor, self.spec.soc_ceiling)

        INFEASIBLE = 1e12

        # --- backward induction ------------------------------------------ #
        # Terminal value: energy left in the battery is worth what it would
        # cost to buy. Without this the optimiser empties the battery on the
        # last block every time, because in a finite horizon stored energy has
        # no value - and since MPC only ever executes the first action, that
        # artefact would leak into every real decision.
        terminal_rate = float(np.mean(rates))
        value = -(self._soc_grid - self.spec.soc_floor) * cap / 1000.0 * terminal_rate * eff

        policy = np.zeros((blocks, n_soc), dtype=np.int16)
        for i in range(blocks - 1, -1, -1):
            stage = self._stage_cost(load[i:i + 1], solar[i:i + 1],
                                     float(rates[i]), float(export_rates[i]),
                                     demand_limit_w, demand_penalty_per_w)
            stage = np.broadcast_to(stage, (n_soc, n_act)).copy()

            # Value of landing at next_soc, interpolated between grid levels so
            # the discretisation costs accuracy rather than correctness.
            future = np.interp(next_soc.ravel(), self._soc_grid, value)
            future = future.reshape(n_soc, n_act)

            total = stage + future
            total[~feasible] = INFEASIBLE

            policy[i] = np.argmin(total, axis=1)
            value = np.min(total, axis=1)

        # --- roll the policy forward from where we actually are ---------- #
        actions: list[DispatchAction] = []
        soc = float(np.clip(soc_now, self.spec.soc_floor, self.spec.soc_ceiling))
        total_cost = 0.0

        for i in range(blocks):
            idx = int(np.argmin(np.abs(self._soc_grid - soc)))
            a_idx = int(policy[i, idx])
            battery_w = float(self._actions[a_idx])

            # Re-check against the true continuous SoC: the policy was chosen
            # on the nearest grid level, which may be marginally infeasible
            # here.
            state = BatteryState(self.spec, soc)
            battery_w = state.apply(battery_w, span_s)
            soc_after = state.soc

            grid_w = float(load[i] - solar[i] - battery_w)
            grid_kwh = grid_w * hours / 1000.0
            cost = (grid_kwh * float(rates[i]) if grid_kwh >= 0
                    else grid_kwh * float(export_rates[i]))
            cost += abs(battery_w) * hours / 1000.0 * self.spec.degradation_inr_per_kwh
            cost += max(0.0, grid_w - demand_limit_w) * demand_penalty_per_w
            total_cost += cost

            actions.append(DispatchAction(
                block=i,
                timestamp=start_t + i * span_s,
                battery_w=battery_w,
                expected_load_w=float(load[i]),
                expected_solar_w=float(solar[i]),
                expected_grid_w=grid_w,
                soc_before=soc,
                soc_after=soc_after,
                import_rate=float(rates[i]),
                stage_cost_inr=cost,
                reason=self._explain(battery_w, float(load[i]), float(solar[i]),
                                     float(rates[i]), grid_w, demand_limit_w),
            ))
            soc = soc_after

        baseline = self._baseline_cost(load, solar, rates, export_rates,
                                       demand_limit_w, demand_penalty_per_w)

        # Value the net change in stored energy on the same basis the DP used
        # for its terminal condition, so the reported saving and the quantity
        # actually optimised agree with each other.
        stored_value = ((soc - float(np.clip(soc_now, self.spec.soc_floor,
                                             self.spec.soc_ceiling)))
                        * cap / 1000.0 * terminal_rate * eff)

        return DispatchPlan(
            actions=actions,
            total_cost_inr=total_cost,
            baseline_cost_inr=baseline,
            stored_value_inr=stored_value,
            solve_ms=(time.perf_counter() - t0) * 1000.0,
            created_at=time.time(),
            horizon_blocks=blocks,
        )

    # ------------------------------------------------------------------ #
    def _baseline_cost(self, load: np.ndarray, solar: np.ndarray,
                       rates: np.ndarray, export_rates: np.ndarray,
                       demand_limit_w: float, demand_penalty_per_w: float) -> float:
        """Same day, same solar, no battery. What the site does today."""
        hours = self.block_minutes / 60.0
        grid_w = load - solar
        grid_kwh = grid_w * hours / 1000.0
        cost = np.where(grid_kwh >= 0, grid_kwh * rates, grid_kwh * export_rates)
        demand = np.maximum(0.0, grid_w - demand_limit_w) * demand_penalty_per_w
        return float(np.sum(cost + demand))

    def _explain(self, battery_w: float, load_w: float, solar_w: float,
                 rate: float, grid_w: float, demand_limit_w: float) -> str:
        """A short human reason, so the dashboard never shows a bare number."""
        if abs(battery_w) < 20.0:
            if solar_w > load_w:
                return "Exporting surplus - battery already full"
            return "Holding charge - not worth cycling at this price"
        if battery_w < 0:
            if solar_w > load_w:
                return f"Storing {abs(battery_w):.0f} W of surplus solar"
            return f"Charging at {rate:.2f}/kWh to discharge at peak"
        if grid_w > demand_limit_w * 0.9:
            return f"Discharging {battery_w:.0f} W to hold demand below the limit"
        return f"Discharging {battery_w:.0f} W to avoid {rate:.2f}/kWh grid"


# --------------------------------------------------------------------------- #
class MPCController:
    """
    Receding-horizon wrapper.

    Re-solves on every block from the measured state of charge and executes
    only the first action. Forecast error therefore costs one block of
    suboptimality rather than a whole misplanned day.
    """

    def __init__(self, optimiser: DispatchOptimiser | None = None) -> None:
        self.optimiser = optimiser or DispatchOptimiser()
        self.plan: DispatchPlan | None = None
        self.last_solved_t = 0.0
        self.solve_count = 0

    @property
    def block_seconds(self) -> float:
        return self.optimiser.block_minutes * 60.0

    def due(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.last_solved_t) >= self.block_seconds

    def step(self, *, soc_now: float, load_profile: np.ndarray,
             solar_profile: np.ndarray, now: float | None = None,
             demand_limit_w: float | None = None,
             force: bool = False) -> DispatchAction | None:
        """Re-solve if due and return the action to execute right now."""
        now = now if now is not None else time.time()
        if force or self.due(now) or self.plan is None:
            self.plan = self.optimiser.solve(
                soc_now=soc_now,
                load_profile=load_profile,
                solar_profile=solar_profile,
                start_t=now,
                demand_limit_w=demand_limit_w,
            )
            self.last_solved_t = now
            self.solve_count += 1
        return self.plan.first if self.plan else None

    def status(self) -> dict[str, Any]:
        return {
            "solve_count": self.solve_count,
            "last_solved_t": self.last_solved_t,
            "battery": self.optimiser.spec.describe(),
            "plan": self.plan.to_dict(limit=96) if self.plan else None,
        }
