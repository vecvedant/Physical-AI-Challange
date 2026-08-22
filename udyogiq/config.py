"""
Udyog IQ - central configuration.

Every tunable in the system lives here so that the report, the dashboard and the
firmware all agree on one set of numbers.  Values can be overridden at runtime
through environment variables (prefix ``UDYOGIQ_``) or a ``config.local.json``
file sitting next to the repository root, which keeps site-specific wiring out
of version control.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MODEL_DIR = DATA_DIR / "models"
RECORDING_DIR = DATA_DIR / "recordings"
LOCAL_OVERRIDE = REPO_ROOT / "config.local.json"


# --------------------------------------------------------------------------- #
# Acquisition
# --------------------------------------------------------------------------- #
@dataclass
class MeterConfig:
    """How we talk to the Selec EM2M-1P-C-100A."""

    # "bridge"  -> Modbus is mastered by the STM32U585, we pull frames over RPC
    # "serial"  -> Modbus is mastered by Python on the Linux side (USB-RS485)
    # "sim"     -> synthetic meter, no hardware required
    transport: str = "sim"

    slave_id: int = 1
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1
    bytesize: int = 8

    # Only used by the "serial" transport.
    port: str = "/dev/ttyUSB0"

    # Modbus timing.  The EM2M datasheet quotes a 100 ms max response time that
    # is independent of baud rate, so 0.5 s is a generous ceiling.
    timeout_s: float = 0.5
    retries: int = 2

    # Sampling.  1 Hz is the practical ceiling for a full parameter sweep at
    # 9600 baud: 17 floats = 34 registers ~= 80 bytes round trip.
    sample_hz: float = 1.0

    # Word order for 32-bit floats.  Selec ships big-endian words; some units in
    # the field are byte-swapped, so the probe tool can flip this.
    word_order: str = "big"
    byte_order: str = "big"


# --------------------------------------------------------------------------- #
# Signal processing
# --------------------------------------------------------------------------- #
@dataclass
class PipelineConfig:
    # Ring buffer depth in samples (1 Hz * 3600 = one hour of raw detail).
    buffer_samples: int = 3600

    # Feature windows in seconds.  Short window catches transients, long window
    # characterises steady-state behaviour.
    short_window_s: int = 10
    medium_window_s: int = 60
    long_window_s: int = 300

    # Change-point detection: a step is "real" if active power moves by more
    # than this and then holds for `settle_s`.
    edge_threshold_w: float = 25.0
    settle_s: int = 3

    # Power below this is treated as "off" rather than a tiny load.
    noise_floor_w: float = 5.0


# --------------------------------------------------------------------------- #
# Learning
# --------------------------------------------------------------------------- #
@dataclass
class LearningConfig:
    """
    We have no labelled data, by design.  Every model here bootstraps itself
    from the site's own unlabelled stream, which is the whole point: the device
    learns *this* factory's normal rather than shipping someone else's.
    """

    # How long we observe before the health model is allowed to raise alarms.
    baseline_minutes: int = 60

    # NILM: max distinct appliances we will try to discover from one meter.
    max_appliances: int = 8
    # Two power steps are the same appliance if they agree within this band.
    cluster_tolerance_w: float = 40.0
    cluster_tolerance_frac: float = 0.15
    # An appliance must be seen this many times before we trust it.
    min_observations: int = 3

    # Machine-state segmentation (per discovered appliance).
    max_states: int = 5

    # Health / anomaly detection.
    anomaly_contamination: float = 0.02
    # Consecutive anomalous windows before we escalate to an alert.
    anomaly_persistence: int = 5
    health_ewma_alpha: float = 0.05

    # Forecasting.
    forecast_block_minutes: int = 15
    forecast_horizons: tuple = (1, 2, 4, 8, 24, 48, 96)
    min_history_blocks: int = 96  # 24 h of 15-min blocks before we forecast


# --------------------------------------------------------------------------- #
# Sustainability accounting
# --------------------------------------------------------------------------- #
@dataclass
class SustainabilityConfig:
    # CEA CO2 Baseline Database for the Indian grid.  Weighted average operating
    # margin, rounded.  Documented in docs/assumptions.md.
    grid_emission_factor_kg_per_kwh: float = 0.71

    # Commercial/industrial LT tariff, INR per kWh.  Site-specific.
    tariff_inr_per_kwh: float = 8.0

    # Maximum demand charge, INR per kVA per month, and the sanctioned ceiling.
    demand_charge_inr_per_kva: float = 350.0
    sanctioned_demand_kva: float = 5.0

    # Utilities penalise power factor below this and reward above it.
    pf_penalty_threshold: float = 0.90
    pf_penalty_pct_per_point: float = 1.0


# --------------------------------------------------------------------------- #
# Control policy
# --------------------------------------------------------------------------- #
@dataclass
class PolicyConfig:
    # Master switch.  When False the engine still reasons and explains, but the
    # contactor is never commanded - "advisory mode".
    actuation_enabled: bool = False

    # Idle cutoff: machine drawing standby power with no productive work.
    idle_cutoff_enabled: bool = True
    idle_timeout_s: int = 900          # 15 minutes of idle before we act
    idle_power_margin_w: float = 15.0

    # Maximum-demand guard.
    demand_guard_enabled: bool = True
    demand_guard_headroom_pct: float = 10.0

    # Protective trip on a severe health anomaly.
    anomaly_trip_enabled: bool = False
    anomaly_trip_score: float = 0.95

    # Contactor protection.  These are enforced *again* on the MCU; the values
    # here only stop us from issuing a request the firmware would reject.
    min_off_s: int = 60
    min_on_s: int = 30
    max_switches_per_hour: int = 10


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #
@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    # Dashboard push rate.  1 Hz matches acquisition; the PWA interpolates.
    broadcast_hz: float = 1.0
    history_page_size: int = 2000


@dataclass
class StoreConfig:
    db_path: str = str(DATA_DIR / "udyogiq.db")
    # Raw 1 Hz samples are kept this long, then rolled into 1-min aggregates.
    raw_retention_hours: int = 48
    aggregate_retention_days: int = 365


# --------------------------------------------------------------------------- #
# Root
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    site_name: str = "Udyog IQ Reference Node"
    meter: MeterConfig = field(default_factory=MeterConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    sustainability: SustainabilityConfig = field(default_factory=SustainabilityConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    store: StoreConfig = field(default_factory=StoreConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SECTIONS = {
    "meter": MeterConfig,
    "pipeline": PipelineConfig,
    "learning": LearningConfig,
    "sustainability": SustainabilityConfig,
    "policy": PolicyConfig,
    "server": ServerConfig,
    "store": StoreConfig,
}


def _coerce(current: Any, raw: str) -> Any:
    """Turn an environment string into the type the dataclass field expects."""
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


def load_config() -> Config:
    """Defaults, then config.local.json, then UDYOGIQ_* environment variables."""
    cfg = Config()

    if LOCAL_OVERRIDE.exists():
        try:
            blob = json.loads(LOCAL_OVERRIDE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:  # pragma: no cover - operator error
            raise SystemExit(f"config.local.json is not valid JSON: {exc}") from exc
        for section, values in blob.items():
            target = getattr(cfg, section, None)
            if target is None or not isinstance(values, dict):
                continue
            for key, value in values.items():
                if hasattr(target, key):
                    setattr(target, key, value)

    # UDYOGIQ_METER_TRANSPORT=serial, UDYOGIQ_SERVER_PORT=9000, ...
    for section_name, section_type in _SECTIONS.items():
        target = getattr(cfg, section_name)
        for f in fields(section_type):
            env_key = f"UDYOGIQ_{section_name.upper()}_{f.name.upper()}"
            if env_key in os.environ:
                setattr(target, f.name,
                        _coerce(getattr(target, f.name), os.environ[env_key]))

    for directory in (DATA_DIR, MODEL_DIR, RECORDING_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    return cfg


CONFIG = load_config()
