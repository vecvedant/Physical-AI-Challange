"""
Online change-point detection: turning a power trace into switching events.

This is the front half of NILM.  A machine switching on or off leaves a step in
active power and, crucially, a *simultaneous* step in reactive power.  The pair
(dP, dQ) is close to a fingerprint: two motors that draw the same real power
usually differ in reactive draw, because their magnetising current differs.

The detector is a three-state machine rather than a filter, because what we
need out of it is a clean list of discrete events with accurate before/after
levels, not a smoothed signal:

    STEADY ---- deviation exceeds threshold ----> TRANSITION
    TRANSITION -- level holds for settle_s ----> STEADY, emit Edge
    TRANSITION -- returns to origin -----------> STEADY, emit nothing

The third arm matters more than it looks.  A momentary inrush, a welder strike,
or a dropped Modbus frame all produce a deviation that comes straight back.
Emitting those as edges would fill the appliance list with phantom machines,
which is the classic way naive NILM implementations fail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

import numpy as np

from ..config import CONFIG
from ..meter.frame import MeterFrame


class EdgeType(str, Enum):
    RISE = "rise"
    FALL = "fall"


@dataclass(slots=True)
class Edge:
    """A confirmed switching event."""

    timestamp: float
    type: EdgeType
    delta_p: float           # W, signed
    delta_q: float           # VAr, signed
    pre_level_p: float
    post_level_p: float
    #: Peak power seen during the transition, above the settled level.  For a
    #: motor this is the inrush, and its growth over months is one of the few
    #: real predictive signals available at this sampling rate.
    peak_p: float = 0.0
    #: Seconds the transition took to settle.
    duration_s: float = 0.0

    @property
    def magnitude(self) -> float:
        return abs(self.delta_p)

    @property
    def inrush_ratio(self) -> float:
        """Peak over settled step size.  ~1 for resistive, >2 for a motor."""
        if abs(self.delta_p) < 1e-6:
            return 1.0
        return max(1.0, self.peak_p / abs(self.delta_p))

    def signature(self) -> np.ndarray:
        """(dP, dQ) - the vector NILM clusters on."""
        return np.array([self.delta_p, self.delta_q], dtype=np.float64)

    def to_dict(self) -> dict[str, Any]:
        blob = asdict(self)
        blob["type"] = self.type.value
        blob["inrush_ratio"] = self.inrush_ratio
        return blob


class _State(str, Enum):
    STEADY = "steady"
    TRANSITION = "transition"


class EdgeDetector:
    """Streaming detector; feed it frames, collect edges."""

    def __init__(self, cfg=None) -> None:
        self.cfg = cfg or CONFIG.pipeline
        self.state = _State.STEADY

        # Rolling estimate of the current steady level.  A median over a short
        # tail rather than a mean, so a single spike cannot drag the baseline.
        self._recent_p: list[float] = []
        self._recent_q: list[float] = []
        self._tail = 8

        self._steady_p = 0.0
        self._steady_q = 0.0
        self._have_steady = False

        # Running estimate of how much the signal wobbles while nothing is
        # actually switching.  Scaled to a standard deviation from the mean
        # absolute deviation (sigma ~ 1.2533 * MAD for Gaussian noise) and
        # updated only while STEADY, so a real transition never inflates it.
        self._noise = 0.0

        # Transition bookkeeping.
        self._t_start = 0.0
        self._t_peak = 0.0
        self._candidate_p: list[float] = []
        self._candidate_q: list[float] = []

        self.edges: list[Edge] = []
        self._max_edges = 5000

    # ------------------------------------------------------------------ #
    def _median(self, values: list[float]) -> float:
        return float(np.median(values)) if values else 0.0

    def threshold(self) -> float:
        """
        Current detection threshold in watts.

        Adaptive by necessity. A 340 W fan switching at 02:00 against a quiet
        baseline and a 40 W step hiding under a running lathe are both real
        events, but no single fixed number catches one without drowning in the
        other. Taking the larger of an absolute floor and a multiple of the
        measured local noise makes the detector as sensitive as the current
        conditions actually allow.
        """
        return max(self.cfg.edge_threshold_w,
                   self.cfg.edge_sigma_multiplier * self._noise)

    def push(self, frame: MeterFrame) -> Edge | None:
        """Feed one sample.  Returns an Edge when one is confirmed."""
        if not frame.valid:
            # A hold-last-value frame carries no new information about the
            # level; folding it in would fake stability during an outage.
            return None

        p = frame.active_power_w
        q = frame.reactive_power_var
        now = frame.timestamp or time.time()

        self._recent_p.append(p)
        self._recent_q.append(q)
        del self._recent_p[:-self._tail]
        del self._recent_q[:-self._tail]

        if not self._have_steady:
            if len(self._recent_p) >= self._tail:
                self._steady_p = self._median(self._recent_p)
                self._steady_q = self._median(self._recent_q)
                # Seed the noise estimate from the warm-up tail, otherwise the
                # first minute runs at the absolute floor and emits a burst of
                # phantom edges before the EWMA catches up.
                self._noise = float(np.std(self._recent_p))
                self._have_steady = True
            return None

        deviation = p - self._steady_p

        if self.state is _State.STEADY:
            if abs(deviation) >= self.threshold():
                self.state = _State.TRANSITION
                self._t_start = now
                self._t_peak = abs(deviation)
                self._candidate_p = [p]
                self._candidate_q = [q]
            else:
                # Only sub-threshold movement counts as noise, so a real step
                # cannot teach the detector to ignore steps of its own size.
                alpha = self.cfg.noise_ewma_alpha
                self._noise = (1 - alpha) * self._noise + alpha * abs(deviation) * 1.2533
                # Let the steady level track slow drift, but only slowly, so a
                # genuine ramp does not get absorbed as "normal".
                self._steady_p += 0.02 * deviation
                self._steady_q += 0.02 * (q - self._steady_q)
            return None

        # --- in transition ---------------------------------------------- #
        self._candidate_p.append(p)
        self._candidate_q.append(q)
        self._t_peak = max(self._t_peak, abs(p - self._steady_p))

        elapsed = now - self._t_start
        if elapsed < self.cfg.settle_s:
            return None

        # Has the new level held? Judge on the settled tail, not the whole
        # transition, so the inrush does not contaminate the step size.
        tail_p = self._candidate_p[-max(2, self.cfg.settle_s):]
        tail_q = self._candidate_q[-max(2, self.cfg.settle_s):]
        new_p = self._median(tail_p)
        new_q = self._median(tail_q)
        step = new_p - self._steady_p

        if abs(step) < self.threshold():
            # Came back to where it started: a transient, not a switch event.
            self.state = _State.STEADY
            self._candidate_p.clear()
            self._candidate_q.clear()
            return None

        spread = float(np.std(tail_p)) if len(tail_p) >= 2 else 0.0
        if spread > max(self.threshold(), 0.25 * abs(step)):
            # Still moving - keep waiting rather than emitting a level we do
            # not believe. Bounded by the caller's sample rate in practice.
            return None

        edge = Edge(
            timestamp=self._t_start,
            type=EdgeType.RISE if step > 0 else EdgeType.FALL,
            delta_p=step,
            delta_q=new_q - self._steady_q,
            pre_level_p=self._steady_p,
            post_level_p=new_p,
            peak_p=self._t_peak,
            duration_s=elapsed,
        )

        self._steady_p = new_p
        self._steady_q = new_q
        self.state = _State.STEADY
        self._candidate_p.clear()
        self._candidate_q.clear()

        self.edges.append(edge)
        del self.edges[:-self._max_edges]
        return edge

    # ------------------------------------------------------------------ #
    @property
    def steady_level(self) -> tuple[float, float]:
        return self._steady_p, self._steady_q

    def recent(self, seconds: float, now: float | None = None) -> list[Edge]:
        now = now if now is not None else time.time()
        return [e for e in self.edges if e.timestamp >= now - seconds]

    def reset(self) -> None:
        self.__init__(self.cfg)  # noqa: PLC2801 - deliberate full reset
