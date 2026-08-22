"""
Windowed feature extraction.

Everything the learning layer sees comes from here.  The design constraint that
shaped this file: at ~1 Hz we can never see a current waveform, so no feature
here pretends to.  There is no spectrum, no harmonic order, no MCSA.  What we
can see is how aggregate quantities move against each other over seconds and
minutes, and it turns out that carries a surprising amount of the signal.

Three windows, because the phenomena live at different timescales:

  * 10 s  - transients: inrush, step edges, momentary overload
  * 60 s  - the working state of a machine: cutting, idling, warming up
  * 300 s - drift: the slow degradation a health model is meant to catch

Two features deserve a note because they are not standard:

  ``distortion_ratio``   how far apparent power exceeds what P and Q explain.
                         For a clean sinusoid S^2 = P^2 + Q^2, so the excess is
                         distortion power - a cheap proxy for harmonic content
                         that costs no extra sampling rate.

  ``sag_coupling``       regression slope of voltage against active power. A
                         stiff supply barely moves; a loaded feeder or a
                         failing connection sags harder for the same current.
                         Changes in this slope have caught loose terminals in
                         the field, and it is free to compute.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any

import numpy as np

from ..config import CONFIG
from .ringbuffer import RingBuffer


@dataclass(slots=True)
class FeatureVector:
    """One window's worth of derived quantities."""

    timestamp: float = 0.0

    # Level and spread, per window.
    p_mean_short: float = 0.0
    p_mean_med: float = 0.0
    p_mean_long: float = 0.0
    p_std_short: float = 0.0
    p_std_med: float = 0.0
    p_min_med: float = 0.0
    p_max_med: float = 0.0
    p_range_med: float = 0.0

    q_mean_med: float = 0.0
    q_over_p: float = 0.0

    v_mean_med: float = 0.0
    v_std_med: float = 0.0
    i_mean_med: float = 0.0
    i_crest_med: float = 0.0

    pf_mean_med: float = 0.0
    pf_std_med: float = 0.0
    pf_slope_long: float = 0.0

    freq_mean_med: float = 0.0
    freq_std_med: float = 0.0

    # Shape and dynamics.
    dp_dt_abs_mean: float = 0.0
    dp_dt_max: float = 0.0
    distortion_mean: float = 0.0
    sag_coupling: float = 0.0
    duty_cycle_long: float = 0.0
    p_slope_long: float = 0.0

    # Cyclic time encoding, so a model can learn shift patterns without
    # treating 23:59 and 00:01 as far apart.
    hour_sin: float = 0.0
    hour_cos: float = 0.0
    dow_sin: float = 0.0
    dow_cos: float = 0.0

    n_samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_array(self, names: tuple[str, ...] | None = None) -> np.ndarray:
        names = names or MODEL_FEATURES
        return np.array([getattr(self, n) for n in names], dtype=np.float64)


#: Subset fed to the health model.  Deliberately excludes absolute time and
#: sample count: a model that learns "anomalies happen at 3pm" has learned the
#: shift roster, not the machine.
MODEL_FEATURES: tuple[str, ...] = (
    "p_mean_short", "p_mean_med", "p_std_short", "p_std_med",
    "p_range_med", "q_over_p",
    "v_std_med", "i_crest_med",
    "pf_mean_med", "pf_std_med",
    "dp_dt_abs_mean", "dp_dt_max",
    "distortion_mean", "sag_coupling",
)


def _safe_std(x: np.ndarray) -> float:
    return float(np.std(x)) if x.size >= 2 else 0.0


def _slope(y: np.ndarray, x: np.ndarray | None = None) -> float:
    """Least-squares slope, returning 0 rather than throwing on degenerate input."""
    if y.size < 3:
        return 0.0
    if x is None:
        x = np.arange(y.size, dtype=np.float64)
    x = x - x.mean()
    denom = float(np.dot(x, x))
    if denom < 1e-9:
        return 0.0
    return float(np.dot(x, y - y.mean()) / denom)


