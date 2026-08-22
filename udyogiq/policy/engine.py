"""
Where inference turns into a contactor moving.

This is the only module in the project that can change the physical world, so
it is written defensively and it is the one place where "do nothing" is always
an acceptable answer.

Three principles it is built on
------------------------------
**Advisory by default.** ``actuation_enabled`` ships false. The engine reasons,
explains and logs exactly what it would have done, and switches nothing until
someone has watched it behave for a while and turned it on deliberately. A
system that starts switching machinery the moment it is powered up does not get
a second chance in a real workshop.

**Every decision carries a reason.** Not a code, a sentence. An operator who
finds a machine off must be able to see "idle for 18 minutes, Rs 4.20 of
standby burned, cut at 14:32" without reading a manual. Unexplained automation
gets disabled.

**The interlock is not here.** Minimum dwell times and the switching-rate cap
are enforced on the STM32, in hardware time. The checks in this file are a
courtesy so we do not issue requests the firmware would refuse; they are not
the safety mechanism. If this process hangs mid-decision, the MCU still
protects the contactor. That division is the entire argument for using a board
with two brains.

Critical loads are never shed, at any priority, for any saving.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable

from ..config import CONFIG


class ActionKind(str, Enum):
    IDLE_CUTOFF = "idle_cutoff"
    DEMAND_SHED = "demand_shed"
    ANOMALY_TRIP = "anomaly_trip"
    RESTORE = "restore"
    DISPATCH = "dispatch"
    NONE = "none"


class Outcome(str, Enum):
    EXECUTED = "executed"
    ADVISED = "advised"          # actuation disabled; we only recommended it
    BLOCKED = "blocked"          # an interlock refused
    SKIPPED = "skipped"          # conditions no longer met


@dataclass
class Decision:
    """One thing the engine decided, whether or not it happened."""

    timestamp: float
    kind: ActionKind
    outcome: Outcome
    reason: str
    target: str = "plant"
    target_id: int = 0
    #: Estimated rupees per hour this decision is worth, where meaningful.
    value_inr_per_h: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        blob = asdict(self)
        blob["kind"] = self.kind.value
        blob["outcome"] = self.outcome.value
        return blob


@dataclass
class ContactorState:
    """What we believe one switchable circuit is doing."""

    name: str = "main"
    closed: bool = True
    last_change_t: float = 0.0
    switches_this_hour: int = 0
    _hour_started_t: float = 0.0
    #: Set when a human has overridden automation on this circuit.
    manual_override: bool = False


class PolicyEngine:
    """Evaluates conditions, issues actions, records why."""

    def __init__(self, cfg=None, actuator: Callable[[bool], bool] | None = None) -> None:
        self.cfg = cfg or CONFIG.policy
        #: Callable that actually moves the contactor. Left None in advisory
        #: mode and on any deployment where nothing is wired to switch.
        self.actuator = actuator
        self.contactor = ContactorState(last_change_t=time.time(),
                                        _hour_started_t=time.time())
        self.decisions: list[Decision] = []
        #: appliance id -> when it was first seen idle
        self._idle_since: dict[int, float] = {}
        self._idle_energy_avoided_kwh = 0.0

    # ------------------------------------------------------------------ #
    def _record(self, decision: Decision) -> Decision:
        self.decisions.append(decision)
        del self.decisions[:-500]
        return decision

    def _interlock_ok(self, now: float) -> tuple[bool, str]:
        """
        Would the firmware accept a switch right now?

        Mirrors the MCU's rules so we do not send requests that will be
        refused. The authoritative copy lives in the sketch.
        """
        c = self.contactor
        if c.manual_override:
            return False, "manual override is engaged on this circuit"

        if now - c._hour_started_t >= 3600.0:
            c.switches_this_hour = 0
            c._hour_started_t = now
        if c.switches_this_hour >= self.cfg.max_switches_per_hour:
            return False, (f"already switched {c.switches_this_hour} times this "
                           f"hour, limit is {self.cfg.max_switches_per_hour}")

        dwell = now - c.last_change_t
        if c.closed and dwell < self.cfg.min_on_s:
            return False, f"minimum on-time not met ({dwell:.0f}s of {self.cfg.min_on_s}s)"
        if not c.closed and dwell < self.cfg.min_off_s:
            return False, f"minimum off-time not met ({dwell:.0f}s of {self.cfg.min_off_s}s)"
        return True, ""

    def _switch(self, closed: bool, now: float) -> tuple[Outcome, str]:
        """Attempt a state change, honouring advisory mode and the interlock."""
        if self.contactor.closed == closed:
            return Outcome.SKIPPED, "already in the requested state"

        ok, why = self._interlock_ok(now)
        if not ok:
            return Outcome.BLOCKED, why

        if not self.cfg.actuation_enabled or self.actuator is None:
            return Outcome.ADVISED, "advisory mode - nothing was switched"

        if not self.actuator(closed):
            return Outcome.BLOCKED, "the controller refused the command"

        self.contactor.closed = closed
        self.contactor.last_change_t = now
        self.contactor.switches_this_hour += 1
        return Outcome.EXECUTED, ""

    # ------------------------------------------------------------------ #
    def evaluate_idle(self, appliances: list[Any], tariff_rate: float,
                      now: float | None = None) -> list[Decision]:
        """
        Find machines that are on but doing nothing, and cut them.

        "Idle" means running at close to its own standby draw rather than its
        working draw - a compressor holding pressure against no demand, a pump
        circulating for a machine nobody is using. The margin is per-appliance
        rather than absolute because standby scales with machine size.
        """
        now = now if now is not None else time.time()
        if not self.cfg.idle_cutoff_enabled:
            return []

        out: list[Decision] = []
        for app in appliances:
            if getattr(app, "critical", False):
                continue
            if not getattr(app, "is_on", False):
                self._idle_since.pop(app.id, None)
                continue

            # Running far below its characteristic step means it is ticking
            # over rather than working.
            idle_threshold = abs(app.mean_dp) * 0.25 + self.cfg.idle_power_margin_w
            current = abs(getattr(app, "current_w", app.mean_dp))
            if current > idle_threshold:
                self._idle_since.pop(app.id, None)
                continue

            since = self._idle_since.setdefault(app.id, now)
            idle_s = now - since
            if idle_s < self.cfg.idle_timeout_s:
                continue

            wasted_kwh = current * idle_s / 3600.0 / 1000.0
            value_per_h = current / 1000.0 * tariff_rate
            outcome, why = self._switch(False, now)
            reason = (f"{app.label} has been idle for {idle_s/60:.0f} min drawing "
                      f"{current:.0f} W - {wasted_kwh:.2f} kWh wasted so far, "
                      f"about Rs {value_per_h:.2f}/h")
            if why:
                reason += f" ({why})"
            if outcome is Outcome.EXECUTED:
                self._idle_energy_avoided_kwh += wasted_kwh
                self._idle_since.pop(app.id, None)

            out.append(self._record(Decision(
                timestamp=now, kind=ActionKind.IDLE_CUTOFF, outcome=outcome,
                reason=reason, target=app.label, target_id=app.id,
                value_inr_per_h=value_per_h,
                detail={"idle_s": round(idle_s, 1), "current_w": round(current, 1)},
            )))
        return out

    # ------------------------------------------------------------------ #
    def evaluate_demand(self, current_kva: float, limit_kva: float,
                        forecast_kva: float | None = None,
                        now: float | None = None) -> Decision | None:
        """
        Shed deferrable load before a demand ceiling is breached.

        Acting on the forecast rather than the measurement is the whole point:
        by the time the meter shows a breach the billing window has already
        recorded it, and that charge is then paid every month. A demand guard
        that reacts is a demand guard that does not work.
        """
        now = now if now is not None else time.time()
        if not self.cfg.demand_guard_enabled or limit_kva <= 0:
            return None

        threshold = limit_kva * (1.0 - self.cfg.demand_guard_headroom_pct / 100.0)
        projected = max(current_kva, forecast_kva or 0.0)
        if projected < threshold:
            return None

        outcome, why = self._switch(False, now)
        source = "forecast" if (forecast_kva or 0) > current_kva else "measured"
        reason = (f"{source} demand {projected:.2f} kVA is within "
                  f"{self.cfg.demand_guard_headroom_pct:.0f}% of the "
                  f"{limit_kva:.2f} kVA ceiling - shedding deferrable load")
        if why:
            reason += f" ({why})"
        return self._record(Decision(
            timestamp=now, kind=ActionKind.DEMAND_SHED, outcome=outcome,
            reason=reason,
            detail={"current_kva": round(current_kva, 3),
                    "projected_kva": round(projected, 3),
                    "limit_kva": round(limit_kva, 3)},
        ))

    # ------------------------------------------------------------------ #
    def evaluate_anomaly(self, health_states: list[dict[str, Any]],
                         now: float | None = None) -> Decision | None:
        """
        Trip a machine whose electrical signature has gone badly wrong.

        Disabled by default, and deliberately so. Cutting power to a machine
        mid-operation can be more dangerous than the fault - a lathe stopping
        under load with a part in the chuck is its own hazard - so on most
        sites the correct response to a health alarm is to tell a human, not to
        open a contactor. The capability exists for the cases where tripping
        genuinely is safer, and it stays off until someone decides that.
        """
        now = now if now is not None else time.time()
        if not self.cfg.anomaly_trip_enabled:
            return None

        worst = None
        for state in health_states:
            if not state.get("alert_active"):
                continue
            if state.get("smoothed_score", 0.0) < self.cfg.anomaly_trip_score:
                continue
            if worst is None or state["smoothed_score"] > worst["smoothed_score"]:
                worst = state
        if worst is None:
            return None

        outcome, why = self._switch(False, now)
        reason = (f"{worst.get('subject')} health has fallen to "
                  f"{worst.get('health_score', 0):.0f}/100 with a sustained "
                  f"anomaly - tripping for inspection")
        if why:
            reason += f" ({why})"
        return self._record(Decision(
            timestamp=now, kind=ActionKind.ANOMALY_TRIP, outcome=outcome,
            reason=reason, target=str(worst.get("subject")),
            target_id=int(worst.get("appliance_id", 0)), detail=dict(worst),
        ))

    # ------------------------------------------------------------------ #
    def restore(self, reason: str = "conditions cleared",
                now: float | None = None) -> Decision:
        """Close the contactor again once whatever caused the cut has passed."""
        now = now if now is not None else time.time()
        outcome, why = self._switch(True, now)
        return self._record(Decision(
            timestamp=now, kind=ActionKind.RESTORE, outcome=outcome,
            reason=reason + (f" ({why})" if why else ""),
        ))

    def set_manual_override(self, engaged: bool) -> None:
        """
        Hand control back to a human, and keep it there.

        Nothing automatic clears this. A person turned it on and a person turns
        it off.
        """
        self.contactor.manual_override = engaged

    # ------------------------------------------------------------------ #
    @property
    def idle_energy_avoided_kwh(self) -> float:
        return self._idle_energy_avoided_kwh

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return [d.to_dict() for d in self.decisions[-limit:][::-1]]

    def status(self) -> dict[str, Any]:
        c = self.contactor
        ok, why = self._interlock_ok(time.time())
        return {
            "actuation_enabled": self.cfg.actuation_enabled,
            "mode": "active" if self.cfg.actuation_enabled else "advisory",
            "contactor_closed": c.closed,
            "manual_override": c.manual_override,
            "switches_this_hour": c.switches_this_hour,
            "max_switches_per_hour": self.cfg.max_switches_per_hour,
            "interlock_ready": ok,
            "interlock_reason": why,
            "idle_energy_avoided_kwh": round(self._idle_energy_avoided_kwh, 3),
            "decisions_logged": len(self.decisions),
        }
