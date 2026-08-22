# Architecture

## The shape of the problem

One meter. Many machines. No labels. No cloud. That combination determines
almost every decision below.

```
                    ┌──────────────────────────────────────────┐
   Selec EM2M ──────┤  STM32U585  (Cortex-M33, real time)      │
   RS485 Modbus     │   • Modbus RTU master, 1 Hz              │
                    │   • contactor interlock (authoritative)  │
                    │   • watchdog, fail-safe closed           │
                    └───────────────┬──────────────────────────┘
                                    │  Bridge RPC (Linux initiates)
                    ┌───────────────┴──────────────────────────┐
                    │  QRB2210  (quad A53, Debian)             │
                    │                                          │
   Open-Meteo ─────►│  pipeline   ring buffer → features       │
   (cached)         │             → change-point events        │
                    │  ml         NILM → per-machine health    │
                    │             load forecast, solar forecast│
                    │  policy     DP dispatch + MPC            │
                    │             idle cutoff, demand guard    │
                    │  sustain    ToD tariff, CO₂, savings     │
                    │  store      SQLite historian             │
                    │  api        FastAPI + WebSocket          │
                    └───────────────┬──────────────────────────┘
                                    │  LAN only
                              mobile PWA dashboard
```

## Why two brains, concretely

This is the argument the whole project rests on, so it is worth being precise
rather than hand-waving about "real-time".

**RS485 has a turnaround deadline.** The transceiver's driver must be disabled
within roughly a character time of the last stop bit, or it holds the line and
the slave's reply collides with our own echo. Linux will meet that deadline
almost every time. "Almost" produces a corrupted frame every few minutes that
looks exactly like a wiring fault and is miserable to diagnose. On the MCU the
turnaround is a few instructions after the UART flushes.

**A contactor interlock must survive the thing above it failing.** Minimum
dwell times and the switching-rate cap are what stop a bug from chattering a
physical contactor to death. Enforcing them on the MCU means they still hold
when the MPU hangs, fills its disk, or is being updated. The Python policy
engine checks the same rules first, but purely so it does not issue requests
that would be refused — it is not the safety mechanism.

**The learning half needs a real computer.** scikit-learn, a SQLite historian,
a 96-step optimisation re-solved every fifteen minutes, and a web server. That
is not microcontroller work.

An ESP32 cannot do the second half. A Raspberry Pi cannot promise the first.
One UNO Q replaces a PLC, a protocol gateway and an edge PC.

## Data flow, one sample at a time

1. **Acquire** — the MCU polls the meter and caches the decoded block; Python
   pulls the snapshot over the Bridge and rejects it if it is stale, because a
   repeated stale frame is indistinguishable from a genuinely steady load and
   would quietly corrupt NILM.
2. **Compensate** — with the meter at the grid tie, generation is added back to
   recover the load-side signal. Solar moves independently of the machines, and
   a cloud crossing otherwise looks exactly like a machine starting.
3. **Detect** — an adaptive-threshold change-point detector turns the power
   trace into discrete switching events carrying a (ΔP, ΔQ) signature.
4. **Disaggregate** — those signatures are clustered online into machines.
5. **Diagnose** — each machine's *start* events are scored against a density
   model of its own learned normal.
6. **Forecast** — load from the site's own history, solar from weather through
   clear-sky physics plus a learned residual.
7. **Decide** — dynamic programming over discretised state of charge, wrapped
   in a receding-horizon MPC loop.
8. **Act** — through the interlock, in advisory mode until someone deliberately
   enables actuation.
9. **Account** — a shadow world with no battery movement and no idle cutoff
   runs alongside, so the saving is a measured difference rather than a claim.

## Why disaggregation comes before diagnosis

This ordering was not obvious and cost a rewrite. The first health model scored
the plant's *aggregate* feature windows. It appeared to work and did not: on
held-out healthy data it scored 0.767 mean anomaly against 0.498 for genuinely
degraded data. Inverted.

The reason is that an aggregate window changes far more when a different mix of
machines happens to be running than it does when one machine degrades. The
model had learned the shift roster, and a quiet afternoon looked more alarming
than a failing compressor.

Health is therefore scored per machine, on the switching events NILM has
already attributed to it. Each such event belongs to exactly one machine and is
unaffected by whatever else is running. You cannot diagnose what you have not
first separated.

## Threading

One thread owns acquisition and every estimator that must see samples in order.
Everything expensive runs on a cooperative timer in the same loop. At 1 Hz
there is an enormous amount of idle time between samples, and one schedule is
far easier to reason about than five threads contending for a single SQLite
connection. The API server reads a snapshot under a lock and mutates nothing.

No periodic task may take the loop down. A weather API returning HTML, a model
throwing on a degenerate window, a full disk — each is logged and skipped. The
one thing this device must never do is stop measuring, because the historian is
the only record the site has.

## Offline posture

Nothing needs the internet except the weather feed, and that degrades in three
tiers: live fetch, then the disk cache while it is fresh, then climatology
built from the site's own measured history. A system that stops optimising when
the link drops gets unplugged.