class FeatureExtractor:
    """Turns the ring buffer into a :class:`FeatureVector`."""

    def __init__(self, cfg=None) -> None:
        self.cfg = cfg or CONFIG.pipeline

    def extract(self, buffer: RingBuffer) -> FeatureVector | None:
        """
        Compute one feature vector from the buffer's tail.

        Returns ``None`` until there is enough history for the medium window to
        mean anything - emitting half-formed vectors would poison the baseline
        the health model is about to learn.
        """
        if len(buffer) < 8:
            return None

        s, m, l = (self.cfg.short_window_s,
                   self.cfg.medium_window_s,
                   self.cfg.long_window_s)

        p_s = buffer.window(s, "active_power_w")
        p_m = buffer.window(m, "active_power_w")
        p_l = buffer.window(l, "active_power_w")
        if p_s.size < 3 or p_m.size < 5:
            return None

        q_m = buffer.window(m, "reactive_power_var")
        v_m = buffer.window(m, "voltage_v")
        i_m = buffer.window(m, "current_a")
        pf_m = buffer.window(m, "power_factor")
        pf_l = buffer.window(l, "power_factor")
        f_m = buffer.window(m, "frequency_hz")
        s_m = buffer.window(m, "apparent_power_va")
        ts = buffer.window(m, "timestamp", valid_only=False)

        fv = FeatureVector()
        fv.timestamp = float(ts[-1]) if ts.size else 0.0
        fv.n_samples = int(p_m.size)

        fv.p_mean_short = float(np.mean(p_s))
        fv.p_mean_med = float(np.mean(p_m))
        fv.p_mean_long = float(np.mean(p_l)) if p_l.size else fv.p_mean_med
        fv.p_std_short = _safe_std(p_s)
        fv.p_std_med = _safe_std(p_m)
        fv.p_min_med = float(np.min(p_m))
        fv.p_max_med = float(np.max(p_m))
        fv.p_range_med = fv.p_max_med - fv.p_min_med

        fv.q_mean_med = float(np.mean(q_m)) if q_m.size else 0.0
        denom = max(abs(fv.p_mean_med), 1.0)
        fv.q_over_p = fv.q_mean_med / denom

        if v_m.size:
            fv.v_mean_med = float(np.mean(v_m))
            fv.v_std_med = _safe_std(v_m)
        if i_m.size:
            fv.i_mean_med = float(np.mean(i_m))
            # Crest here is peak-over-mean of the *envelope*, not of a waveform.
            fv.i_crest_med = float(np.max(i_m)) / max(fv.i_mean_med, 1e-3)
        if pf_m.size:
            fv.pf_mean_med = float(np.mean(pf_m))
            fv.pf_std_med = _safe_std(pf_m)
        if pf_l.size >= 3:
            # Per-hour drift, so the number stays comparable across window sizes.
            fv.pf_slope_long = _slope(pf_l) * 3600.0
        if f_m.size:
            fv.freq_mean_med = float(np.mean(f_m))
            fv.freq_std_med = _safe_std(f_m)

        if p_m.size >= 3:
            dp = np.diff(p_m)
            fv.dp_dt_abs_mean = float(np.mean(np.abs(dp)))
            fv.dp_dt_max = float(np.max(np.abs(dp)))
        if p_l.size >= 3:
            fv.p_slope_long = _slope(p_l) * 3600.0
            fv.duty_cycle_long = float(np.mean(p_l > self.cfg.noise_floor_w))

        # Distortion: how much of S is unexplained by P and Q.
        if s_m.size and q_m.size and s_m.size == q_m.size == p_m.size:
            explained = np.hypot(p_m, q_m)
            excess = np.clip(s_m ** 2 - explained ** 2, 0.0, None)
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = np.where(s_m > 1.0, np.sqrt(excess) / np.maximum(s_m, 1e-6), 0.0)
            fv.distortion_mean = float(np.mean(ratio))

        # Supply stiffness: dV/dP over the medium window.
        if v_m.size == p_m.size and p_m.size >= 5 and np.ptp(p_m) > 10.0:
            fv.sag_coupling = _slope(v_m, p_m)

        if ts.size:
            import time as _time
            lt = _time.localtime(fv.timestamp)
            hour = lt.tm_hour + lt.tm_min / 60.0
            fv.hour_sin = float(np.sin(2 * np.pi * hour / 24.0))
            fv.hour_cos = float(np.cos(2 * np.pi * hour / 24.0))
            fv.dow_sin = float(np.sin(2 * np.pi * lt.tm_wday / 7.0))
            fv.dow_cos = float(np.cos(2 * np.pi * lt.tm_wday / 7.0))

        return fv
