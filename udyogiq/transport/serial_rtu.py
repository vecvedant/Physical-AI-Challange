"""
Modbus RTU master running in-process on the Linux side.

Used when the RS485 converter presents itself to Debian as a serial device -
typically a USB-RS485 dongle on ``/dev/ttyUSB0``, but equally a UART exposed
through a Linux tty.  This is the easiest path to first light during bring-up
because it takes the MCU firmware out of the loop entirely: if this works and
the Bridge transport does not, the problem is the sketch, not the wiring.
"""

from __future__ import annotations

import logging
import time

from ..config import CONFIG
from ..meter import selec_em2m as em2m
from ..meter.frame import MeterFrame
from .base import MeterTransport, TransportError

log = logging.getLogger(__name__)


class SerialModbusMeter(MeterTransport):
    """Polls the meter directly with pymodbus."""

    name = "serial"

    def __init__(self, *, port: str | None = None, slave_id: int | None = None,
                 baudrate: int | None = None, full_sweep: bool = True) -> None:
        super().__init__()
        cfg = CONFIG.meter
        self.port = port or cfg.port
        self.slave_id = slave_id if slave_id is not None else cfg.slave_id
        self.baudrate = baudrate or cfg.baudrate
        self.full_sweep = full_sweep
        self._client = None
        # pymodbus renamed the slave keyword between major versions; resolved
        # once on first use rather than guessed on every read.
        self._slave_kw: str | None = None

    # ------------------------------------------------------------------ #
    def open(self) -> None:
        if self._client is not None:
            return
        try:
            from pymodbus.client import ModbusSerialClient
        except ImportError as exc:  # pragma: no cover - deployment issue
            raise TransportError(
                "pymodbus is not installed; pip install -r python/requirements.txt"
            ) from exc

        cfg = CONFIG.meter
        self._client = ModbusSerialClient(
            port=self.port,
            baudrate=self.baudrate,
            parity=cfg.parity,
            stopbits=cfg.stopbits,
            bytesize=cfg.bytesize,
            timeout=cfg.timeout_s,
        )
        if not self._client.connect():
            self._client = None
            raise TransportError(
                f"could not open {self.port}. Check the device exists, that the "
                f"user is in the 'dialout' group, and that nothing else holds it."
            )
        log.info("Modbus RTU open on %s @ %d baud, slave %d",
                 self.port, self.baudrate, self.slave_id)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    # ------------------------------------------------------------------ #
    def _call_read(self, address: int, count: int):
        """Read input registers, papering over the pymodbus keyword rename."""
        if self._slave_kw is None:
            for kw in ("slave", "unit", "device_id"):
                try:
                    result = self._client.read_input_registers(
                        address, count=count, **{kw: self.slave_id})
                except TypeError:
                    continue
                self._slave_kw = kw
                return result
            # No keyword worked; fall back to positional-only form.
            self._slave_kw = ""
            return self._client.read_input_registers(address, count)

        if self._slave_kw == "":
            return self._client.read_input_registers(address, count)
        return self._client.read_input_registers(
            address, count=count, **{self._slave_kw: self.slave_id})

    def _read(self) -> MeterFrame:
        if self._client is None:
            self.open()

        start = em2m.BLOCK_START if self.full_sweep else em2m.FAST_BLOCK_START
        length = em2m.BLOCK_LENGTH if self.full_sweep else em2m.FAST_BLOCK_LENGTH

        cfg = CONFIG.meter
        last_exc: Exception | None = None
        for attempt in range(cfg.retries + 1):
            try:
                result = self._call_read(start, length)
            except Exception as exc:                    # noqa: BLE001
                last_exc = exc
                time.sleep(0.05 * (attempt + 1))
                continue

            if result is None or (hasattr(result, "isError") and result.isError()):
                last_exc = TransportError(f"modbus exception: {result}")
                time.sleep(0.05 * (attempt + 1))
                continue

            return em2m.decode_block(
                list(result.registers),
                start_offset=start,
                word_order=cfg.word_order,
                byte_order=cfg.byte_order,
                slave_id=self.slave_id,
                source="modbus-serial",
                timestamp=time.time(),
            )

        raise TransportError(
            f"no response from slave {self.slave_id} on {self.port} "
            f"after {cfg.retries + 1} attempts: {last_exc}"
        )
