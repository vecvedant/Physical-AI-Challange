"""
The orchestrator: one acquisition loop, and everything else on a schedule.

Structure
---------
A single thread owns acquisition and every model that has to see each sample in
order - the ring buffer, the edge detector, NILM, health.  Nothing else touches
them.  That is deliberate: these are stateful streaming estimators, and sharing
them across threads would mean either locks in the hot path or a class of bug
that only appears after a week of running.

Everything expensive runs on a timer inside that same loop rather than in its
own thread: retraining, weather refresh, dispatch, rollup, retention.  At 1 Hz
there is an enormous amount of idle time between samples, and a cooperative
schedule is far easier to reason about than five threads contending for one
SQLite connection on a four-core board that also has to serve a dashboard.

The API server reads a snapshot under a lock and never mutates anything.

Failure policy
--------------
No periodic task is allowed to kill the loop.  A weather API that starts
returning HTML, a model that throws on a degenerate window, a full disk - each
of these is logged and skipped.  The one thing this device must not do is stop
measuring, because the historian is the only record the site has.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from typing import Any

import numpy as np

from .config import CONFIG, MODEL_DIR
from .meter.frame import MeterFrame
from .ml.battery import BatterySpec, BatteryState
from .ml.forecast import LoadForecaster
from .ml.health import HealthRegistry
from .ml.nilm import NILMEngine
from .ml.solar import SolarForecaster
from .pipeline.events import EdgeDetector
from .pipeline.features import FeatureExtractor
from .pipeline.ringbuffer import RingBuffer
from .policy.dispatch import DispatchOptimiser, MPCController
from .policy.engine import PolicyEngine
from .store.historian import Historian
from .sustain.accounting import CounterfactualLedger, SustainabilityAccountant
from .sustain.tariff import DemandTracker, TariffSchedule
from .sustain.weather import WeatherClient
from .transport import make_inverter, make_meter

log = logging.getLogger("udyogiq")


class Task:
    """A periodic job that is never allowed to take the loop down with it."""

    def __init__(self, name: str, interval_s: float, fn, *, jitter: float = 0.0,
                 run_at_start: bool = False) -> None:
        self.name = name
        self.interval_s = interval_s
        self.fn = fn
        self.jitter = jitter
        self.last_run = 0.0 if run_at_start else time.time()
        self.runs = 0
        self.failures = 0
        self.last_error = ""
        self.last_duration_ms = 0.0

    def due(self, now: float) -> bool:
        return (now - self.last_run) >= self.interval_s

    def run(self, now: float) -> None:
        self.last_run = now
        t0 = time.perf_counter()
        try:
            self.fn()
            self.runs += 1
        except Exception as exc:                            # noqa: BLE001
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("task %s failed: %s", self.name, self.last_error)
        finally:
            self.last_duration_ms = (time.perf_counter() - t0) * 1000.0

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "interval_s": self.interval_s,
                "runs": self.runs, "failures": self.failures,
                "last_run": self.last_run, "last_error": self.last_error,
                "last_duration_ms": round(self.last_duration_ms, 1)}


class UdyogIQ:
    """Everything, wired together."""

    def __init__(self, transport: str | None = None, *,
                 time_scale: float = 1.0) -> None:
        self.started_t = time.time()

        # --- acquisition --------------------------------------------------
        kwargs = {"time_scale": time_scale} if (transport or CONFIG.meter.transport) == "sim" else {}
        self.meter = make_meter(transport, **kwargs)
        self.inverter = make_inverter(self.meter)

        # --- streaming estimators ----------------------------------------
        self.buffer = RingBuffer(CONFIG.pipeline.buffer_samples)
        self.features = FeatureExtractor()
        self.edges = EdgeDetector()
        self.nilm = NILMEngine()
        self.health = HealthRegistry()

        # --- batch models -------------------------------------------------
        self.load_forecaster = LoadForecaster()
        self.solar_forecaster = SolarForecaster()
        self.weather = WeatherClient()

        # --- economics and control ---------------------------------------
        self.tariff = TariffSchedule.from_config()
        self.demand = DemandTracker(self.tariff)
        self.battery = BatteryState(BatterySpec.from_config(), soc=0.5)
        self.mpc = MPCController(DispatchOptimiser(self.battery.spec, self.tariff))
        self.policy = PolicyEngine(actuator=self._actuate)
        self.accountant = SustainabilityAccountant(self.tariff)
        self.counterfactual = CounterfactualLedger(self.tariff)

        # --- storage ------------------------------------------------------
        self.historian = Historian()

        # --- live state ---------------------------------------------------
        self._lock = threading.Lock()
        self._latest: MeterFrame | None = None
        self._latest_feature = None
        self._solar_w = 0.0
        self._battery_w = 0.0
        self._load_w = 0.0
        self._samples = 0
        self._errors = 0
        self._running = False
        self._thread: threading.Thread | None = None

        self._load_models()
        self.tasks = self._build_tasks()

    # ------------------------------------------------------------------ #
    def _build_tasks(self) -> list[Task]:
        return [
            Task("nilm_consolidate", 900, self._task_consolidate),
            Task("weather_refresh", CONFIG.weather.refresh_minutes * 60,
                 self._task_weather, run_at_start=True),
            Task("dispatch", CONFIG.learning.forecast_block_minutes * 60,
                 self._task_dispatch, run_at_start=True),
            Task("policy", 30, self._task_policy),
            Task("retrain_load", 6 * 3600, self._task_retrain_load),
            Task("retrain_solar", 12 * 3600, self._task_retrain_solar),
            Task("rollup", 300, self._task_rollup),
            Task("retention", 3600, self._task_retention),
            Task("persist_models", 3600, self._task_persist),
        ]

    def _load_models(self) -> None:
        """Warm-start from disk so a restart does not relearn from scratch."""
        if self.load_forecaster.load():
            log.info("loaded load forecaster (%d blocks trained)",
                     self.load_forecaster.n_train)
        if self.solar_forecaster.load():
            log.info("loaded solar correction model")

    # ------------------------------------------------------------------ #
    # Acquisition
    # ------------------------------------------------------------------ #
    def _acquire_once(self) -> None:
        frame = self.meter.read()
        self._samples += 1
        if not frame.valid:
            self._errors += 1

        self.buffer.append(frame)

        # Source-side telemetry, measured or estimated.
        if hasattr(self.inverter, "update"):
            self.inverter.update(grid_w=frame.active_power_w,
                                 timestamp=frame.timestamp)
        telemetry = self.inverter.read()
        solar_w = float(telemetry.get("solar_w", 0.0))
        battery_w = float(telemetry.get("battery_w", 0.0))
        soc = float(telemetry.get("soc", self.battery.soc))
        load_w = float(telemetry.get("load_w",
                                     frame.active_power_w + solar_w + battery_w))

        # Switching events feed NILM, and NILM feeds per-machine health.
        #
        # Disaggregation runs on the *load* signal, not on what the meter reads.
        # With the meter at the grid tie the reading is load minus generation,
        # and solar moves on its own: a cloud crossing looks exactly like a
        # machine switching, and a machine that starts while irradiance is
        # falling registers a step of the wrong size. Measured over a week of
        # replay, that produced six clusters spanning 1243-1828 W where only two
        # machines existed, against clean separation on the same plant metered
        # load-side.
        #
        # Adding generation back recovers the load-side view. It is only as good
        # as the telemetry: exact with a Modbus inverter, approximate when
        # EstimatedInverter is dead-reckoning, which is one more reason to
        # prefer real telemetry or load-side placement.
        edge = self.edges.push(self._load_frame(frame, solar_w, battery_w))
        if edge is not None:
            appliance = self.nilm.push(edge)
            if appliance is not None:
                self.historian.add_event(edge, appliance.id)
                if appliance.confirmed:
                    self.health.push(appliance.id, appliance.label, edge)

        feature = self.features.extract(self.buffer)
        self.demand.push(frame.timestamp, frame.apparent_power_va)

        self.accountant.update(
            timestamp=frame.timestamp, grid_w=frame.active_power_w,
            load_w=load_w, solar_w=solar_w, battery_w=battery_w,
            idle_w=self._idle_power(),
            degradation_inr_per_kwh=self.battery.spec.degradation_inr_per_kwh)
        self.counterfactual.update(
            timestamp=frame.timestamp, load_w=load_w, solar_w=solar_w,
            battery_w=battery_w, idle_w=self._idle_power(),
            power_factor=frame.power_factor)

        # Feed measured generation back so climatology keeps improving even
        # when the weather API has been unreachable for weeks.
        if solar_w > 0:
            self.weather.observe_generation(frame.timestamp, solar_w)

        self.historian.add_sample(frame, solar_w=solar_w,
                                  battery_w=battery_w, soc=soc)

        with self._lock:
            self._latest = frame
            self._latest_feature = feature
            self._solar_w, self._battery_w, self._load_w = solar_w, battery_w, load_w
            self.battery.soc = soc

    def _load_frame(self, frame: MeterFrame, solar_w: float,
                    battery_w: float) -> MeterFrame:
        """
        The frame as it would look with the meter on the load side.

        Returns the original untouched when the meter already sits load-side or
        when there is no generation to add back, so the common case costs
        nothing and nothing is fabricated.
        """
        if CONFIG.meter.meter_position == "load_side":
            return frame
        if abs(solar_w) < 1.0 and abs(battery_w) < 1.0:
            return frame
        blob = frame.to_dict()
        blob["active_power_w"] = frame.active_power_w + solar_w + battery_w
        return MeterFrame(**blob)

    def _idle_power(self) -> float:
        """Power currently going to machines that are on but not working."""
        total = 0.0
        for app in self.nilm.appliances:
            if app.is_on and not getattr(app, "critical", False):
                threshold = abs(app.mean_dp) * 0.25
                if abs(app.mean_dp) <= threshold:
                    total += abs(app.mean_dp)
        return total

    # ------------------------------------------------------------------ #
    # Periodic tasks
    # ------------------------------------------------------------------ #
    def _task_consolidate(self) -> None:
        merged = self.nilm.consolidate(force=True)
        for app in self.nilm.confirmed_appliances():
            state = next((s for s in self.health.states()
                          if s.get("appliance_id") == app.id), {})
            self.historian.upsert_appliance(app, state.get("health_score", 100.0))
        if merged:
            log.info("NILM consolidation merged or dropped %d clusters", merged)

    def _task_weather(self) -> None:
        self.weather.refresh()

    def _task_dispatch(self) -> None:
        """Re-solve the schedule and hand the first action to the battery."""
        now = time.time()
        blocks = 96

        ts, pw = self.historian.training_series(days=14, now=now)
        if self.load_forecaster.trained and len(ts) > 200:
            load_profile = self.load_forecaster.profile(
                np.asarray(ts), np.asarray(pw), blocks)
        else:
            # Before the forecaster has enough history, plan against the
            # current load held flat. Crude, but it keeps the demand guard and
            # the solar storage logic working from day one instead of leaving
            # the site unoptimised for a fortnight.
            with self._lock:
                current = self._load_w
            load_profile = np.full(blocks, max(current, 0.0))

        forecast = self.weather.refresh()
        solar_profile = self.solar_forecaster.profile(forecast, now, blocks)

        action = self.mpc.step(soc_now=self.battery.soc,
                               load_profile=load_profile,
                               solar_profile=solar_profile,
                               now=now, force=True)
        if action is None:
            return
        if self.inverter.controllable and CONFIG.policy.actuation_enabled:
            self.inverter.command_battery(action.battery_w)

    def _task_policy(self) -> None:
        now = time.time()
        rate = self.tariff.rate(now)
        decisions = self.policy.evaluate_idle(
            self.nilm.confirmed_appliances(), rate, now=now)

        forecast_kva = None
        if self.mpc.plan and self.mpc.plan.actions:
            nxt = self.mpc.plan.actions[0]
            pf = max(0.5, abs(self._latest.power_factor) if self._latest else 0.9)
            forecast_kva = abs(nxt.expected_grid_w) / pf / 1000.0

        demand_decision = self.policy.evaluate_demand(
            self.demand.current_average_kva(),
            self.tariff.sanctioned_demand_kva,
            forecast_kva=forecast_kva, now=now)
        if demand_decision:
            decisions.append(demand_decision)

        anomaly = self.policy.evaluate_anomaly(self.health.states(), now=now)
        if anomaly:
            decisions.append(anomaly)

        for d in decisions:
            self.historian.add_decision(d)

    def _task_retrain_load(self) -> None:
        ts, pw = self.historian.training_series(days=21)
        if len(ts) < 200:
            return
        if self.load_forecaster.fit(np.asarray(ts), np.asarray(pw)):
            self.load_forecaster.save()
            log.info("load forecaster retrained on %d blocks",
                     self.load_forecaster.n_train)

    def _task_retrain_solar(self) -> None:
        """
        Fit the solar correction against measured generation.

        Needs both a weather history and a generation history to line up, which
        only exists once the node has been running for a few days with the
        array producing.
        """
        forecast = self.weather.refresh()
        if not forecast.points:
            return
        samples = []
        for row in self.historian.minute_series(hours=72):
            t = row["minute"] * 60.0
            wx = forecast.at(t)
            if wx and row.get("solar_w"):
                samples.append((t, wx, float(row["solar_w"])))
        if self.solar_forecaster.fit(samples):
            self.solar_forecaster.save()
            log.info("solar correction retrained on %d daylight samples",
                     self.solar_forecaster.n_train)

    def _task_rollup(self) -> None:
        self.historian.rollup()

    def _task_retention(self) -> None:
        self.historian.enforce_retention()

    def _task_persist(self) -> None:
        saved = self.health.save_all(MODEL_DIR)
        self.historian.set_meta("last_persist_t", time.time())
        if saved:
            log.debug("persisted %d health models", saved)

    # ------------------------------------------------------------------ #
    def _actuate(self, closed: bool) -> bool:
        """Ask the controller to move the contactor. False if it declined."""
        setter = getattr(self.meter, "set_contactor", None)
        if setter is None:
            return False
        try:
            return bool(setter(closed))
        except Exception as exc:                            # noqa: BLE001
            log.warning("contactor command failed: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    # Warm start
    # ------------------------------------------------------------------ #
    def warmup(self, days: float = 7.0, step_s: float = 1.0,
               progress: bool = True) -> dict[str, Any]:
        """
        Run the pipeline over simulated history as fast as the CPU allows.

        Needed because the models learn from *sample sequence*, not from
        elapsed time. Speeding up the simulator's clock does not help: the
        acquisition loop still runs at 1 Hz wall clock, so a 600x time scale
        just puts ten simulated minutes between consecutive samples, and an
        edge detector handed a ten-minute gap sees no switching events at all.
        Everything downstream - NILM, health, the forecaster - then has nothing
        to learn from.

        So this walks the simulated plant one second at a time with no waiting,
        pushing samples through the identical code path the live loop uses. A
        week of plant history takes a few seconds of CPU, and the node comes up
        already knowing the machines instead of spending its first fortnight
        looking ignorant on a dashboard.

        Only available on the simulated transport, for obvious reasons.
        """
        if self.meter.name != "sim":
            raise RuntimeError("warmup only works with the simulated transport")

        from sim.plant import Plant

        plant = Plant(meter_position=self.meter.plant.meter_position,
                      seed=self.meter.plant.seed)
        start_t = time.time() - days * 86400
        total = int(days * 86400 / step_s)
        t0 = time.perf_counter()

        for i in range(total):
            t = start_t + i * step_s
            sample = plant.sample(t)
            solar_w = sample.pop("_solar_w", 0.0)
            battery_w = sample.pop("_battery_w", 0.0)
            soc = sample.pop("_soc", 0.5)
            for key in [k for k in sample if k.startswith("_")]:
                sample.pop(key)

            frame = MeterFrame(**sample, source="sim")
            self.buffer.append(frame)
            edge = self.edges.push(frame)
            if edge is not None:
                appliance = self.nilm.push(edge)
                if appliance is not None and appliance.confirmed:
                    self.health.push(appliance.id, appliance.label, edge)
            self.demand.push(frame.timestamp, frame.apparent_power_va)
            self.historian.add_sample(frame, solar_w=solar_w,
                                      battery_w=battery_w, soc=soc)

            if i % 3600 == 0:
                self.nilm.consolidate(now=t)
                if progress and i and i % 86400 == 0:
                    log.info("warmup: %d/%d days, %d appliances discovered",
                             i // 86400, int(days),
                             len(self.nilm.confirmed_appliances()))

        self.nilm.consolidate(now=start_t + days * 86400, force=True)
        self.historian.flush()
        self.historian.rollup(force=True)

        ts, pw = self.historian.training_series(days=days + 1)
        if len(ts) > 200:
            if self.load_forecaster.fit(np.asarray(ts), np.asarray(pw)):
                self.load_forecaster.save()

        self._task_consolidate()
        self.health.save_all(MODEL_DIR)

        summary = {
            "days": days,
            "samples": total,
            "seconds": round(time.perf_counter() - t0, 1),
            "appliances": len(self.nilm.confirmed_appliances()),
            "health_models": sum(1 for m in self.health._monitors.values()
                                 if m.model.trained),
            "forecaster_trained": self.load_forecaster.trained,
        }
        log.info("warmup complete: %(days)s days in %(seconds)ss, "
                 "%(appliances)d appliances, %(health_models)d health models",
                 summary)
        return summary

    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        period = 1.0 / max(CONFIG.meter.sample_hz, 0.1)
        next_sample = time.time()
        while self._running:
            now = time.time()
            if now >= next_sample:
                try:
                    self._acquire_once()
                except Exception as exc:                    # noqa: BLE001
                    self._errors += 1
                    log.warning("acquisition failed: %s", exc)
                # Schedule from the intended time, not from now, so a slow
                # sample does not permanently shift the cadence.
                next_sample += period
                if next_sample < now - period:
                    next_sample = now + period

            for task in self.tasks:
                if task.due(now):
                    task.run(now)
                    break      # at most one heavy task per tick

            sleep_for = max(0.0, min(next_sample - time.time(), 0.25))
            if sleep_for:
                time.sleep(sleep_for)

    def start(self) -> None:
        if self._running:
            return
        self.meter.open()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="udyogiq-acq",
                                        daemon=True)
        self._thread.start()
        log.info("Udyog IQ running: transport=%s, mode=%s",
                 self.meter.name, self.policy.status()["mode"])

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        self.historian.flush()
        self.historian.close()
        self.meter.close()

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """Everything the dashboard needs, in one consistent read."""
        with self._lock:
            frame = self._latest
            feature = self._latest_feature
            solar_w, battery_w, load_w = self._solar_w, self._battery_w, self._load_w

        now = time.time()
        plan = self.mpc.plan
        return {
            "now": now,
            "site": CONFIG.site_name,
            "uptime_s": round(now - self.started_t, 1),
            "live": {
                "timestamp": frame.timestamp if frame else 0.0,
                "grid_w": round(frame.active_power_w, 1) if frame else 0.0,
                "load_w": round(load_w, 1),
                "solar_w": round(solar_w, 1),
                "battery_w": round(battery_w, 1),
                "voltage_v": round(frame.voltage_v, 1) if frame else 0.0,
                "current_a": round(frame.current_a, 2) if frame else 0.0,
                "power_factor": round(frame.power_factor, 3) if frame else 0.0,
                "frequency_hz": round(frame.frequency_hz, 2) if frame else 0.0,
                "soc": round(self.battery.soc, 4),
                "valid": frame.valid if frame else False,
                "source": frame.source if frame else "none",
            },
            "acquisition": {
                "samples": self._samples,
                "errors": self._errors,
                "transport": self.meter.name,
                "success_rate": round(self.meter.health.success_rate, 4),
                "online": self.meter.health.online,
                "last_error": self.meter.health.last_error,
            },
            "nilm": self.nilm.snapshot(),
            "residual_w": round(
                self.nilm.residual(frame.active_power_w) if frame else 0.0, 1),
            "health": self.health.states(),
            "demand": self.demand.status(),
            "tariff": self.tariff.describe(now),
            "battery": self.battery.status(),
            "dispatch": plan.to_dict(limit=96) if plan else None,
            "policy": self.policy.status(),
            "decisions": self.policy.recent(20),
            "accounting": self.accountant.snapshot(),
            "savings": self.counterfactual.snapshot(),
            "weather": self.weather.status(),
            "solar_model": self.solar_forecaster.status(),
            "forecast": {
                "trained": self.load_forecaster.trained,
                "n_train": self.load_forecaster.n_train,
            },
            "features": feature.to_dict() if feature else None,
            "tasks": [t.status() for t in self.tasks],
            "storage": self.historian.stats(),
        }


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="udyogiq",
        description="Udyog IQ - edge AI energy intelligence for small industry")
    parser.add_argument("--transport", choices=("sim", "serial", "bridge"),
                        default=None, help="meter backend (default: from config)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--time-scale", type=float, default=1.0,
                        help="simulator speed multiplier (sim transport only)")
    parser.add_argument("--no-server", action="store_true",
                        help="run acquisition only, without the dashboard")
    parser.add_argument("--warmup", type=float, default=0.0, metavar="DAYS",
                        help="replay this many days of simulated plant history "
                             "before going live, so the models start trained "
                             "(simulated transport only)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    node = UdyogIQ(args.transport, time_scale=args.time_scale)
    if args.warmup > 0:
        node.warmup(days=args.warmup)
    node.start()

    if args.no_server:
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            node.stop()
        return 0

    from .api.server import serve
    try:
        serve(node, host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
