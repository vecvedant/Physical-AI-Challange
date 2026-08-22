"""
Regression tests for the things that broke during development.

Every test here corresponds to a bug that was actually found by measurement
rather than by reading the code, which is the only reason to keep it: a test
that has never failed is documentation, a test that caught something is a
guard.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from sim.plant import Plant
from udyogiq.meter.frame import MeterFrame
from udyogiq.meter.selec_em2m import BLOCK_LENGTH, decode_block, decode_float, encode_float
from udyogiq.ml.battery import BatterySpec, BatteryState
from udyogiq.ml.nilm import NILMEngine
from udyogiq.pipeline.events import EdgeDetector, EdgeType
from udyogiq.pipeline.ringbuffer import RingBuffer
from udyogiq.policy.dispatch import DispatchOptimiser
from udyogiq.policy.engine import Outcome, PolicyEngine
from udyogiq.sustain.tariff import DemandTracker, TariffSchedule


# --------------------------------------------------------------------------- #
# Meter decoding
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("word_order", ["big", "little"])
@pytest.mark.parametrize("value", [0.0, 230.7, -1450.25, 49.97, 1e-4])
def test_float_roundtrip(value, word_order):
    high, low = encode_float(value, word_order=word_order)
    assert decode_float(high, low, word_order=word_order) == pytest.approx(value, rel=1e-6)


def test_sanity_check_rejects_wrong_word_order():
    """A swapped word order must not decode into something believable."""
    regs = [0] * BLOCK_LENGTH
    for addr, val in ((30009, 1450.0), (30011, 620.0), (30013, 1577.0),
                      (30015, 231.4), (30017, 6.81), (30019, 0.919)):
        off = addr - 30001
        regs[off], regs[off + 1] = encode_float(val, word_order="big")

    assert decode_block(regs, word_order="big").sanity_check() == []
    assert decode_block(regs, word_order="little").sanity_check() != []


# --------------------------------------------------------------------------- #
# Ring buffer
# --------------------------------------------------------------------------- #
def test_ringbuffer_wraps_without_losing_order():
    buf = RingBuffer(capacity=10)
    for i in range(25):
        buf.append(MeterFrame(timestamp=float(i), active_power_w=float(i)))
    series = buf.column("active_power_w")
    assert len(buf) == 10
    assert series[0] == 15.0 and series[-1] == 24.0
    assert list(series) == sorted(series), "wrapped buffer returned out of order"


def test_window_selects_by_time_not_sample_count():
    """A stretched poll interval must not silently widen the window."""
    buf = RingBuffer(capacity=100)
    for i in range(10):
        buf.append(MeterFrame(timestamp=float(i), active_power_w=1.0))
    for i in range(10, 20):
        buf.append(MeterFrame(timestamp=10.0 + (i - 10) * 5.0, active_power_w=2.0))
    assert len(buf.window(10.0, "active_power_w")) == 3


# --------------------------------------------------------------------------- #
# Edge detection
# --------------------------------------------------------------------------- #
def _feed(detector, values, start_t=0.0):
    edges = []
    for i, v in enumerate(values):
        e = detector.push(MeterFrame(timestamp=start_t + i, active_power_w=v,
                                     reactive_power_var=v * 0.5))
        if e:
            edges.append(e)
    return edges


def test_step_is_detected_with_correct_magnitude():
    edges = _feed(EdgeDetector(), [100.0] * 30 + [700.0] * 30)
    assert len(edges) == 1
    assert edges[0].type is EdgeType.RISE
    assert edges[0].delta_p == pytest.approx(600.0, abs=30.0)


def test_transient_does_not_emit_an_edge():
    """A spike that returns must not invent a machine."""
    assert _feed(EdgeDetector(), [100.0] * 30 + [900.0] * 2 + [100.0] * 30) == []


def test_threshold_adapts_to_noise():
    """
    A fixed threshold produced 479 edges against 71 real switches, because a
    2.2 kW load with 6% ripple swings far past any sensible fixed value.
    """
    rng = np.random.default_rng(0)
    noisy = list(2200.0 + rng.normal(0, 130, 400))
    detector = EdgeDetector()
    edges = _feed(detector, noisy)
    assert len(edges) <= 3, f"noise produced {len(edges)} phantom edges"
    assert detector.threshold() > detector.cfg.edge_threshold_w


# --------------------------------------------------------------------------- #
# NILM
# --------------------------------------------------------------------------- #
def test_nilm_recovers_machines_from_aggregate():
    """
    End to end against simulator ground truth.

    Five days, not four. Swept across seeds, four days recovers the compressor,
    pump, grinder and office load but not reliably the lathe - it runs on a
    shift schedule and simply has not started often enough to confirm. From
    five days on, all five are found on every seed tried. That interval is a
    property of the method rather than of this test: with no labels, a machine
    is only knowable once it has switched enough times to form a cluster, so
    rarely-used machines take proportionally longer to appear.
    """
    plant = Plant(meter_position="load_side", seed=3)
    detector, nilm = EdgeDetector(), NILMEngine()
    t0 = 1787000000
    for i in range(5 * 86400):
        s = plant.sample(t0 + i)
        for k in [k for k in s if k.startswith("_")]:
            s.pop(k)
        e = detector.push(MeterFrame(**s, source="sim"))
        if e:
            nilm.push(e)
        if i % 3600 == 0:
            nilm.consolidate(now=t0 + i)
    nilm.consolidate(now=t0 + 5 * 86400, force=True)

    found = nilm.confirmed_appliances()
    assert found, "discovered nothing at all"
    # compressor, pump, lathe, grinder, office load
    for target in (1500.0, 1350.0, 2200.0, 750.0, 180.0):
        assert any(abs(abs(a.mean_dp) - target) / target < 0.12 for a in found), \
            f"failed to recover the {target:.0f} W machine"


def test_stale_on_state_is_expired():
    """
    A missed stop event left a machine believed-on forever, which drove the
    residual to -3781 W and inflated every runtime total.
    """
    nilm = NILMEngine()
    from udyogiq.pipeline.events import Edge
    now = 1787000000.0
    for i in range(6):
        nilm.push(Edge(timestamp=now + i * 100, type=EdgeType.RISE, delta_p=1500.0,
                       delta_q=1000.0, pre_level_p=0.0, post_level_p=1500.0))
        nilm.push(Edge(timestamp=now + i * 100 + 50, type=EdgeType.FALL, delta_p=-1500.0,
                       delta_q=-1000.0, pre_level_p=1500.0, post_level_p=0.0))
    nilm.push(Edge(timestamp=now + 1000, type=EdgeType.RISE, delta_p=1500.0,
                   delta_q=1000.0, pre_level_p=0.0, post_level_p=1500.0))
    assert any(a.is_on for a in nilm.appliances)
    nilm.expire_stale(now + 1000 + 48 * 3600)
    assert not any(a.is_on for a in nilm.appliances)


# --------------------------------------------------------------------------- #
# Battery and dispatch
# --------------------------------------------------------------------------- #
def test_battery_respects_soc_floor():
    state = BatteryState(BatterySpec.from_config(), soc=0.21)
    delivered = state.apply(1500.0, 3600.0)
    assert delivered < 1500.0
    assert state.soc >= state.spec.soc_floor - 1e-6


def test_degradation_cost_is_material():
    """If this ever returns ~0 the optimiser will cycle the battery to death."""
    assert BatterySpec.from_config().degradation_inr_per_kwh > 1.0


def test_dispatch_never_loses_to_doing_nothing():
    """
    Doing nothing is always in the action set, so the plan can never be worse
    than the baseline. It reported -23% once, because stored energy was
    optimised for but excluded from the reported cost.
    """
    hours = np.arange(96) * 0.25
    load = 400 + 2600 * np.exp(-((hours - 13) ** 2) / 18)
    solar = np.zeros(96)
    day = (hours > 6.3) & (hours < 18.7)
    solar[day] = 2400 * np.sin(np.pi * (hours[day] - 6.3) / 12.4) ** 1.3

    plan = DispatchOptimiser().solve(soc_now=0.5, load_profile=load,
                                     solar_profile=solar, start_t=time.time())
    assert plan.horizon_blocks == 96
    assert plan.saving_inr >= -1e-6, f"plan lost money: {plan.saving_inr}"
    assert plan.solve_ms < 500.0


def test_dispatch_shaves_a_demand_spike():
    load = np.full(96, 400.0)
    load[40:48] = 4800.0
    plan = DispatchOptimiser().solve(soc_now=0.9, load_profile=load,
                                     solar_profile=np.zeros(96),
                                     start_t=time.time(), demand_limit_w=3000.0)
    assert plan.saving_inr > 0.0
    assert any(a.battery_w > 0 for a in plan.actions[40:48]), \
        "battery did not discharge into the spike"


# --------------------------------------------------------------------------- #
# Tariff and demand
# --------------------------------------------------------------------------- #
def test_demand_tracker_ignores_a_partial_window():
    """Otherwise the first samples after a restart invent a billing peak."""
    tracker = DemandTracker()
    now = time.time()
    for i in range(10):
        tracker.push(now + i, 9000.0)
    assert tracker.billing_peak_kva == 0.0


def test_demand_is_averaged_not_instantaneous():
    tracker = DemandTracker()
    now = time.time()
    window = tracker.window_seconds
    for i in range(int(window) + 60):
        # One brief spike inside an otherwise quiet window.
        tracker.push(now + i, 20000.0 if i == 100 else 1000.0)
    assert tracker.billing_peak_kva < 2.0, "a two-second spike set the billing peak"


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
class _App:
    def __init__(self, id, label, mean_dp, current_w, critical=False):
        self.id, self.label, self.mean_dp = id, label, mean_dp
        self.current_w, self.critical, self.is_on = current_w, critical, True


def test_advisory_mode_never_switches():
    switched = []
    engine = PolicyEngine(actuator=lambda c: switched.append(c) or True)
    engine.cfg.actuation_enabled = False
    apps = [_App(1, "Compressor", 1500, 100)]
    now = time.time()
    engine.evaluate_idle(apps, 9.0, now=now)
    decisions = engine.evaluate_idle(apps, 9.0, now=now + 2000)
    assert decisions and decisions[0].outcome is Outcome.ADVISED
    assert switched == [], "advisory mode actuated the contactor"


def test_critical_loads_are_never_shed():
    engine = PolicyEngine()
    engine.cfg.actuation_enabled = False
    apps = [_App(2, "Server", 180, 10, critical=True)]
    now = time.time()
    engine.evaluate_idle(apps, 9.0, now=now)
    assert engine.evaluate_idle(apps, 9.0, now=now + 5000) == []


def test_interlock_blocks_rapid_switching():
    engine = PolicyEngine(actuator=lambda c: True)
    engine.cfg.actuation_enabled = True
    now = time.time()
    apps = [_App(1, "Compressor", 1500, 100)]
    engine.evaluate_idle(apps, 9.0, now=now)
    engine.evaluate_idle(apps, 9.0, now=now + 2000)
    decision = engine.restore("test", now=now + 2001)
    assert decision.outcome is Outcome.BLOCKED
    engine.cfg.actuation_enabled = False
