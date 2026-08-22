"""
Transport that pulls meter frames across the Arduino Bridge from the STM32.

This is the intended production path on the UNO Q, and it is the one that
justifies the board.  The MCU owns the RS485 bus: it polls the meter on a fixed
cadence, keeps the newest decoded reading in a struct, and enforces the
contactor interlock in hardware time.  Linux never touches the bus and never
holds a deadline.

Two facts about the Bridge shape everything here:

  * Linux always initiates.  The MCU cannot push, so we poll a cached snapshot
    rather than subscribing to a stream.  The MCU's Modbus cadence and our poll
    rate are therefore independent, and a Linux stall costs us freshness, never
    bus timing.
  * Calls are synchronous RPC.  A blocked call would stall the whole asyncio
    loop, so reads happen on a worker thread in the runtime, not inline.

The MCU returns a flat list of floats in a fixed order rather than a dict,
because MessagePack over the Bridge is cheapest that way and the ordering is
pinned by :data:`FIELD_ORDER` on both sides.
"""

from __future__ import annotations

import logging
import time

from ..config import CONFIG
from ..meter.frame import MeterFrame
from .base import InverterAdapter, MeterTransport, TransportError

log = logging.getLogger(__name__)

#: Field order agreed with sketch/sketch.ino. Changing this without changing
#: the sketch will silently scramble every reading, so both sides name it.
FIELD_ORDER: tuple[str, ...] = (
    "active_power_w",
    "reactive_power_var",
    "apparent_power_va",
    "voltage_v",
    "current_a",
    "power_factor",
    "frequency_hz",
    "import_active_energy_kwh",
    "export_active_energy_kwh",
    "total_active_energy_kwh",
    "max_demand_active_w",
)

#: Extra status words appended after the measurements.
STATUS_ORDER: tuple[str, ...] = (
    "age_ms",          # how stale the MCU's cached reading is
    "modbus_errors",   # cumulative failed transactions
    "contactor_state",  # 1 closed, 0 open
    "interlock_flags",  # bitfield, see sketch
)


def _import_bridge():
    """
    Resolve the Bridge object provided by the App Lab runtime.

    Only importable on the board, which is why it is deferred: the same module
    has to import cleanly on a laptop so the rest of the package stays
    testable.
    """
    try:
        from arduino.app_utils import Bridge  # type: ignore
        return Bridge
    except Exception as exc:                            # noqa: BLE001
        raise TransportError(
            "arduino.app_utils is unavailable - this transport only runs on the "
            "UNO Q under App Lab. Use transport='serial' or 'sim' elsewhere."
        ) from exc


class BridgeMeter(MeterTransport):
    """Reads the STM32's cached Modbus snapshot over RPC."""

    name = "bridge"

    #: Refuse a reading the MCU has not refreshed recently; a stale frame
    #: repeated into the feature extractor looks exactly like a genuinely
    #: steady load, which would quietly corrupt NILM.
    max_age_s: float = 5.0

    def __init__(self) -> None:
        super().__init__()
        self._bridge = None
        self.last_status: dict[str, float] = {}

    def open(self) -> None:
        if self._bridge is None:
            self._bridge = _import_bridge()
            log.info("Bridge transport ready; MCU owns the RS485 bus")

    def close(self) -> None:
        self._bridge = None

    # ------------------------------------------------------------------ #
    def _read(self) -> MeterFrame:
        if self._bridge is None:
            self.open()

        values = self._bridge.call("read_meter")
        if not values:
            raise TransportError("MCU returned no meter data")
        if len(values) < len(FIELD_ORDER):
            raise TransportError(
                f"MCU returned {len(values)} fields, expected at least "
                f"{len(FIELD_ORDER)}; sketch and host are out of sync"
            )

        payload = {name: float(values[i]) for i, name in enumerate(FIELD_ORDER)}
        status = {
            name: float(values[len(FIELD_ORDER) + i])
            for i, name in enumerate(STATUS_ORDER)
            if len(FIELD_ORDER) + i < len(values)
        }
        self.last_status = status

        age_s = status.get("age_ms", 0.0) / 1000.0
        if age_s > self.max_age_s:
            raise TransportError(
                f"MCU snapshot is {age_s:.1f}s stale - the meter is probably "
                f"not responding on the RS485 bus"
            )

        frame = MeterFrame(**payload, source="bridge",
                           slave_id=CONFIG.meter.slave_id)
        frame.timestamp = time.time() - age_s
        return frame

    # ------------------------------------------------------------------ #
    def set_contactor(self, closed: bool) -> bool:
        """
        Request a contactor state.

        The MCU is free to refuse: minimum on/off dwell and the switching-rate
        cap are enforced there, in hardware time, precisely so that a bug or a
        hang on the Linux side cannot chatter a physical contactor to death.
        A False return means the interlock declined, not that the call failed.
        """
        if self._bridge is None:
            self.open()
        return bool(self._bridge.call("set_contactor", 1 if closed else 0))

    def read_interlock(self) -> dict[str, float]:
        if self._bridge is None:
            self.open()
        raw = self._bridge.call("read_status") or []
        return {name: float(raw[i]) for i, name in enumerate(STATUS_ORDER)
                if i < len(raw)}


class BridgeInverter(InverterAdapter):
    """
    Inverter telemetry read by the MCU from a second Modbus slave.

    This is where multi-drop finally earns its keep: the meter answers on one
    slave id and the hybrid inverter on another, over the same pair of wires,
    so solar generation and battery SoC become measured rather than inferred.
    """

    name = "bridge-inverter"

    def __init__(self, *, controllable: bool = False) -> None:
        self.controllable = controllable
        self._bridge = None

    def open(self) -> None:
        if self._bridge is None:
            self._bridge = _import_bridge()

    def read(self) -> dict[str, float]:
        if self._bridge is None:
            self.open()
        raw = self._bridge.call("read_inverter") or []
        keys = ("solar_w", "battery_w", "soc", "load_w", "grid_w")
        return {k: float(raw[i]) for i, k in enumerate(keys) if i < len(raw)}

    def command_battery(self, watts: float) -> bool:
        if not self.controllable:
            return False
        if self._bridge is None:
            self.open()
        return bool(self._bridge.call("set_battery_power", float(watts)))
