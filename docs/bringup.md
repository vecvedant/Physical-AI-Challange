# Bring-up

The order below is deliberate: each step removes a class of failure before the
next step can be confused by it. Do not skip ahead — a meter that answers is
not the same as a meter that is being read correctly, and that difference is
invisible if you jump straight to running the app.

## 0. Before anything is wired

Run the whole system against the simulator. It needs no hardware and proves the
software half works on this machine:

```bash
python -m udyogiq.runtime --transport sim --warmup 4
```

Open `http://<this-machine>:8080` on a phone on the same network. You should
see machines discovered, a battery plan, and a tariff strip. If this does not
work, fix it before introducing mains voltage to the problem.

## 1. Meter alone, on the bench

Wire the meter to a load you do not mind losing — a lamp, a fan, a kettle. Set
its address and baud rate from the front panel and note them.

## 2. Prove the bus from Linux

Stop the App Lab app first: if the STM32 sketch is running it is driving the
same wires, and two masters on one bus produce nothing but collisions.

```bash
python tools/probe_meter.py --port /dev/ttyUSB0 --scan
```

This sweeps baud rates and slave ids, decides the word order from physics, and
prints readings to check against the meter's LCD.

**If nothing answers**, in order of likelihood:

1. A and B swapped. By a wide margin the most common fault, and it looks
   exactly like a dead meter — silence, no error.
2. No common ground between converter and meter.
3. Wrong baud or address on the meter's front panel.
4. Missing 120 Ω termination on a long run.
5. The sketch is still running and fighting you for the bus.

**If it answers but the physics check fails**, the register offsets are wrong
for your firmware revision. The probe prints the raw register block; that dump
is what to work from.

## 3. Check the numbers against the LCD

The probe prints voltage, current, power, power factor and the energy counter.
Compare all five against the meter's own display.

Pay particular attention to the **energy counter**. It is large, it only ever
increases, and it is the reading most likely to expose a wrong offset — a
plausible-looking but wrong power reading is easy to miss, a kWh total that
disagrees with the display is not.

## 4. Write the config

Paste the block the probe prints into `config.local.json`, then correct the
site-specific values listed in [`assumptions.md`](assumptions.md) — location,
array size, battery cost, and above all your **actual tariff**. Until the
tariff is right, every rupee figure the dashboard shows is fiction.

## 5. Run against the real meter

```bash
python -m udyogiq.runtime --transport serial
```

Watch `/api/health`. It should report `ok: true` with `stale_s` under a second.

## 6. Move Modbus onto the MCU

Only now flash the sketch and switch to `transport: bridge`. Doing this last
means that if it breaks, you already know the wiring and the register map are
good, so the problem is in the sketch — most likely the `RS485_SERIAL` define.

## 7. Let it learn

NILM needs machines to switch several times before it will confirm them, and
the health models need about eighty starts each. A machine that cycles a few
times an hour is confirmed within a day; one that runs twice a day takes
proportionally longer. That is the honest cost of having no labels.

Watch the residual on the dashboard. Large and persistent means loads are
switching below the detection threshold, or machines are ramping rather than
stepping.

## 8. Only then, enable actuation

`policy.actuation_enabled` ships `false`. Leave it false until you have watched
the decision log for a day or two and agree with what it *would* have done.
Every advisory decision is logged with its reasoning precisely so this can be
judged before anything is switched.

When you do enable it, start with a load whose loss costs nothing.
