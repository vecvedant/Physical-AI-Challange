#!/usr/bin/env python3
"""
Hardware bring-up: find the meter, prove the register map, and print the config.

Run this before trusting a single number out of this system.

The register map in ``udyogiq/meter/selec_em2m.py`` has six offsets confirmed
against Selec's published parameter list and eleven inferred from it.  A wrong
offset does not raise an error - it returns a plausible-looking wrong number
that would then be trained on for a fortnight before anyone noticed the
compressor was 400 watts heavier than it should be.  Word order is the same
class of problem: swap it and every float becomes garbage that still decodes.

So this tool does not trust the map either.  It sweeps baud rates and slave
ids, dumps the whole input-register block, and checks the readings against
physics that must hold on any single-phase supply:

    S  ~=  V * I          apparent power against volts times amps
    P  ~=  S * PF         active power against apparent times power factor
    S^2 >= P^2 + Q^2      apparent power cannot be smaller than its components

Those three are enough to catch a wrong word order, a wrong offset, and a
half-connected CT, and they need no reference instrument. What they cannot
catch is a *consistent* relabelling, so the tool finishes by printing the
values it believes it found and asking you to check them against the meter's
own LCD. That takes fifteen seconds and it is the only way to be sure.

    python tools/probe_meter.py --port COM5
    python tools/probe_meter.py --port /dev/ttyUSB0 --scan
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from udyogiq.meter import selec_em2m as em2m       # noqa: E402
from udyogiq.meter.frame import MeterFrame          # noqa: E402

COMMON_BAUDS = (9600, 19200, 4800, 38400, 115200)
DIM, BOLD, OK, WARN, BAD, END = "\033[2m", "\033[1m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def _plain(text: str) -> str:
    for code in (DIM, BOLD, OK, WARN, BAD, END):
        text = text.replace(code, "")
    return text


class Printer:
    def __init__(self, colour: bool) -> None:
        self.colour = colour

    def __call__(self, text: str = "") -> None:
        print(text if self.colour else _plain(text))


def open_client(port: str, baud: int, parity: str, stopbits: int, timeout: float):
    from pymodbus.client import ModbusSerialClient
    client = ModbusSerialClient(port=port, baudrate=baud, parity=parity,
                                stopbits=stopbits, bytesize=8, timeout=timeout)
    return client if client.connect() else None


def read_block(client, slave: int, start: int, count: int):
    """Read input registers, papering over the pymodbus keyword rename."""
    for kw in ("slave", "unit", "device_id"):
        try:
            result = client.read_input_registers(start, count=count, **{kw: slave})
        except TypeError:
            continue
        except Exception:
            return None
        if result is None or (hasattr(result, "isError") and result.isError()):
            return None
        return list(result.registers)
    try:
        result = client.read_input_registers(start, count)
    except Exception:
        return None
    if result is None or (hasattr(result, "isError") and result.isError()):
        return None
    return list(result.registers)


def score_frame(frame: MeterFrame) -> tuple[float, list[str]]:
    """
    How physically plausible is this decode? Lower is better.

    Returns a penalty and the human-readable reasons behind it, so a bad word
    order can be told apart from a bad offset.
    """
    penalty = 0.0
    notes: list[str] = []

    if not (150.0 <= frame.voltage_v <= 300.0):
        penalty += 100.0
        notes.append(f"voltage {frame.voltage_v:.1f} V is not a mains voltage")
    if not (0.0 <= frame.current_a <= 120.0):
        penalty += 100.0
        notes.append(f"current {frame.current_a:.2f} A is out of range for a 100 A meter")
    if not (0.0 <= abs(frame.power_factor) <= 1.001):
        penalty += 100.0
        notes.append(f"power factor {frame.power_factor:.3f} is impossible")
    if frame.frequency_hz and not (45.0 <= frame.frequency_hz <= 65.0):
        penalty += 50.0
        notes.append(f"frequency {frame.frequency_hz:.2f} Hz is not a mains frequency")

    vi = frame.voltage_v * abs(frame.current_a)
    if vi > 20.0 and frame.apparent_power_va > 1.0:
        err = abs(frame.apparent_power_va - vi) / max(vi, 1.0)
        penalty += err * 40.0
        if err > 0.15:
            notes.append(f"S={frame.apparent_power_va:.0f} VA disagrees with "
                         f"V*I={vi:.0f} VA by {err*100:.0f}%")

    if frame.apparent_power_va > 20.0 and abs(frame.power_factor) > 0.05:
        expect = frame.apparent_power_va * abs(frame.power_factor)
        err = abs(abs(frame.active_power_w) - expect) / max(expect, 1.0)
        penalty += err * 40.0
        if err > 0.15:
            notes.append(f"P={frame.active_power_w:.0f} W disagrees with "
                         f"S*PF={expect:.0f} W by {err*100:.0f}%")

    import math
    hyp = math.hypot(frame.active_power_w, frame.reactive_power_var)
    if hyp > 20.0 and frame.apparent_power_va > 1.0:
        if frame.apparent_power_va < hyp * 0.85:
            penalty += 30.0
            notes.append("apparent power is smaller than sqrt(P^2 + Q^2), "
                         "which cannot happen")
    return penalty, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify a Selec EM2M against its register map")
    ap.add_argument("--port", required=True, help="e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=None, help="skip the baud sweep")
    ap.add_argument("--slave", type=int, default=None, help="skip the address sweep")
    ap.add_argument("--parity", default="N", choices=("N", "E", "O"))
    ap.add_argument("--stopbits", type=int, default=1, choices=(1, 2))
    ap.add_argument("--scan", action="store_true", help="sweep slave ids 1-16")
    ap.add_argument("--timeout", type=float, default=0.5)
    ap.add_argument("--samples", type=int, default=5, help="readings to average")
    ap.add_argument("--no-colour", action="store_true")
    args = ap.parse_args(argv)

    say = Printer(colour=not args.no_colour)

    try:
        import pymodbus  # noqa: F401
    except ImportError:
        say(f"{BAD}pymodbus is not installed.{END}  pip install -r python/requirements.txt")
        return 2

    say(f"\n{BOLD}Udyog IQ meter probe{END}")
    say(f"{DIM}port {args.port} · parity {args.parity} · {args.stopbits} stop bit(s){END}\n")

    bauds = [args.baud] if args.baud else list(COMMON_BAUDS)
    slaves = [args.slave] if args.slave else (list(range(1, 17)) if args.scan else [1])

    # ---------------------------------------------------------------- #
    # 1. find something that answers
    # ---------------------------------------------------------------- #
    found = None
    for baud in bauds:
        client = open_client(args.port, baud, args.parity, args.stopbits, args.timeout)
        if client is None:
            say(f"{BAD}Cannot open {args.port}.{END}")
            say(f"{DIM}  Check the device exists, that you are in the 'dialout' "
                f"group on Linux, and that nothing else holds the port.{END}")
            return 2
        for slave in slaves:
            regs = read_block(client, slave, em2m.BLOCK_START, em2m.BLOCK_LEN)
            tag = f"  {baud:>6} baud  slave {slave:>2}"
            if regs:
                say(f"{tag}   {OK}responded{END}")
                found = (baud, slave, client)
                break
            say(f"{tag}   {DIM}silence{END}")
        if found:
            break
        client.close()

    if not found:
        say(f"\n{BAD}Nothing answered.{END} Things worth checking, in order:")
        say("  1. A and B swapped - by far the most common fault, and it looks "
            "exactly like a dead meter")
        say("  2. No common ground between the converter and the meter")
        say("  3. Missing 120 ohm termination on a long run")
        say("  4. Meter's own address and baud rate, set from its front panel")
        say("  5. If the STM32 is mastering the bus, it is driving these same "
            "wires - stop the App Lab app before probing from Linux")
        return 1

    baud, slave, client = found
    say(f"\n{OK}Meter found{END} at {BOLD}{baud} baud, slave {slave}{END}\n")

    # ---------------------------------------------------------------- #
    # 2. decide the word order from physics
    # ---------------------------------------------------------------- #
    say(f"{BOLD}Word order{END}")
    best = None
    for word_order in ("big", "little"):
        for byte_order in ("big", "little"):
            regs = read_block(client, slave, em2m.BLOCK_START, em2m.BLOCK_LEN)
            if not regs:
                continue
            frame = em2m.decode_block(regs, word_order=word_order,
                                      byte_order=byte_order, slave_id=slave,
                                      source="probe")
            penalty, notes = score_frame(frame)
            mark = OK if penalty < 5 else (WARN if penalty < 50 else BAD)
            say(f"  words={word_order:<6} bytes={byte_order:<6} "
                f"{mark}penalty {penalty:7.1f}{END}   "
                f"{DIM}V={frame.voltage_v:.1f} I={frame.current_a:.2f} "
                f"P={frame.active_power_w:.0f}{END}")
            if best is None or penalty < best[0]:
                best = (penalty, word_order, byte_order, frame)

    penalty, word_order, byte_order, frame = best
    if penalty >= 50:
        say(f"\n{BAD}No word order gives physically sensible readings.{END}")
        say(f"{DIM}  The register offsets are probably wrong for this firmware "
            f"revision. The raw dump below is what to send to whoever maintains "
            f"this map.{END}")
    else:
        say(f"\n{OK}Best: word_order={word_order}, byte_order={byte_order}{END}")

    # ---------------------------------------------------------------- #
    # 3. average a few readings so noise is not mistaken for error
    # ---------------------------------------------------------------- #
    say(f"\n{BOLD}Readings{END} {DIM}(mean of {args.samples}){END}")
    frames = []
    for _ in range(args.samples):
        regs = read_block(client, slave, em2m.BLOCK_START, em2m.BLOCK_LEN)
        if regs:
            frames.append(em2m.decode_block(regs, word_order=word_order,
                                            byte_order=byte_order,
                                            slave_id=slave, source="probe"))
        time.sleep(0.2)

    if not frames:
        say(f"{BAD}Lost the meter mid-probe.{END}")
        return 1

    def mean(attr: str) -> float:
        return sum(getattr(f, attr) for f in frames) / len(frames)

    for reg in em2m.REGISTER_MAP:
        if not reg.name:
            continue
        value = mean(reg.name)
        conf = OK + "confirmed" + END if reg.confidence is em2m.Confidence.CONFIRMED \
            else WARN + "CHECK LCD" + END
        say(f"  {reg.modbus_address}  {reg.name:<30} {value:12.3f} {reg.unit:<6} {conf}")

    # ---------------------------------------------------------------- #
    # 4. verdict
    # ---------------------------------------------------------------- #
    final = MeterFrame(**{r.name: mean(r.name) for r in em2m.REGISTER_MAP if r.name})
    penalty, notes = score_frame(final)
    say(f"\n{BOLD}Physics check{END}")
    if not notes:
        say(f"  {OK}All consistency checks passed.{END}")
    for note in notes:
        say(f"  {WARN}!{END} {note}")

    vi = final.voltage_v * abs(final.current_a)
    say(f"  {DIM}V*I = {vi:.0f} VA vs reported S = {final.apparent_power_va:.0f} VA{END}")
    say(f"  {DIM}S*PF = {final.apparent_power_va * abs(final.power_factor):.0f} W "
        f"vs reported P = {final.active_power_w:.0f} W{END}")

    say(f"\n{BOLD}Now check these against the meter's own display{END}")
    say(f"  voltage        {final.voltage_v:10.1f} V")
    say(f"  current        {final.current_a:10.2f} A")
    say(f"  active power   {final.active_power_w:10.0f} W")
    say(f"  power factor   {final.power_factor:10.3f}")
    say(f"  frequency      {final.frequency_hz:10.2f} Hz")
    say(f"  import energy  {final.import_active_energy_kwh:10.2f} kWh")
    say(f"{DIM}  The energy counter is the most useful one: it should match the "
        f"LCD exactly, and it is the reading most likely to expose a wrong "
        f"offset, because it is large and it only ever goes up.{END}")

    # ---------------------------------------------------------------- #
    # 5. hand back something to paste
    # ---------------------------------------------------------------- #
    blob = {"meter": {"transport": "serial", "port": args.port, "baudrate": baud,
                      "slave_id": slave, "parity": args.parity,
                      "stopbits": args.stopbits, "word_order": word_order,
                      "byte_order": byte_order}}
    say(f"\n{BOLD}Write this to config.local.json{END}")
    say(json.dumps(blob, indent=2))

    raw = read_block(client, slave, em2m.BLOCK_START, em2m.BLOCK_LEN)
    if raw:
        say(f"\n{DIM}Raw input registers {em2m.BLOCK_START}.."
            f"{em2m.BLOCK_START + em2m.BLOCK_LEN - 1}:{END}")
        say(DIM + " ".join(f"{r:04X}" for r in raw) + END)

    client.close()
    return 0 if penalty < 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())
