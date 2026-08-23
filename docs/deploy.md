# Deploying to the UNO Q

Do [`bringup.md`](bringup.md) first if the meter has never been read. This page
assumes the wiring is proved and only covers getting the software onto the
board.

## What runs where

| Half | File | Runs on |
|---|---|---|
| Real-time | `sketch/sketch.ino` | STM32U585 |
| Everything else | `python/main.py` → `udyogiq/` | QRB2210, Debian |

`app.yaml` ties them together. App Lab uploads the sketch to the MCU, installs
`python/requirements.txt`, and executes `python/main.py` on the MPU.

## Option A — App Lab

Open the project folder in App Lab and press run. It builds the sketch, flashes
the MCU, installs the Python dependencies and starts the app.

## Option B — command line

Useful when you would rather work in your own editor. Copy the project to the
board and run it there:

```bash
ssh arduino@<board-ip> "mkdir -p ~/ArduinoApps/udyogiq"
scp -r app.yaml python sketch udyogiq web sim tools docs arduino@<board-ip>:~/ArduinoApps/udyogiq/
ssh arduino@<board-ip>
cd ~/ArduinoApps/udyogiq
python3 -m pip install -r python/requirements.txt
arduino-app run .
```

To run only the Linux half while bringing things up — no sketch, no MCU
involvement:

```bash
python3 python/main.py
```

## Choosing the transport

`python/main.py` defaults to `bridge`, because on the board the STM32 owns the
RS485 bus. Override with an environment variable:

```bash
UDYOGIQ_METER_TRANSPORT=serial python3 python/main.py    # Linux masters Modbus
UDYOGIQ_METER_TRANSPORT=sim    python3 python/main.py    # no meter at all
```

**Use `serial` first.** It takes the sketch out of the loop entirely, so if it
works and `bridge` does not, the problem is firmware rather than wiring — most
likely the `RS485_SERIAL` define in the sketch. Only two things can be masters
on one bus, so stop the App Lab app before running a Linux-side master.

## Reaching the dashboard

```
http://<board-ip>:8080
```

Find the address with `hostname -I` on the board. Any phone, tablet or laptop
on the same network can open it; on a phone, "Add to Home Screen" installs it
as an app. Nothing is served outside the LAN.

## Running it as a service

So it survives a reboot and starts without anyone logging in:

```ini
# /etc/systemd/system/udyogiq.service
[Unit]
Description=Udyog IQ
After=network-online.target

[Service]
Type=simple
User=arduino
WorkingDirectory=/home/arduino/ArduinoApps/udyogiq
Environment=UDYOGIQ_METER_TRANSPORT=bridge
ExecStart=/usr/bin/python3 python/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now udyogiq
journalctl -u udyogiq -f
```

`Restart=on-failure` matters more than it looks. The node is the only record
the site has of its own consumption, so a crash at 3 a.m. must not mean a
missing morning. Shutdown is handled explicitly on SIGTERM so the historian
flushes and SQLite is not left with a hot journal.

## First run on real hardware

Expect the dashboard to look empty for a while, and that is correct behaviour
rather than a fault:

- **Machines** appear once they have switched enough times to form a cluster.
  A machine cycling a few times an hour is confirmed within a day; one used
  twice a day takes proportionally longer.
- **Health scores** need about eighty start events each before they leave
  "learning".
- **The load forecaster** needs roughly a day of history before it trains.
- **Savings** stay at zero until there is something to save — and legitimately
  stay there if the tariff spread does not cover battery degradation.

Watch the residual on the dashboard. Large and persistent means loads are
switching below the detection threshold, or machines are ramping rather than
stepping.

## Before quoting any number to anyone

Set the real values in `config.local.json` — tariff windows above all, then
battery cost, array geometry and location. See
[`assumptions.md`](assumptions.md). Until the tariff is right, every rupee
figure on the dashboard is arithmetic on a placeholder.

## Actuation stays off

`policy.actuation_enabled` ships `false`. Leave it there until you have watched
the decision log for a day or two and agree with what it *would* have done —
every advisory decision is logged with its reasoning precisely so that judgement
can be made before anything is switched. When you do enable it, start with a
load whose loss costs nothing.
