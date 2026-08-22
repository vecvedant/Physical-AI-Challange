# Wiring

## Warning

The meter's voltage terminals sit at mains potential and its current path
carries the full load. Do not work on this live. If you are not competent to
terminate a 100 A single-phase supply, have someone who is do it.

## Overview

```
   L ───────┬────────────────────────────►  load
            │
    ┌───────┴──────────────┐
    │  Selec EM2M-1P-C     │   direct connected, no CT
    │  100 A, single phase │
    │                      │
    │   A(+)   B(-)   GND  │   RS485, Modbus RTU
    └────┬──────┬──────┬───┘
         │      │      │        screened 3-core, screen earthed at ONE end
    ┌────┴──────┴──────┴───┐
    │  isolated RS485 to   │   galvanic isolation
    │  UART converter      │
    │  RX   TX   DE  3V3 GND│
    └────┬────┬────┬───┬──┬─┘
         │    │    │   │  │
  UNO Q  D1   D0   D2 3V3 GND
        (TX) (RX) (DE)
```

## Why the isolation matters

The meter sits on the mains side. The UNO Q is a Linux computer with a USB-C
port that will, at some point, be plugged into a laptop. Without galvanic
isolation, a fault or even a difference in earth potential between the two puts
mains-referenced voltage onto the board's UART pins and from there into
whatever is attached. The isolated converter is the only thing between a wiring
fault and a destroyed board, or a destroyed laptop with a person attached to it.

## Pin assignment

| Signal | UNO Q pin | STM32 pin | Notes |
|---|---|---|---|
| RS485 TX | D1 | PB6 | `usart1` TX per the Zephyr device tree |
| RS485 RX | D0 | PB7 | `usart1` RX |
| RS485 DE/RE | D2 | PB3 | Tie DE and RE together; HIGH transmits |
| Contactor | D7 | PB2 | Most relay boards are active low |
| 3V3, GND | — | — | Powers the converter's logic side |

**The UART is a single `#define` in the sketch.** The Zephyr device tree maps
D0/D1 to `usart1` and aliases it `arduino_serial`, and puts the MPU router on
`lpuart1` (PG5–PG8, hardware flow control, not brought out to the headers). One
published tutorial states `Serial1` is reserved for the router, contradicting
the device tree. That was not resolvable without the board in hand, so it is
one line to change, and `tools/probe_meter.py` verifies the bus from the Linux
side independently of the sketch.

## RS485 practicalities

- **A and B swapped is the most common fault**, and it presents exactly like a
  dead meter: silence, no error. Try swapping before suspecting anything else.
- **Run the ground.** RS485 is differential but not magic; the transceivers
  need their common mode within range.
- **Terminate only long runs**: 120 ohm across A–B at each end of the bus, not
  on every device.
- **Earth the screen at one end only.** Both ends creates a ground loop that
  injects noise into the pair you were trying to protect.
- Set the meter's own address and baud rate from its front panel, and make them
  match `config.local.json`.
- If the STM32 sketch is running it is driving these same wires. Stop the App
  Lab app before probing the bus from Linux, or the two masters will collide.

## Multi-drop

The bus takes up to 32 devices. A hybrid inverter with a Modbus interface can
be dropped onto the same pair at a different slave id, which turns solar
generation and battery state from *estimated* into *measured*. That is the
intended scale-up path and it needs no wiring beyond the daisy chain.
