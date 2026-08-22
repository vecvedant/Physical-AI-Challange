"""
Register map and decoder for the Selec EM2M-1P-C-100A single-phase energy meter.

The meter exposes its measurements as 32-bit IEEE-754 floats in the Modbus
*input register* space (function code 04), two 16-bit registers per value,
starting at 30001.  Seventeen parameters occupy 30001-30034.

A warning about trusting this file
----------------------------------
Six of these offsets are confirmed against Selec's published parameter list.
The rest are inferred from the documented parameter set and the standard
ordering Selec uses across the EM series.  Meter firmware revisions do move
things around, and a wrong offset does not raise an error - it silently yields
a plausible-looking wrong number, which would then be baked into a trained
model.

So: run ``tools/probe_meter.py`` against your actual unit before trusting any
of this.  It dumps the whole block, cross-checks the physics (S vs V*I, P vs
S*PF), and tells you which offsets disagree with the meter's own LCD.  The map
below is overridable from config so a correction never needs a code change.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum

from .frame import MeterFrame


class Confidence(str, Enum):
    """How much we trust an offset before the probe tool has run."""

    CONFIRMED = "confirmed"   # in Selec's published parameter list
    PROBABLE = "probable"     # inferred from the documented set and EM-series ordering


@dataclass(frozen=True)
class Register:
    """One 32-bit float parameter in the input-register block."""

    name: str            # attribute on MeterFrame, or "" if unmapped
    modbus_address: int  # 1-based 3xxxx address as printed in the manual
    unit: str
    confidence: Confidence
    scale: float = 1.0   # applied after decoding, e.g. kW -> W
    description: str = ""

    @property
    def offset(self) -> int:
        """Zero-based offset used on the wire for function code 04."""
        return self.modbus_address - 30001


# Ordered exactly as they sit on the wire.  Do not reorder; the reader relies
# on contiguity to fetch the whole block in one transaction.
REGISTER_MAP: tuple[Register, ...] = (
    Register("total_active_energy_kwh", 30001, "kWh", Confidence.PROBABLE,
             description="Cumulative active energy, both directions"),
    Register("total_reactive_energy_kvarh", 30003, "kVArh", Confidence.PROBABLE,
             description="Cumulative reactive energy"),
    Register("apparent_energy_kvah", 30005, "kVAh", Confidence.PROBABLE,
             description="Cumulative apparent energy"),
    Register("", 30007, "kVArh", Confidence.PROBABLE,
             description="Export reactive energy (not consumed by Udyog IQ)"),

    # --- the six anchors we are sure of ------------------------------------
    Register("active_power_w", 30009, "W", Confidence.CONFIRMED,
             description="Instantaneous active power"),
    Register("reactive_power_var", 30011, "VAr", Confidence.CONFIRMED,
             description="Instantaneous reactive power"),
    Register("apparent_power_va", 30013, "VA", Confidence.CONFIRMED,
             description="Instantaneous apparent power"),
    Register("voltage_v", 30015, "V", Confidence.CONFIRMED,
             description="Line-to-neutral voltage"),
    Register("current_a", 30017, "A", Confidence.CONFIRMED,
             description="Line current"),
    Register("power_factor", 30019, "", Confidence.CONFIRMED,
             description="Power factor, sign indicates lead/lag"),
    # -----------------------------------------------------------------------

    Register("frequency_hz", 30021, "Hz", Confidence.PROBABLE,
             description="Supply frequency"),
    Register("import_active_energy_kwh", 30023, "kWh", Confidence.PROBABLE,
             description="Energy drawn from the source"),
    Register("export_active_energy_kwh", 30025, "kWh", Confidence.PROBABLE,
             description="Energy delivered back to the source"),
    Register("", 30027, "kVArh", Confidence.PROBABLE,
             description="Import reactive energy"),
    Register("", 30029, "kVArh", Confidence.PROBABLE,
             description="Export reactive energy"),
    Register("max_demand_active_w", 30031, "W", Confidence.PROBABLE,
             description="Meter's own maximum demand register, active"),
    Register("max_demand_apparent_va", 30033, "VA", Confidence.PROBABLE,
             description="Meter's own maximum demand register, apparent"),
)

#: Contiguous block covering every parameter: 17 floats == 34 registers.
BLOCK_START = REGISTER_MAP[0].offset
BLOCK_LENGTH = REGISTER_MAP[-1].offset + 2 - BLOCK_START

#: Subset worth polling at full rate when we want a fast loop.  The energy
#: counters barely move at 1 Hz, so a reduced sweep can skip them.
FAST_BLOCK_START = REGISTER_MAP[4].offset      # active power
FAST_BLOCK_LENGTH = REGISTER_MAP[10].offset + 2 - FAST_BLOCK_START  # .. frequency


# --------------------------------------------------------------------------- #
# Float decoding
# --------------------------------------------------------------------------- #
def decode_float(high: int, low: int,
                 word_order: str = "big", byte_order: str = "big") -> float:
    """
    Turn two Modbus registers into a float.

    Modbus has no opinion about how a 32-bit value spans two registers, so
    vendors disagree.  Selec ships big-endian words, but field units have been
    seen byte-swapped, hence both knobs.
    """
    words = (high, low) if word_order == "big" else (low, high)
    endian = ">" if byte_order == "big" else "<"
    raw = struct.pack(f"{endian}HH", words[0] & 0xFFFF, words[1] & 0xFFFF)
    return struct.unpack(f"{endian}f", raw)[0]


def encode_float(value: float,
                 word_order: str = "big", byte_order: str = "big") -> tuple[int, int]:
    """Inverse of :func:`decode_float`; used by the simulated meter."""
    endian = ">" if byte_order == "big" else "<"
    raw = struct.pack(f"{endian}f", value)
    high, low = struct.unpack(f"{endian}HH", raw)
    return (high, low) if word_order == "big" else (low, high)


def decode_block(registers: list[int], *,
                 start_offset: int = BLOCK_START,
                 word_order: str = "big",
                 byte_order: str = "big",
                 slave_id: int = 1,
                 source: str = "modbus",
                 timestamp: float | None = None) -> MeterFrame:
    """
    Decode a contiguous run of input registers into a :class:`MeterFrame`.

    Registers outside the supplied window are simply left at their defaults, so
    this works for both the full sweep and the reduced fast block.
    """
    frame = MeterFrame(source=source, slave_id=slave_id)
    if timestamp is not None:
        frame.timestamp = timestamp

    end_offset = start_offset + len(registers)
    for reg in REGISTER_MAP:
        if not reg.name:
            continue
        idx = reg.offset - start_offset
        if idx < 0 or reg.offset + 2 > end_offset:
            continue
        value = decode_float(registers[idx], registers[idx + 1],
                             word_order=word_order, byte_order=byte_order)
        # A NaN from a meter that has not yet latched a value would poison every
        # downstream average, so clamp it out here rather than three layers up.
        if value != value or value in (float("inf"), float("-inf")):
            value = 0.0
        setattr(frame, reg.name, value * reg.scale)

    return frame


def registers_by_name() -> dict[str, Register]:
    return {r.name: r for r in REGISTER_MAP if r.name}


def unconfirmed_registers() -> tuple[Register, ...]:
    """Everything the probe tool should ask a human to eyeball against the LCD."""
    return tuple(r for r in REGISTER_MAP
                 if r.name and r.confidence is not Confidence.CONFIRMED)
