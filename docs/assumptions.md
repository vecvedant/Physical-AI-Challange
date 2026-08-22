# Assumptions, and what must be replaced before quoting a number

Every figure this system reports rests on values that are site-specific. They
live in `udyogiq/config.py` and are overridden in `config.local.json`. The
defaults are representative, not authoritative.

## Must be corrected for the actual installation

| Value | Default | Why it matters |
|---|---|---|
| Site latitude / longitude | 18.52, 73.86 (Pune) | Solar geometry. Wrong location, wrong generation forecast. |
| Array peak power, tilt, azimuth | 3 kW, 20°, due south | Plane-of-array irradiance. |
| System efficiency | 0.80 | Inverter, wiring and mismatch losses. |
| Battery capacity and rate limits | 5 kWh, 1.5 kW | Dispatch feasibility. |
| Battery installed cost, cycle life | ₹15,000/kWh, 4,000 cycles | **Sets the degradation cost, which decides whether arbitrage is ever worth doing at all.** |
| Tariff base rate and ToD windows | ₹8/kWh, representative | Every rupee figure. |
| Demand charge, sanctioned load | ₹350/kVA, 5 kVA | Peak-shaving value, usually the largest single saving. |
| Export rate | ₹3/kWh | Whether storing surplus beats exporting it. |

## Grid emission factor

0.71 kg CO₂/kWh, the approximate weighted-average operating margin for the
Indian grid from the CEA CO₂ baseline database. This is a national annual
average; the real figure moves by state, season and hour. Every CO₂ number
derived from it is an estimate good to roughly one significant figure and is
presented that way.

## Time-of-day tariffs

The Electricity (Rights of Consumers) Amendment Rules made ToD tariffs
mandatory for commercial and industrial consumers, with a discount in the
designated solar window and a premium at peak. The specific windows and
multipliers are set by each state commission. The defaults here are
representative and **must** be replaced with the consumer's actual tariff order
before any saving is quoted to them.

## Modelling conventions

- Solar and battery output are counted as zero-carbon **at the point of use**,
  the standard operational convention. Embodied emissions in panels and cells
  are out of scope; including them would require a life-cycle assessment this
  project has no basis to perform.
- Demand is billed on the **average over the billing window**, not on an
  instantaneous peak. The real and counterfactual worlds are averaged
  identically before being compared.
- The counterfactual baseline is the same measured load and the same solar,
  with no battery movement and no idle cutoff. It is what the site would have
  done without the device, which is what makes the difference attributable.

## What the numbers in this repository actually are

Every measured result quoted in the commit history and the report comes from
the **simulator**, evaluated against known ground truth, and is labelled as
such. Meter frames carry a `source` field end to end so simulated data cannot
be presented as measured.

Results on real hardware will differ, and the honest expectation is that they
will be somewhat worse. The simulator has no harmonic distortion, no metering
error, no unmodelled loads and no contact bounce. What it does reproduce is the
*structure* the algorithms depend on — discrete switching steps with distinct
real and reactive signatures, motor inrush, jittered duty cycling and supply
sag under load — which is why a model that works here has learned something
transferable rather than memorised a waveform.
