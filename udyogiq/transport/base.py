"""
The transport contract.

Udyog IQ has to run in three quite different places:

  * on the UNO Q with the STM32 mastering Modbus and Python pulling frames
    across the Bridge,
  * on any Linux box with a USB-RS485 dongle and pymodbus in-process,
  * and against a simulated plant with no hardware at all.

Everything above this file consumes :class:`MeterFrame` and never learns which
of the three it is talking to.  That is what let the models be developed and
trained before the hardware existed, and it is what makes a bring-up problem a
one-line config change rather than a rewrite.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass

from ..meter.frame import MeterFrame

log = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """Raised when a transport cannot produce a reading at all."""


@dataclass
class TransportHealth:
    """Rolling quality statistics, surfaced on the dashboard."""

    reads_ok: int = 0
    reads_failed: int = 0
    consecutive_failures: int = 0
    last_success_t: float = 0.0
    last_error: str = ""

    @property
    def total(self) -> int:
        return self.reads_ok + self.reads_failed

    @property
    def success_rate(self) -> float:
        return self.reads_ok / self.total if self.total else 1.0

    @property
    def online(self) -> bool:
        return self.consecutive_failures < 5


class MeterTransport(abc.ABC):
    """Base class for anything that can hand us a MeterFrame."""

    name: str = "base"

    def __init__(self) -> None:
        self.health = TransportHealth()
        self._last_good: MeterFrame | None = None

    # ------------------------------------------------------------------ #
    # Implemented by subclasses
    # ------------------------------------------------------------------ #
    @abc.abstractmethod
    def open(self) -> None:
        """Acquire the underlying resource.  Must be idempotent."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release the resource.  Must be safe to call twice."""

    @abc.abstractmethod
    def _read(self) -> MeterFrame:
        """One raw acquisition.  Raise TransportError on failure."""

    # ------------------------------------------------------------------ #
    # Shared behaviour
    # ------------------------------------------------------------------ #
    def read(self) -> MeterFrame:
        """
        Read one frame, with bookkeeping and a hold-last-value fallback.

        A dropped Modbus frame is a routine event on a long RS485 run next to a
        contactor, and it must not tear down the pipeline.  We return the last
        good frame marked ``valid=False`` so downstream code can choose to skip
        it - the feature extractor does, the dashboard shows it greyed out -
        without every call site having to handle an exception.
        """
        try:
            frame = self._read()
        except Exception as exc:                        # noqa: BLE001
            self.health.reads_failed += 1
            self.health.consecutive_failures += 1
            self.health.last_error = str(exc)
            log.debug("%s read failed: %s", self.name, exc)
            if self._last_good is None:
                raise TransportError(f"{self.name}: no reading available: {exc}") from exc
            stale = MeterFrame(**{**self._last_good.to_dict(),
                                  "timestamp": time.time(), "valid": False})
            return stale

        problems = frame.sanity_check()
        if problems:
            # Physically impossible numbers almost always mean the register map
            # or the word order is wrong, so say so loudly and once.
            self.health.last_error = "; ".join(problems)
            if self.health.consecutive_failures == 0:
                log.warning("%s implausible frame: %s", self.name, self.health.last_error)

        self.health.reads_ok += 1
        self.health.consecutive_failures = 0
        self.health.last_success_t = frame.timestamp
        self._last_good = frame
        return frame

    # Context-manager sugar so callers cannot forget to close a serial port.
    def __enter__(self) -> "MeterTransport":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class InverterAdapter(abc.ABC):
    """
    Source-side telemetry: solar generation, battery state, and battery command.

    With a single meter we cannot measure solar, battery and load
    independently, so this interface has three implementations of decreasing
    fidelity - real Modbus telemetry, a vendor HTTP API, and dead-reckoned
    estimation from the meter plus the solar model.  The dispatch optimiser
    works against all three; only its accuracy changes.
    """

    name: str = "base"
    #: True when the inverter will actually accept a charge/discharge command.
    controllable: bool = False

    @abc.abstractmethod
    def read(self) -> dict[str, float]:
        """Return solar_w, battery_w, soc, and whatever else is available."""

    def command_battery(self, watts: float) -> bool:
        """Positive discharges, negative charges.  False if not supported."""
        return False

    def open(self) -> None:  # pragma: no cover - trivial default
        pass

    def close(self) -> None:  # pragma: no cover - trivial default
        pass
