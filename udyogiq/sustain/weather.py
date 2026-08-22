"""
Weather forecast client, built to survive having no internet.

Solar prediction needs a sky forecast, and a factory floor is exactly the sort
of place where the Wi-Fi is someone else's problem.  So this is not a thin HTTP
wrapper; it is a three-tier fallback that always returns *something* the
dispatch optimiser can plan against:

  1. Live fetch from Open-Meteo, cached to disk.
  2. The cached forecast, while it is fresh enough to trust.
  3. Climatology learned from the site's own measured history - what this hour
     of this month has actually produced here before.

Tier 3 matters more than it looks.  A system that stops optimising when the
link drops is a system that gets unplugged, and "we cannot plan today because
the internet is down" is not an answer anyone accepts about their own roof.

Open-Meteo was chosen because it needs no API key.  A key is one more thing to
expire silently on a box nobody logs into for a year.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..config import CONFIG

log = logging.getLogger(__name__)

#: Variables we ask Open-Meteo for.  shortwave_radiation is global horizontal
#: irradiance, which with the diffuse component is enough to put irradiance on
#: a tilted plane; temperature drives the cell derate.
HOURLY_VARS = (
    "shortwave_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "cloud_cover",
    "temperature_2m",
)


@dataclass
class WeatherPoint:
    """One hourly forecast row."""

    timestamp: float
    ghi_w_m2: float = 0.0        # global horizontal irradiance
    dhi_w_m2: float = 0.0        # diffuse horizontal
    dni_w_m2: float = 0.0        # direct normal
    cloud_cover_pct: float = 0.0
    temperature_c: float = 25.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "ghi_w_m2": self.ghi_w_m2,
            "dhi_w_m2": self.dhi_w_m2,
            "dni_w_m2": self.dni_w_m2,
            "cloud_cover_pct": self.cloud_cover_pct,
            "temperature_c": self.temperature_c,
        }

    @classmethod
    def from_dict(cls, blob: dict[str, Any]) -> "WeatherPoint":
        return cls(**{k: blob[k] for k in blob if k in cls.__dataclass_fields__})


@dataclass
class Forecast:
    """A run of hourly points, plus where it came from."""

    points: list[WeatherPoint] = field(default_factory=list)
    fetched_at: float = 0.0
    #: "live", "cache", "climatology" or "none" - carried through to the
    #: dashboard so an operator can see the plan is running on stale sky.
    source: str = "none"

    @property
    def age_hours(self) -> float:
        return (time.time() - self.fetched_at) / 3600.0 if self.fetched_at else 1e9

    def at(self, timestamp: float) -> WeatherPoint | None:
        """Nearest point, or None if the forecast does not reach that far."""
        if not self.points:
            return None
        best = min(self.points, key=lambda p: abs(p.timestamp - timestamp))
        return best if abs(best.timestamp - timestamp) <= 5400 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "source": self.source,
            "points": [p.to_dict() for p in self.points],
        }


class Climatology:
    """
    What this hour of this month has actually produced at this site before.

    Built from the historian's own record of measured generation, so it needs no
    network and improves the longer the node has been running. Crude compared
    with a real forecast, and far better than assuming zero.
    """

    def __init__(self) -> None:
        # month (1-12) x hour (0-23) -> mean measured irradiance proxy
        self._grid = np.full((13, 24), np.nan)
        self._counts = np.zeros((13, 24))

    def observe(self, timestamp: float, ghi_proxy: float) -> None:
        lt = time.localtime(timestamp)
        m, h = lt.tm_mon, lt.tm_hour
        n = self._counts[m, h]
        current = self._grid[m, h]
        self._grid[m, h] = ghi_proxy if math.isnan(current) else (current * n + ghi_proxy) / (n + 1)
        self._counts[m, h] = n + 1

    def ready(self) -> bool:
        return bool(np.nansum(self._counts) >= 48)

    def forecast(self, start_t: float, hours: int) -> Forecast:
        points: list[WeatherPoint] = []
        for i in range(hours):
            t = start_t + i * 3600
            lt = time.localtime(t)
            value = self._grid[lt.tm_mon, lt.tm_hour]
            if math.isnan(value):
                # Neighbouring months are a better guess than zero.
                column = self._grid[:, lt.tm_hour]
                value = float(np.nanmean(column)) if not np.all(np.isnan(column)) else 0.0
            points.append(WeatherPoint(
                timestamp=t,
                ghi_w_m2=max(0.0, float(value)),
                dhi_w_m2=max(0.0, float(value) * 0.35),
                temperature_c=28.0,
            ))
        return Forecast(points=points, fetched_at=time.time(), source="climatology")

    def to_dict(self) -> dict[str, Any]:
        return {"grid": np.nan_to_num(self._grid, nan=-1.0).tolist(),
                "counts": self._counts.tolist()}

    def load_dict(self, blob: dict[str, Any]) -> None:
        grid = np.array(blob.get("grid", []), dtype=float)
        if grid.shape == (13, 24):
            grid[grid < 0] = np.nan
            self._grid = grid
        counts = np.array(blob.get("counts", []), dtype=float)
        if counts.shape == (13, 24):
            self._counts = counts


class WeatherClient:
    """Fetches, caches and degrades gracefully."""

    def __init__(self, cfg=None, site=None) -> None:
        self.cfg = cfg or CONFIG.weather
        self.site = site or CONFIG.site
        self.climatology = Climatology()
        self._forecast = Forecast()
        self._last_attempt = 0.0
        self._consecutive_failures = 0
        self._load_cache()

    # ------------------------------------------------------------------ #
    @property
    def cache_path(self) -> Path:
        return Path(self.cfg.cache_path)

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            blob = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:                                   # noqa: BLE001
            return
        self._forecast = Forecast(
            points=[WeatherPoint.from_dict(p) for p in blob.get("points", [])],
            fetched_at=float(blob.get("fetched_at", 0.0)),
            source="cache",
        )
        if "climatology" in blob:
            self.climatology.load_dict(blob["climatology"])

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            blob = self._forecast.to_dict()
            blob["climatology"] = self.climatology.to_dict()
            self.cache_path.write_text(json.dumps(blob), encoding="utf-8")
        except Exception as exc:                            # noqa: BLE001
            log.debug("could not write weather cache: %s", exc)

    # ------------------------------------------------------------------ #
    def _fetch(self) -> Forecast | None:
        try:
            import httpx
        except ImportError:
            log.debug("httpx unavailable; weather will run from cache")
            return None

        params = {
            "latitude": self.site.latitude,
            "longitude": self.site.longitude,
            "hourly": ",".join(HOURLY_VARS),
            "forecast_days": self.cfg.forecast_days,
            "timeformat": "unixtime",
            "timezone": "UTC",
        }
        try:
            resp = httpx.get(self.cfg.endpoint, params=params,
                             timeout=self.cfg.request_timeout_s)
            resp.raise_for_status()
            blob = resp.json()
        except Exception as exc:                            # noqa: BLE001
            self._consecutive_failures += 1
            log.info("weather fetch failed (%d in a row): %s",
                     self._consecutive_failures, exc)
            return None

        hourly = blob.get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            return None

        def col(name: str) -> list[float]:
            values = hourly.get(name) or []
            return [float(v) if v is not None else 0.0 for v in values]

        ghi, dhi = col("shortwave_radiation"), col("diffuse_radiation")
        dni, cloud = col("direct_normal_irradiance"), col("cloud_cover")
        temp = col("temperature_2m")

        points = []
        for i, t in enumerate(times):
            points.append(WeatherPoint(
                timestamp=float(t),
                ghi_w_m2=ghi[i] if i < len(ghi) else 0.0,
                dhi_w_m2=dhi[i] if i < len(dhi) else 0.0,
                dni_w_m2=dni[i] if i < len(dni) else 0.0,
                cloud_cover_pct=cloud[i] if i < len(cloud) else 0.0,
                temperature_c=temp[i] if i < len(temp) else 25.0,
            ))
        self._consecutive_failures = 0
        return Forecast(points=points, fetched_at=time.time(), source="live")

    # ------------------------------------------------------------------ #
    def refresh(self, force: bool = False) -> Forecast:
        """
        Return the best forecast available, fetching if it is time to.

        Never raises and never blocks longer than the HTTP timeout. A weather
        outage must degrade the plan's quality, not stop the plant optimising.
        """
        now = time.time()
        due = (now - self._last_attempt) >= self.cfg.refresh_minutes * 60
        if force or due:
            self._last_attempt = now
            fetched = self._fetch()
            if fetched is not None:
                self._forecast = fetched
                self._save_cache()
                return self._forecast

        if self._forecast.points and self._forecast.age_hours <= self.cfg.max_cache_age_h:
            self._forecast.source = "live" if self._forecast.age_hours < 1 else "cache"
            return self._forecast

        if self.climatology.ready():
            return self.climatology.forecast(now, self.cfg.forecast_days * 24)

        # Nothing at all: return whatever stale thing we have rather than an
        # empty forecast, and label it honestly.
        if self._forecast.points:
            self._forecast.source = "cache"
        return self._forecast

    # ------------------------------------------------------------------ #
    def observe_generation(self, timestamp: float, ghi_proxy: float) -> None:
        """Feed measured generation back in, so climatology keeps improving."""
        self.climatology.observe(timestamp, ghi_proxy)

    def status(self) -> dict[str, Any]:
        f = self._forecast
        return {
            "source": f.source,
            "age_hours": round(f.age_hours, 2) if f.fetched_at else None,
            "points": len(f.points),
            "consecutive_failures": self._consecutive_failures,
            "climatology_ready": self.climatology.ready(),
        }
