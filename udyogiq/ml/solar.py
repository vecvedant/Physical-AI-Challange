"""
Solar generation forecasting: clear-sky physics, corrected by what actually
happened on this roof.

Why not pure machine learning
-----------------------------
A model asked to learn "weather in, watts out" from scratch has to rediscover
the solar geometry - that output depends on the sun's angle, which depends on
latitude, date and time of day - from a few weeks of data.  It will fit the
weeks it saw and then be confidently wrong in December, because it never saw
December.  The geometry is not empirical; it is astronomy, and we have known it
for centuries.

Why not pure physics either
---------------------------
The physics assumes a clean, unshaded, correctly-oriented array, and no real
installation is any of those.  The neighbour's water tank shades the corner
until 09:30.  The installer's "due south" was 20 degrees off.  Dust builds up
until the monsoon washes it away.

So: physics computes what the array *should* make, and a gradient-boosting
model learns the *residual* - the systematic gap between that and what the
meter actually recorded.  The physics carries the seasonal structure the data
cannot, and the learned part carries the site-specific reality the physics
cannot.  If the correction model has never been trained the forecast quietly
falls back to bare physics, which is wrong by maybe 20% rather than useless.

Self-supervised, again: the label is simply the generation the meter measured
an hour later, which the historian already holds.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..config import CONFIG, MODEL_DIR
from ..sustain.weather import Forecast, WeatherPoint

#: Ground reflectance for the isotropic sky model. 0.2 is the standard value
#: for ordinary terrain; snow or a white roof would be far higher.
GROUND_ALBEDO = 0.2


# --------------------------------------------------------------------------- #
# Solar geometry
# --------------------------------------------------------------------------- #
def solar_position(timestamp: float, latitude: float, longitude: float,
                   tz_offset_h: float) -> tuple[float, float]:
    """
    Return (elevation, azimuth) of the sun in degrees.

    Azimuth is measured clockwise from north, so 180 is due south, matching the
    convention used for the array's own orientation.
    """
    lt = time.gmtime(timestamp + tz_offset_h * 3600)
    day_of_year = lt.tm_yday
    local_hour = lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0

    # Equation of time: the sun's disagreement with the clock, up to ~16 min.
    b = math.radians(360.0 * (day_of_year - 81) / 364.0)
    eot_min = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)

    # Longitude correction against the timezone's standard meridian.
    standard_meridian = 15.0 * tz_offset_h
    solar_time = local_hour + (4.0 * (longitude - standard_meridian) + eot_min) / 60.0

    hour_angle = math.radians(15.0 * (solar_time - 12.0))
    declination = math.radians(
        23.45 * math.sin(math.radians(360.0 * (284 + day_of_year) / 365.0)))
    phi = math.radians(latitude)

    sin_elev = (math.sin(phi) * math.sin(declination)
                + math.cos(phi) * math.cos(declination) * math.cos(hour_angle))
    sin_elev = max(-1.0, min(1.0, sin_elev))
    elevation = math.degrees(math.asin(sin_elev))

    cos_elev = math.cos(math.asin(sin_elev))
    if abs(cos_elev) < 1e-6:
        return elevation, 180.0
    cos_az = ((math.sin(declination) * math.cos(phi)
               - math.cos(declination) * math.sin(phi) * math.cos(hour_angle))
              / cos_elev)
    cos_az = max(-1.0, min(1.0, cos_az))
    azimuth = math.degrees(math.acos(cos_az))
    if hour_angle > 0:                     # afternoon
        azimuth = 360.0 - azimuth
    return elevation, azimuth


def plane_of_array(ghi: float, dhi: float, dni: float,
                   elevation_deg: float, azimuth_deg: float,
                   tilt_deg: float, array_azimuth_deg: float) -> float:
    """
    Irradiance on the tilted panel, isotropic sky model.

    Three components: beam projected onto the panel, diffuse from the sky dome
    the panel can see, and ground reflection. Good to a few percent for a fixed
    array, which is well inside the error the residual model then absorbs.
    """
    if elevation_deg <= 0.0:
        return 0.0

    beta = math.radians(tilt_deg)
    gamma = math.radians(array_azimuth_deg)
    elev = math.radians(elevation_deg)
    azim = math.radians(azimuth_deg)

    # Angle of incidence between the sun and the panel normal.
    cos_incidence = (math.sin(elev) * math.cos(beta)
                     + math.cos(elev) * math.sin(beta) * math.cos(azim - gamma))
    cos_incidence = max(0.0, cos_incidence)

    beam = dni * cos_incidence
    diffuse = dhi * (1.0 + math.cos(beta)) / 2.0
    ground = ghi * GROUND_ALBEDO * (1.0 - math.cos(beta)) / 2.0
    return max(0.0, beam + diffuse + ground)


# --------------------------------------------------------------------------- #
@dataclass
class SolarPrediction:
    timestamp: float
    predicted_w: float
    physics_w: float
    poa_w_m2: float
    cell_temp_c: float
    corrected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "predicted_w": round(self.predicted_w, 1),
            "physics_w": round(self.physics_w, 1),
            "poa_w_m2": round(self.poa_w_m2, 1),
            "cell_temp_c": round(self.cell_temp_c, 1),
            "corrected": self.corrected,
        }


class SolarForecaster:
    """Clear-sky physics plus a learned site correction."""

    def __init__(self, site=None) -> None:
        self.site = site or CONFIG.site
        self._model = None
        self.trained = False
        self.trained_at = 0.0
        self.n_train = 0
        self._train_mae = 0.0

    # ------------------------------------------------------------------ #
    def physics(self, timestamp: float, weather: WeatherPoint | None) -> SolarPrediction:
        """What the array should produce, from geometry and irradiance alone."""
        s = self.site
        elevation, azimuth = solar_position(
            timestamp, s.latitude, s.longitude, s.timezone_offset_h)

        if weather is None or elevation <= 0.0:
            return SolarPrediction(timestamp, 0.0, 0.0, 0.0, 25.0)

        ghi, dhi, dni = weather.ghi_w_m2, weather.dhi_w_m2, weather.dni_w_m2
        # Some feeds omit DNI. Recover it from the beam component rather than
        # dropping the largest term on the clearest days.
        if dni <= 0.0 and ghi > dhi:
            sin_elev = max(math.sin(math.radians(elevation)), 0.05)
            dni = max(0.0, (ghi - dhi) / sin_elev)

        poa = plane_of_array(ghi, dhi, dni, elevation, azimuth,
                             s.tilt_deg, s.azimuth_deg)

        # Cell temperature from the NOCT model, then the temperature derate.
        # On a 40 C afternoon in Maharashtra this is a 10-15% haircut, which is
        # far too big to leave out.
        ambient = weather.temperature_c
        cell_temp = ambient + (s.noct_c - 20.0) / 800.0 * poa
        derate = 1.0 + s.temp_coeff_per_k * (cell_temp - 25.0)

        watts = s.array_peak_w * (poa / 1000.0) * s.system_efficiency * derate
        watts = max(0.0, min(watts, s.array_peak_w * 1.15))
        return SolarPrediction(timestamp, watts, watts, poa, cell_temp)

    # ------------------------------------------------------------------ #
    def _features(self, timestamp: float, weather: WeatherPoint,
                  phys: SolarPrediction) -> list[float]:
        elevation, azimuth = solar_position(
            timestamp, self.site.latitude, self.site.longitude,
            self.site.timezone_offset_h)
        lt = time.localtime(timestamp)
        hour = lt.tm_hour + lt.tm_min / 60.0
        return [
            phys.physics_w,
            phys.poa_w_m2,
            phys.cell_temp_c,
            weather.ghi_w_m2,
            weather.dhi_w_m2,
            weather.cloud_cover_pct,
            weather.temperature_c,
            elevation,
            azimuth,
            math.sin(2 * math.pi * hour / 24.0),
            math.cos(2 * math.pi * hour / 24.0),
            math.sin(2 * math.pi * lt.tm_yday / 365.0),
            math.cos(2 * math.pi * lt.tm_yday / 365.0),
        ]

    def fit(self, samples: list[tuple[float, WeatherPoint, float]]) -> bool:
        """
        Learn the correction from (timestamp, weather, measured_watts) triples.

        Only daylight samples are used. Night rows are all exactly zero, and a
        tree model handed thousands of trivially-correct rows spends its
        capacity learning that the sun sets.
        """
        rows, targets = [], []
        for ts, wx, measured in samples:
            phys = self.physics(ts, wx)
            if phys.poa_w_m2 < 20.0:
                continue
            rows.append(self._features(ts, wx, phys))
            # Predict the residual, not the output: the model only has to learn
            # what the physics got wrong, which is a far smaller and better
            # conditioned target than the generation curve itself.
            targets.append(measured - phys.physics_w)

        if len(rows) < 80:
            return False

        from sklearn.ensemble import HistGradientBoostingRegressor

        X = np.asarray(rows, dtype=np.float64)
        y = np.asarray(targets, dtype=np.float64)
        split = int(X.shape[0] * 0.8)

        model = HistGradientBoostingRegressor(
            max_iter=180, learning_rate=0.06, max_depth=5,
            min_samples_leaf=8, l2_regularization=1.0, random_state=0)
        model.fit(X[:split], y[:split])
        if X.shape[0] - split >= 5:
            self._train_mae = float(np.mean(np.abs(y[split:] - model.predict(X[split:]))))
        model.fit(X, y)

        self._model = model
        self.trained = True
        self.trained_at = time.time()
        self.n_train = X.shape[0]
        return True

    # ------------------------------------------------------------------ #
    def predict(self, timestamp: float, weather: WeatherPoint | None) -> SolarPrediction:
        phys = self.physics(timestamp, weather)
        if not self.trained or weather is None or phys.poa_w_m2 < 20.0:
            return phys

        x = np.asarray([self._features(timestamp, weather, phys)], dtype=np.float64)
        correction = float(self._model.predict(x)[0])
        corrected = phys.physics_w + correction
        # The correction is a nudge, not a licence to invent generation.
        corrected = max(0.0, min(corrected, self.site.array_peak_w * 1.15))
        return SolarPrediction(timestamp, corrected, phys.physics_w,
                               phys.poa_w_m2, phys.cell_temp_c, corrected=True)

    def profile(self, forecast: Forecast, start_t: float, blocks: int,
                block_minutes: int = 15) -> np.ndarray:
        """
        Expected generation for each of the next N blocks, for dispatch.

        The weather feed is hourly and dispatch plans in quarter hours, so the
        physics is evaluated at each block's own timestamp while the weather is
        held at its hourly value. That keeps the sunrise and sunset edges sharp
        instead of smearing them across an hour.
        """
        out = np.zeros(blocks, dtype=np.float64)
        for i in range(blocks):
            t = start_t + i * block_minutes * 60
            wx = forecast.at(t)
            out[i] = self.predict(t, wx).predicted_w if wx else 0.0
        return out

    def daily_energy_kwh(self, forecast: Forecast, start_t: float,
                         hours: int = 24) -> float:
        """Total expected generation over the next N hours."""
        total = 0.0
        for i in range(hours * 4):
            t = start_t + i * 900
            wx = forecast.at(t)
            if wx:
                total += self.predict(t, wx).predicted_w * 0.25
        return total / 1000.0

    # ------------------------------------------------------------------ #
    def save(self, path: Path | None = None) -> Path:
        import joblib
        path = Path(path or (MODEL_DIR / "solar_correction.joblib"))
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": self._model, "trained_at": self.trained_at,
                     "n_train": self.n_train, "train_mae": self._train_mae}, path)
        return path

    def load(self, path: Path | None = None) -> bool:
        import joblib
        path = Path(path or (MODEL_DIR / "solar_correction.joblib"))
        if not path.exists():
            return False
        try:
            blob = joblib.load(path)
        except Exception:                                   # noqa: BLE001
            return False
        self._model = blob.get("model")
        self.trained_at = float(blob.get("trained_at", 0.0))
        self.n_train = int(blob.get("n_train", 0))
        self._train_mae = float(blob.get("train_mae", 0.0))
        self.trained = self._model is not None
        return self.trained

    def status(self) -> dict[str, Any]:
        return {
            "trained": self.trained,
            "n_train": self.n_train,
            "residual_mae_w": round(self._train_mae, 1),
            "array_peak_w": self.site.array_peak_w,
            "tilt_deg": self.site.tilt_deg,
            "azimuth_deg": self.site.azimuth_deg,
        }
