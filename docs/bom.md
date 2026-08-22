# Bill of Materials

| # | Component | Qty | Notes |
|---|---|---|---|
| 1 | **Arduino UNO Q, 4 GB / 32 GB** | 1 | Qualcomm Dragonwing QRB2210 + STM32U585. The 4 GB variant is used; 2 GB would run the models but leaves little room for the historian and the dashboard together. |
| 2 | **Selec EM2M-1P-C-100A** single-phase energy meter | 1 | Class 1, direct connected to 100 A with no external CT, RS485 Modbus RTU, DIN rail. |
| 3 | **Isolated RS485 to UART converter** | 1 | Galvanic isolation is not optional here. See `wiring.md`. |
| 4 | Relay / contactor module | 1 | Sized for the controlled circuit, opto-isolated input. |
| 5 | 5 V power supply, 3 A or better, USB-C | 1 | The UNO Q draws well past a phone charger's rating under load. |
| 6 | DIN rail, enclosure, ferrules, screened 3-core cable | as needed | Screened cable for the RS485 run. |
| 7 | 120 ohm termination resistors | 2 | Only needed on runs beyond a few metres. |

## A note on the part number

The challenge report template lists the UNO Q as **ABX00087**. Arduino's own
store lists **ABX00162** for the 2 GB board. Check the invoice and quote the
number that matches the purchase proof, because the two have to agree.

## Deliberately not included

A split-core CT sampled at kilohertz on the STM32's ADC would enable genuine
motor current signature analysis, and it costs very little. It was left out on
purpose: the project claims trend analysis at 1 Hz and demonstrates exactly
that. Adding hardware that no reported result depends on would be padding, and
the limitation is more useful stated honestly than papered over.
