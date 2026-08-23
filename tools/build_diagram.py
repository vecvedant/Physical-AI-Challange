#!/usr/bin/env python3
"""
Draw the Udyog IQ system block diagram.

Deliberately a *system* diagram, not a compute one. An earlier version showed
only the two processors talking to each other, which answers "how is the
software arranged" when the question was "what is the installation". A reader
who has never seen the project needs to see where the electricity comes from,
where it is measured, what it feeds, and what the node can actually switch.

Layout follows the physical circuit, which also happens to be the easiest thing
to route cleanly: the power spine runs left to right along the top, sources into
the inverter, through the meter, through the contactor, into the machines. The
contactor really is in series there, so drawing it that way is accurate rather
than decorative. The UNO Q sits underneath with only signal lines going up to
the two things it touches, which keeps every arrow outside every box.

Two kinds of connection, drawn differently so the distinction survives a
photocopy:

    thick solid   power. Real current, kilowatts.
    thin dashed   signal. Measurements, commands, data.

Monochrome, matching the dashboard, and legible printed in black and white.

    python tools/build_diagram.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                                  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch    # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "diagrams"

INK = "#0d0d0f"
MID = "#5a5a64"
FAINT = "#9a9aa4"
WHITE = "#ffffff"
PANEL = "#f1f1f4"
BAND = "#e3e3e8"

TITLE_PT = 9.2
BODY_PT = 7.3
LINE_H = 0.040          # vertical space one body line occupies, in axes units
HEAD_H = 0.052          # space the title occupies


def box(ax, cx, cy, w, title, lines=(), *, fill=WHITE, lw=1.7,
        title_pt=TITLE_PT, body_pt=BODY_PT, dashed=False, min_h=0.10):
    """
    A labelled block centred on (cx, cy).

    Height is derived from the number of body lines rather than passed in,
    which is what stopped the first version overflowing its own boxes.
    """
    h = max(min_h, HEAD_H + LINE_H * len(lines) + 0.030)
    x, y = cx - w / 2, cy - h / 2
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.010,rounding_size=0.018",
        linewidth=lw, edgecolor=INK, facecolor=fill,
        linestyle=(0, (4, 3)) if dashed else "solid", zorder=3))

    if lines:
        ax.text(cx, y + h - 0.022, title, ha="center", va="top",
                fontsize=title_pt, fontweight="bold", color=INK, zorder=4)
        for i, line in enumerate(lines):
            ax.text(cx, y + h - HEAD_H - 0.012 - i * LINE_H, line,
                    ha="center", va="top", fontsize=body_pt, color=MID, zorder=4)
    else:
        ax.text(cx, cy, title, ha="center", va="center",
                fontsize=title_pt, fontweight="bold", color=INK, zorder=4)

    return {"cx": cx, "cy": cy, "w": w, "h": h,
            "l": x, "r": x + w, "t": y + h, "b": y}


def power(ax, a, b, label=None, *, rad=0.0, dx=0.0, dy=0.020):
    ax.add_patch(FancyArrowPatch(
        a, b, arrowstyle="-|>", mutation_scale=16, linewidth=2.5,
        color=INK, zorder=2, connectionstyle=f"arc3,rad={rad}",
        shrinkA=2, shrinkB=2))
    if label:
        ax.text((a[0] + b[0]) / 2 + dx, (a[1] + b[1]) / 2 + dy, label,
                ha="center", va="bottom", fontsize=7.0, color=INK,
                fontweight="bold", zorder=5,
                bbox=dict(boxstyle="round,pad=0.20", fc=WHITE, ec="none"))


def signal(ax, a, b, label=None, *, rad=0.0, dx=0.0, dy=0.018, both=False):
    ax.add_patch(FancyArrowPatch(
        a, b, arrowstyle="<|-|>" if both else "-|>", mutation_scale=11,
        linewidth=1.3, color=MID, zorder=2, linestyle=(0, (5, 3)),
        connectionstyle=f"arc3,rad={rad}", shrinkA=2, shrinkB=2))
    if label:
        ax.text((a[0] + b[0]) / 2 + dx, (a[1] + b[1]) / 2 + dy, label,
                ha="center", va="bottom", fontsize=6.9, color=MID,
                style="italic", zorder=5,
                bbox=dict(boxstyle="round,pad=0.18", fc=WHITE, ec="none"))


def build() -> Path:
    fig, ax = plt.subplots(figsize=(13.6, 7.4), dpi=210)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    fig.patch.set_facecolor(WHITE)

    SPINE = 0.775          # the power path runs along here

    # ---- sources -------------------------------------------------------- #
    solar = box(ax, 0.085, 0.930, 0.150, "SOLAR PV", ["3 kW rooftop array"])
    batt  = box(ax, 0.085, 0.775, 0.150, "BATTERY", ["5 kWh, 1.5 kW rate"])
    grid  = box(ax, 0.085, 0.620, 0.150, "UTILITY GRID", ["230 V, time of day tariff"])

    inv = box(ax, 0.255, SPINE, 0.130, "HYBRID INVERTER",
              ["couples all three"], fill=BAND)

    # ---- measurement and switching, in series on the spine -------------- #
    meter = box(ax, 0.445, SPINE, 0.155, "SELEC EM2M METER",
                ["EM2M-1P-C-100A", "100 A, class 1", "V, I, P, Q, PF, kWh"],
                fill=BAND)
    cont = box(ax, 0.640, SPINE, 0.130, "CONTACTOR",
               ["in series with the load"], fill=BAND)
    loads = box(ax, 0.860, SPINE, 0.215, "WORKSHOP MACHINES",
                ["compressor, coolant pump", "lathe, bench grinder",
                 "fan, lighting, office"])

    # ---- the node ------------------------------------------------------- #
    OUTER_L, OUTER_R, OUTER_B, OUTER_T = 0.285, 0.715, 0.180, 0.545
    ax.add_patch(FancyBboxPatch(
        (OUTER_L, OUTER_B), OUTER_R - OUTER_L, OUTER_T - OUTER_B,
        boxstyle="round,pad=0.010,rounding_size=0.020",
        linewidth=2.3, edgecolor=INK, facecolor=WHITE, zorder=3))
    ax.text((OUTER_L + OUTER_R) / 2, OUTER_T - 0.030, "ARDUINO UNO Q",
            ha="center", va="center", fontsize=11.0, fontweight="bold",
            color=INK, zorder=4)
    ax.text((OUTER_L + OUTER_R) / 2, OUTER_T - 0.066,
            "one board, two processors, nothing leaves the site",
            ha="center", va="center", fontsize=7.0, color=FAINT, zorder=4)

    mcu = box(ax, 0.392, 0.330, 0.180, "STM32U585",
              ["Cortex-M33, real time", "Modbus RTU master",
               "contactor interlock", "watchdog, fail safe"],
              fill=PANEL, lw=1.2, title_pt=8.5, body_pt=6.9)
    mpu = box(ax, 0.608, 0.330, 0.180, "QRB2210",
              ["quad Cortex-A53, Debian", "disaggregation, health",
               "forecasts, dispatch", "historian, dashboard"],
              fill=PANEL, lw=1.2, title_pt=8.5, body_pt=6.9)
    ax.add_patch(FancyArrowPatch(
        (mcu["r"], 0.330), (mpu["l"], 0.330), arrowstyle="<|-|>",
        mutation_scale=10, linewidth=1.2, color=MID, zorder=5))
    ax.text(0.500, 0.347, "Bridge RPC", ha="center", va="bottom",
            fontsize=6.8, color=MID, style="italic", zorder=5)

    # ---- people and the optional feed ----------------------------------- #
    dash = box(ax, 0.878, 0.470, 0.200, "DASHBOARD",
               ["phone, tablet or laptop", "overview and forecast",
                "local network only"])
    wx = box(ax, 0.878, 0.215, 0.200, "OPEN METEO",
             ["hourly sky forecast", "cached 72 hours", "optional"],
             dashed=True, lw=1.3)

    # ---- power spine ---------------------------------------------------- #
    power(ax, (solar["r"], solar["cy"]), (inv["cx"] - 0.020, inv["t"]), rad=-0.16)
    # Two heads: the battery is charged from the array or the grid, and
    # discharged into the plant. A single head would misstate the circuit.
    ax.add_patch(FancyArrowPatch(
        (batt["r"], batt["cy"]), (inv["l"], inv["cy"]), arrowstyle="<|-|>",
        mutation_scale=16, linewidth=2.5, color=INK, zorder=2,
        shrinkA=2, shrinkB=2))
    power(ax, (grid["r"], grid["cy"]), (inv["cx"] - 0.020, inv["b"]), rad=0.16)
    power(ax, (inv["r"], SPINE), (meter["l"], SPINE))
    power(ax, (meter["r"], SPINE), (cont["l"], SPINE))
    power(ax, (cont["r"], SPINE), (loads["l"], SPINE))

    # ---- signal ---------------------------------------------------------- #
    signal(ax, (meter["cx"], meter["b"]), (mcu["cx"], OUTER_T),
           "RS485 Modbus, 1 Hz", dx=-0.052, dy=0.030)
    signal(ax, (0.660, OUTER_T), (cont["cx"], cont["b"]),
           "open or close", dx=0.056, dy=0.020)
    signal(ax, (OUTER_R, 0.470), (dash["l"], 0.470), "WiFi", both=True, dy=0.012)
    signal(ax, (wx["l"], 0.250), (OUTER_R, 0.270), "sky forecast", dy=0.010)

    # ---- legend and caption ---------------------------------------------- #
    ax.add_patch(FancyArrowPatch((0.030, 0.095), (0.085, 0.095),
                                 arrowstyle="-|>", mutation_scale=15,
                                 linewidth=2.5, color=INK, zorder=5))
    ax.text(0.094, 0.095, "power", va="center", fontsize=8.2, color=INK,
            fontweight="bold", zorder=5)
    ax.add_patch(FancyArrowPatch((0.152, 0.095), (0.207, 0.095),
                                 arrowstyle="-|>", mutation_scale=11,
                                 linewidth=1.3, color=MID, zorder=5,
                                 linestyle=(0, (5, 3))))
    ax.text(0.216, 0.095, "signal", va="center", fontsize=8.2, color=MID,
            style="italic", zorder=5)

    ax.text(0.5, 0.032,
            "One meter measures the whole supply. The UNO Q separates it into "
            "individual machines, decides, and switches. No cloud service is "
            "required for any control decision.",
            ha="center", va="center", fontsize=8.0, color=FAINT, zorder=5)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "system_block_diagram.png"
    fig.savefig(path, dpi=210, bbox_inches="tight", pad_inches=0.14,
                facecolor=WHITE)
    plt.close(fig)
    return path


def build_wiring() -> Path:
    """
    The circuit detail the block diagram deliberately leaves out.

    Its job is to make the isolation barrier obvious. The meter terminals sit at
    mains potential and the UNO Q is a Linux computer with a USB port somebody
    will eventually plug a laptop into, so the galvanic barrier between them is
    the single most important thing on this drawing.
    """
    fig, ax = plt.subplots(figsize=(13.2, 5.6), dpi=210)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    fig.patch.set_facecolor(WHITE)

    # Isolation barrier, drawn first so everything else sits on top of it.
    ax.plot([0.395, 0.395], [0.165, 0.94], linestyle=(0, (7, 5)),
            linewidth=1.6, color=MID, zorder=1)
    ax.text(0.395, 0.965, "GALVANIC ISOLATION BARRIER", ha="center", va="center",
            fontsize=8.4, fontweight="bold", color=MID, zorder=5,
            bbox=dict(boxstyle="round,pad=0.34", fc=WHITE, ec=MID, lw=1.0))
    ax.text(0.200, 0.048, "mains referenced", ha="center", va="center",
            fontsize=8.0, color=FAINT, style="italic")
    ax.text(0.700, 0.048, "logic side, safe to touch", ha="center", va="center",
            fontsize=8.0, color=FAINT, style="italic")

    meter = box(ax, 0.140, 0.660, 0.210, "SELEC EM2M METER",
                ["terminal A, data positive", "terminal B, data return",
                 "terminal G, signal ground"], fill=BAND)
    conv = box(ax, 0.545, 0.660, 0.215, "ISOLATED RS485 CONVERTER",
               ["A, B, G on the meter side", "RX, TX, DE, 3V3, GND",
                "on the board side"], fill=BAND)
    unoq = box(ax, 0.870, 0.660, 0.200, "ARDUINO UNO Q",
               ["D0 is RX, pin PB7", "D1 is TX, pin PB6",
                "D2 drives DE and RE", "3V3 and GND to converter"])

    relay = box(ax, 0.870, 0.330, 0.200, "RELAY OR CONTACTOR",
                ["IN driven from D7, opto isolated", "contacts in series with",
                 "the machine supply"], fill=BAND)

    ax.add_patch(FancyArrowPatch(
        (meter["r"], meter["cy"]), (conv["l"], conv["cy"]), arrowstyle="<|-|>",
        mutation_scale=13, linewidth=1.9, color=INK, zorder=2))
    ax.text(0.345, 0.700, "screened 3 core cable", ha="center", va="bottom",
            fontsize=7.4, color=INK, fontweight="bold", zorder=5,
            bbox=dict(boxstyle="round,pad=0.20", fc=WHITE, ec="none"))
    ax.text(0.345, 0.600, "screen earthed at one end only", ha="center",
            va="top", fontsize=7.0, color=MID, style="italic", zorder=5,
            bbox=dict(boxstyle="round,pad=0.20", fc=WHITE, ec="none"))

    ax.add_patch(FancyArrowPatch(
        (conv["r"], conv["cy"]), (unoq["l"], unoq["cy"]), arrowstyle="<|-|>",
        mutation_scale=13, linewidth=1.5, color=MID, zorder=2,
        linestyle=(0, (5, 3))))
    ax.text(0.740, 0.700, "UART", ha="center", va="bottom", fontsize=7.4,
            color=MID, style="italic", zorder=5,
            bbox=dict(boxstyle="round,pad=0.20", fc=WHITE, ec="none"))

    signal(ax, (unoq["cx"], unoq["b"]), (relay["cx"], relay["t"]),
           "D7", dx=0.026, dy=0.030)

    ax.text(0.5, 0.118,
            "The UART on the header is usart1 in the Zephyr device tree, which maps "
            "D0 to PB7 and D1 to PB6. The sketch selects it with one definition.",
            ha="center", va="center", fontsize=7.8, color=FAINT, zorder=5)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "wiring_diagram.png"
    fig.savefig(path, dpi=210, bbox_inches="tight", pad_inches=0.14,
                facecolor=WHITE)
    plt.close(fig)
    return path


if __name__ == "__main__":
    for fn in (build, build_wiring):
        p = fn()
        print(f"wrote {p} ({p.stat().st_size / 1024:.0f} KB)")
    sys.exit(0)
