# ⚡ Udyog IQ

**Edge AI energy intelligence for small industry — on one Arduino UNO Q, with one energy meter, and no cloud.**

Built for the **Arduino Physical AI Challenge India 2026** · Track: *Industrial & Sustainability AI*

---

## The idea in one paragraph

A small factory has a dozen machines, one electricity connection, and an
electricity bill nobody understands. Metering every machine is expensive, so
nobody does it. Udyog IQ puts **one** energy meter on the incoming supply and an
Arduino UNO Q next to it. From that single aggregate signal the node works out —
by itself, with no labelled training data — *which* machines are running, *what
state* each one is in, *which one is starting to fail*, and *how much money is
being burned on idle.* Then it schedules solar, battery and grid against
time-of-day tariffs so the plant actually pays less.

Everything runs on the board. Nothing leaves the site.

## Why this needs an UNO Q specifically

The UNO Q has two brains on one board, and this project genuinely needs both:

| Brain | Runs | Why it has to be this one |
|---|---|---|
| **STM32U585** (Cortex-M33) | Modbus RTU master, contactor safety interlock, watchdog | Deterministic timing. A Linux scheduler hiccup must never stall a protective trip. |
| **Qualcomm QRB2210** (quad A53, Debian) | NILM, anomaly detection, forecasting, dispatch optimiser, dashboard | Needs a real filesystem, scikit-learn, and enough compute to re-solve a 96-step optimisation every 15 minutes. |

One board replaces a **PLC + protocol gateway + edge PC**. An ESP32 cannot run
the learning half. A Raspberry Pi cannot promise the real-time half. That
division of labour is the reason this project exists on this hardware.

## What it actually does

### 1. Discovers your machines from one meter — NILM
Non-Intrusive Load Monitoring. Every time a machine switches on or off it leaves
a step in active and reactive power. The node clusters those step signatures and
recovers the individual machines from the aggregate. **You do not need a meter
per machine.**

### 2. Learns healthy behaviour without being told what healthy is
There is no labelled fault dataset for your specific lathe. So the node watches,
builds a baseline of normal operation, and flags departures from it —
reconstruction error from an autoencoder plus an isolation forest, tracked as a
per-machine health score that trends over time.

### 3. Predicts tomorrow
- **Load forecast** — multi-horizon gradient boosting, self-supervised on the site's own history.
- **Solar forecast** — Open-Meteo weather feed through a clear-sky physics model, with a *learned residual correction* that adapts to your roof's shading, tilt error and dust.

### 4. Decides when to use what — the part that saves money
A dynamic-programming optimiser wrapped in a receding-horizon MPC loop re-solves
the next 24 hours every 15 minutes:

```
minimise   ToD energy cost + maximum-demand penalty + battery degradation cost
subject to load always served, SoC and C-rate limits,
           critical loads never shed
```

Battery degradation is priced in explicitly, so the optimiser will not wreck a
battery to chase a small arbitrage.

### 5. Proves it worked
The node runs a **counterfactual in parallel** — what the same day would have
cost on naive grid-only operation — and reports the delta in rupees and kg CO2e.
The device measures its own ROI.

## Architecture

```
  Selec EM2M-1P-C-100A --RS485--> [ STM32U585 ]  Modbus master + safety interlock
   (100 A, single phase)            |  Bridge RPC
                                    v
                            [ QRB2210 / Debian ]
                                    |
   Open-Meteo --> weather cache ----+
                                    +- pipeline   features, change-point events
                                    +- ml         nilm - states - health
                                    |             load forecast - solar forecast
                                    +- policy     DP + MPC dispatch, counterfactual
                                    +- sustain    ToD tariff, CO2e, cost ledger
                                    +- api        FastAPI + WebSocket
                                    |                   |
                                    |         mobile PWA dashboard
                                    v
                        contactor / inverter command
```

## Repository layout

```
app.yaml  python/  sketch/     App Lab project (FQBN arduino:zephyr:unoq)
udyogiq/
├── transport/   bridge | serial | sim | inverter adapters
├── meter/       Selec EM2M register map and decoding
├── pipeline/    ring buffer, feature extraction, change-point events
├── ml/          nilm · states · health · forecast · solar · battery
├── sustain/     ToD tariff, weather client, CO2e and cost accounting
├── policy/      DP dispatch optimiser, MPC loop, counterfactual, actuation
├── store/       SQLite historian
└── api/         FastAPI + WebSocket server
web/             mobile-first PWA dashboard
sim/             virtual meter, synthetic plant loads, solar and battery
tools/           hardware bring-up probe, dataset recorder, offline trainer
docs/            architecture, wiring, BOM, register map, assumptions
```

## The dashboard

Two views, both served from the board and both usable on a phone:

- **Overview** — live power and energy flows, discovered machines with health,
  savings against the counterfactual, tariff position, and the decision log.
- **Forecast** — the next 24 hours: predicted load against expected solar with
  peak-tariff bands behind it, the battery schedule with its state-of-charge
  trajectory, what the plan costs against doing nothing, per-horizon load
  predictions with their empirical intervals, and the training state of every
  model.

The forecast view answers the question the overview cannot: not "what is
happening" but "what is about to happen, and what does the node intend to do
about it".

## Running it without hardware

The whole system runs against a simulated plant, so you can develop, train and
demo before anything is wired:

```bash
python -m udyogiq.runtime --transport sim
```

Then open `http://localhost:8080` on a phone on the same network.

## Running it on the UNO Q

```bash
arduino-app run
```

See [`docs/wiring.md`](docs/wiring.md) for the RS485 connections and
[`docs/bringup.md`](docs/bringup.md) for verifying the meter register map
against your unit's LCD before trusting any number.

## Honest limitations

- At ~1 Hz Modbus polling this is **not** motor current signature analysis.
  True MCSA needs kHz current sampling. Predictive maintenance here is
  trend-and-anomaly detection on aggregate electrical parameters — real, but a
  different technique, and claimed as such.
- NILM resolves loads that are separated in power and reactive signature. Two
  near-identical motors switching together will be reported as one.
- Battery dispatch is advisory unless the inverter accepts external commands.

## Licence

MIT
