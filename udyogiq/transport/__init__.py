"""Transport selection.

One place decides which backend the rest of the system talks to, driven by
``CONFIG.meter.transport``.  Nothing above this module imports a concrete
transport, which is what keeps a bring-up problem to a config edit.
"""

from __future__ import annotations

import logging

from ..config import CONFIG
from .base import InverterAdapter, MeterTransport, TransportError, TransportHealth

log = logging.getLogger(__name__)

__all__ = [
    "MeterTransport",
    "InverterAdapter",
    "TransportError",
    "TransportHealth",
    "make_meter",
    "make_inverter",
]


def make_meter(transport: str | None = None, **kwargs) -> MeterTransport:
    """Build the configured meter transport."""
    kind = (transport or CONFIG.meter.transport).lower()

    if kind == "sim":
        from .simulated import SimulatedMeter
        return SimulatedMeter(**kwargs)

    if kind == "serial":
        from .serial_rtu import SerialModbusMeter
        return SerialModbusMeter(**kwargs)

    if kind == "bridge":
        from .bridge import BridgeMeter
        return BridgeMeter(**kwargs)

    raise ValueError(
        f"unknown meter transport {kind!r}; expected one of sim, serial, bridge"
    )


def make_inverter(meter: MeterTransport, kind: str = "auto",
                  **kwargs) -> InverterAdapter:
    """
    Build the best source-side adapter available.

    ``auto`` follows the meter: a simulated plant has perfect telemetry, a real
    one falls back to estimation unless the caller knows better.
    """
    kind = kind.lower()

    if kind == "auto":
        kind = "sim" if meter.name == "sim" else "estimated"

    if kind == "sim":
        from .simulated import SimulatedInverter, SimulatedMeter
        if not isinstance(meter, SimulatedMeter):
            raise TypeError("the sim inverter needs a SimulatedMeter")
        return SimulatedInverter(meter)

    if kind == "estimated":
        from .estimated import EstimatedInverter
        log.info("no inverter telemetry: solar and battery will be estimated "
                 "from the energy balance and reported as estimates")
        return EstimatedInverter(**kwargs)

    if kind == "bridge":
        from .bridge import BridgeInverter
        return BridgeInverter(**kwargs)

    raise ValueError(
        f"unknown inverter adapter {kind!r}; expected auto, sim, estimated or bridge"
    )
