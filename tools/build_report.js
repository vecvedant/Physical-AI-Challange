const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat, PageBreak, convertInchesToTwip,
} = require("docx");

/* ------------------------------------------------------------------ */
/* Style helpers                                                       */
/* ------------------------------------------------------------------ */
const INK = "1A1A1A";
const MUTED = "5A6270";
const ACCENT = "0B5394";
const RULE = "C9CFD8";
const PANEL = "F2F5F9";
const TABLE_W = 9360; // 6.5" content width in DXA

const P = (text, opts = {}) =>
  new Paragraph({
    spacing: { after: opts.after ?? 120, line: opts.line ?? 264 },
    alignment: opts.align,
    indent: opts.indent,
    children: [new TextRun({
      text, size: opts.size ?? 20, color: opts.color ?? INK,
      bold: opts.bold, italics: opts.italics, font: "Calibri",
    })],
  });

/** Paragraph with mixed runs: [["text",{bold:true}], ...] */
const PR = (runs, opts = {}) =>
  new Paragraph({
    spacing: { after: opts.after ?? 120, line: 264 },
    alignment: opts.align,
    children: runs.map(([t, o = {}]) => new TextRun({
      text: t, size: o.size ?? 20, color: o.color ?? INK,
      bold: o.bold, italics: o.italics, font: o.font ?? "Calibri",
    })),
  });

const H1 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_1,
  spacing: { before: 340, after: 160 },
  border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: RULE, space: 6 } },
  children: [new TextRun({ text, size: 28, bold: true, color: ACCENT, font: "Calibri" })],
});

const H2 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_2,
  spacing: { before: 240, after: 100 },
  children: [new TextRun({ text, size: 23, bold: true, color: INK, font: "Calibri" })],
});

const BULLET = (text, opts = {}) => new Paragraph({
  numbering: { reference: "bullets", level: opts.level ?? 0 },
  spacing: { after: 70, line: 264 },
  children: [new TextRun({ text, size: 20, color: INK, font: "Calibri" })],
});

const BULLET_R = (runs, opts = {}) => new Paragraph({
  numbering: { reference: "bullets", level: opts.level ?? 0 },
  spacing: { after: 70, line: 264 },
  children: runs.map(([t, o = {}]) => new TextRun({
    text: t, size: 20, color: o.color ?? INK, bold: o.bold,
    italics: o.italics, font: o.font ?? "Calibri",
  })),
});

const MONO = (text) => new Paragraph({
  spacing: { after: 40, line: 240 },
  indent: { left: 260 },
  children: [new TextRun({ text, size: 17, font: "Consolas", color: "24324A" })],
});

/** Callout panel for the honesty / limitations boxes. */
const CALLOUT = (title, lines) => new Table({
  width: { size: TABLE_W, type: WidthType.DXA },
  columnWidths: [TABLE_W],
  borders: {
    top: { style: BorderStyle.SINGLE, size: 2, color: RULE },
    bottom: { style: BorderStyle.SINGLE, size: 2, color: RULE },
    left: { style: BorderStyle.SINGLE, size: 18, color: ACCENT },
    right: { style: BorderStyle.SINGLE, size: 2, color: RULE },
    insideHorizontal: { style: BorderStyle.NONE },
    insideVertical: { style: BorderStyle.NONE },
  },
  rows: [new TableRow({
    children: [new TableCell({
      width: { size: TABLE_W, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: PANEL },
      margins: { top: 130, bottom: 130, left: 190, right: 190 },
      children: [
        PR([[title, { bold: true, size: 20, color: ACCENT }]], { after: 70 }),
        ...lines.map((l) => P(l, { size: 19, after: 60 })),
      ],
    })],
  })],
});

/** Simple table. cols = array of DXA widths. */
const TBL = (header, rows, cols) => new Table({
  width: { size: TABLE_W, type: WidthType.DXA },
  columnWidths: cols,
  borders: {
    top: { style: BorderStyle.SINGLE, size: 4, color: RULE },
    bottom: { style: BorderStyle.SINGLE, size: 4, color: RULE },
    left: { style: BorderStyle.NONE }, right: { style: BorderStyle.NONE },
    insideHorizontal: { style: BorderStyle.SINGLE, size: 2, color: RULE },
    insideVertical: { style: BorderStyle.NONE },
  },
  rows: [
    new TableRow({
      tableHeader: true,
      children: header.map((h, i) => new TableCell({
        width: { size: cols[i], type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: PANEL },
        margins: { top: 90, bottom: 90, left: 130, right: 130 },
        children: [PR([[h, { bold: true, size: 19, color: ACCENT }]], { after: 0 })],
      })),
    }),
    ...rows.map((r) => new TableRow({
      children: r.map((c, i) => new TableCell({
        width: { size: cols[i], type: WidthType.DXA },
        margins: { top: 80, bottom: 80, left: 130, right: 130 },
        children: (Array.isArray(c) ? c : [c]).map((line, idx) =>
          PR([[String(line), { size: 19, color: idx > 0 ? MUTED : INK }]], { after: 0 })),
      })),
    })),
  ],
});

/** Fill-in field the entrant must complete. */
const FIELD = (label, value, hint) => new TableRow({
  children: [
    new TableCell({
      width: { size: 2900, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: PANEL },
      margins: { top: 90, bottom: 90, left: 130, right: 130 },
      children: [PR([[label, { bold: true, size: 19 }]], { after: 0 })],
    }),
    new TableCell({
      width: { size: 6460, type: WidthType.DXA },
      margins: { top: 90, bottom: 90, left: 130, right: 130 },
      children: [PR(
        value
          ? [[value, { size: 19 }]]
          : [[hint || "— to be completed —", { size: 19, color: "B03A2E", italics: true }]],
        { after: 0 })],
    }),
  ],
});

const FIELDS = (rows) => new Table({
  width: { size: TABLE_W, type: WidthType.DXA },
  columnWidths: [2900, 6460],
  borders: {
    top: { style: BorderStyle.SINGLE, size: 4, color: RULE },
    bottom: { style: BorderStyle.SINGLE, size: 4, color: RULE },
    left: { style: BorderStyle.NONE }, right: { style: BorderStyle.NONE },
    insideHorizontal: { style: BorderStyle.SINGLE, size: 2, color: RULE },
    insideVertical: { style: BorderStyle.NONE },
  },
  rows,
});

const PLACEHOLDER = (text, height) => new Table({
  width: { size: TABLE_W, type: WidthType.DXA },
  columnWidths: [TABLE_W],
  borders: {
    top: { style: BorderStyle.DASHED, size: 6, color: "9AA5B4" },
    bottom: { style: BorderStyle.DASHED, size: 6, color: "9AA5B4" },
    left: { style: BorderStyle.DASHED, size: 6, color: "9AA5B4" },
    right: { style: BorderStyle.DASHED, size: 6, color: "9AA5B4" },
  },
  rows: [new TableRow({
    height: { value: height || 1900, rule: "atLeast" },
    children: [new TableCell({
      width: { size: TABLE_W, type: WidthType.DXA },
      verticalAlign: "center",
      margins: { top: 200, bottom: 200, left: 200, right: 200 },
      children: [PR([[text, { size: 19, color: MUTED, italics: true }]],
        { align: AlignmentType.CENTER, after: 0 })],
    })],
  })],
});

const SPACER = (h) => new Paragraph({ spacing: { after: h || 160 }, children: [] });

/* ================================================================== */
/* Document                                                            */
/* ================================================================== */
const doc = new Document({
  creator: "Vedant Charegaonkar",
  title: "Udyog IQ — Arduino Physical AI Challenge India 2026",
  description: "Edge AI energy intelligence for small industry on the Arduino UNO Q",
  numbering: {
    config: [{
      reference: "bullets",
      levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 340, hanging: 200 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "◦", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 680, hanging: 200 } } } },
      ],
    }, {
      reference: "steps",
      levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 380, hanging: 240 } } } },
      ],
    }],
  },
  styles: {
    default: { document: { run: { font: "Calibri", size: 20, color: INK } } },
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: {
          top: convertInchesToTwip(0.85), bottom: convertInchesToTwip(0.85),
          left: convertInchesToTwip(0.95), right: convertInchesToTwip(0.95),
        },
      },
    },
    children: [
      /* ---------------- Title ---------------- */
      PR([["ARDUINO PHYSICAL AI CHALLENGE INDIA 2026", { bold: true, size: 19, color: MUTED }]],
        { after: 60 }),
      PR([["Project Report", { size: 19, color: MUTED }]], { after: 240 }),

      new Paragraph({
        spacing: { after: 60 },
        children: [new TextRun({ text: "UDYOG IQ", size: 52, bold: true, color: ACCENT, font: "Calibri" })],
      }),
      PR([["Edge AI energy intelligence for small industry — one meter, one board, no cloud",
        { size: 24, color: INK }]], { after: 300 }),

      FIELDS([
        FIELD("Project Title", "Udyog IQ — Edge AI Energy Intelligence for Small Industry"),
        FIELD("Team Name", null, "— enter your registered team name —"),
        FIELD("Registration / Team ID", null, "— enter your portal team ID —"),
        FIELD("Contest Track", "Industrial & Sustainability AI"),
        FIELD("Institution & City", null, "— enter your institution and city —"),
      ]),

      SPACER(220),
      H2("Team Members"),
      TBL(
        ["Role", "Name", "Email"],
        [
          ["Team Leader", "Vedant Charegaonkar", "vec.vedant@gmail.com"],
          ["Member 2 (optional)", "—", "—"],
          ["Member 3 (optional)", "—", "—"],
          ["Member 4 (optional)", "—", "—"],
        ],
        [2200, 3580, 3580]),

      SPACER(200),
      CALLOUT("In one sentence", [
        "A small factory has a dozen machines, one electricity connection, and a bill nobody understands. " +
        "Udyog IQ puts a single energy meter on the incoming supply and an Arduino UNO Q beside it, and works out " +
        "by itself — with no labelled training data — which machines are running, which one is starting to fail, " +
        "how much money is being burned on idle, and when to use solar, battery or grid so the plant pays less.",
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 1. Overview ---------------- */
      H1("1. Project Overview"),

      H2("Problem Statement"),
      P("India has millions of MSME workshops — machine shops, textile units, cold stores, small " +
        "fabricators. Each runs a handful of motors on a single-phase or small three-phase connection, " +
        "and each receives an electricity bill it has no way to explain. Which machine consumed what is " +
        "unknown, because metering every machine costs more than the electricity it would save. So nothing " +
        "is measured, nothing is optimised, and two large costs go unmanaged."),
      BULLET_R([["Maximum demand charges. ", { bold: true }],
        ["Utilities bill the highest average demand in any 15- or 30-minute window of the month, priced per kVA. " +
         "One careless quarter-hour where several motors start together sets a charge that is then paid every month."]]),
      BULLET_R([["Unplanned breakdowns. ", { bold: true }],
        ["A compressor or pump fails without warning, stopping production. The electrical signature had been " +
         "drifting for weeks, but nobody was watching it."]]),
      P("Both problems are invisible without per-machine visibility, and per-machine metering is exactly " +
        "what these businesses cannot justify. That is the gap this project closes."),

      H2("How Your Project Works"),
      P("A single Selec EM2M energy meter is clamped on the incoming supply and read over RS485 Modbus by " +
        "the Arduino UNO Q. The STM32 microcontroller masters the bus at 1 Hz and drives a contactor behind a " +
        "hardware interlock. The Qualcomm processor, running Debian, does everything else on-device."),
      P("Every time a machine switches on or off it leaves a step in real and reactive power. The node clusters " +
        "those step signatures and recovers the individual machines from the aggregate — non-intrusive load " +
        "monitoring, so one meter serves the whole workshop. Each machine's start events are then scored against " +
        "a model of its own learned normal, so degradation is flagged before failure. In parallel the node " +
        "forecasts plant load from its own history and solar generation from a weather feed, and solves a " +
        "24-hour battery schedule against time-of-day tariffs every fifteen minutes. Idle machines are cut, " +
        "demand peaks are shaved before they are set, and a shadow ledger records what the same day would " +
        "have cost without the device."),
      P("Everything runs on the board. A mobile dashboard is served from it over Wi-Fi. No cloud account, " +
        "no subscription, and no production data leaving the site."),

      H2("Why Arduino UNO Q?"),
      P("This project needs two different kinds of computer at once, and the UNO Q is one board that is both."),
      TBL(
        ["Brain", "What it runs", "Why it must be this one"],
        [
          [["STM32U585", "Cortex-M33"],
            "Modbus RTU master; contactor interlock; watchdog",
            "RS485 has a turnaround deadline — the driver must drop within a character time of the last stop bit or it holds the line and the reply collides with our own echo. Linux meets that deadline almost always, and “almost” means a corrupt frame every few minutes that looks exactly like a wiring fault."],
          [["Qualcomm QRB2210", "quad Cortex-A53, Debian"],
            "NILM, anomaly detection, forecasting, dispatch optimiser, historian, dashboard",
            "scikit-learn, a SQLite historian, a 96-step optimisation re-solved every 15 minutes, and a web server. Not microcontroller work."],
        ],
        [1900, 2700, 4760]),
      SPACER(140),
      P("The division also matters for safety. Minimum contactor dwell times and the switching-rate cap are " +
        "enforced on the microcontroller, so they hold even when the Linux side hangs, fills its disk, or is " +
        "being updated. The Python policy engine checks the same rules first, but only so it does not issue " +
        "requests that would be refused — it is not the safety mechanism."),
      SPACER(60),
      CALLOUT("The honest version of this claim", [
        "An ESP32 could read the meter but could not run the learning half. A Raspberry Pi could run the " +
        "learning half but could not promise the real-time half. Using both would mean two boards, two power " +
        "supplies and a link between them. One UNO Q replaces a PLC, a protocol gateway and an edge PC — and " +
        "that is the specific reason this project exists on this hardware rather than being ported to it.",
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 2. BOM ---------------- */
      H1("2. Components Used (BOM)"),
      TBL(
        ["Component", "Qty", "Notes"],
        [
          ["Arduino UNO Q (4 GB / 32 GB)", "1", "Qualcomm Dragonwing QRB2210 + STM32U585. Purchase proof uploaded separately."],
          ["Selec EM2M-1P-C-100A energy meter", "1", "Class 1, single phase, direct connected to 100 A with no external CT. RS485 Modbus RTU."],
          ["Isolated RS485 ↔ UART converter", "1", "Galvanic isolation between the mains-referenced meter and the board."],
          ["Relay / contactor module", "1", "Opto-isolated input, sized for the controlled circuit."],
          ["5 V USB-C power supply, ≥ 3 A", "1", "The UNO Q draws well past a phone charger's rating under load."],
          ["Screened 3-core cable, ferrules, DIN rail, enclosure", "—", "Screen earthed at one end only."],
          ["120 Ω termination resistors", "2", "Only for RS485 runs beyond a few metres."],
        ],
        [3500, 700, 5160]),
      SPACER(140),
      CALLOUT("Part number check", [
        "This report template lists the UNO Q as ABX00087; Arduino's store lists ABX00162 for the 2 GB board. " +
        "Confirm against the invoice so the BOM and the purchase proof agree.",
      ]),
      SPACER(120),
      P("A split-core CT sampled at kilohertz on the STM32's ADC was considered and deliberately left out. " +
        "It would have enabled genuine motor current signature analysis, but no result in this report depends " +
        "on it, and the limitation is more useful stated plainly than hidden behind hardware that was added " +
        "to make a claim sound better.", { color: MUTED }),

      /* ---------------- 3. Architecture ---------------- */
      H1("3. System Architecture & Circuit"),

      H2("Step-by-Step Workflow"),
      ...[
        "Acquire — the STM32 polls the Selec meter over RS485 at 1 Hz and caches the decoded block. Python pulls that snapshot across the Arduino Bridge and rejects it if stale, because a repeated stale frame is indistinguishable from a genuinely steady load.",
        "Compensate — with the meter at the grid tie, measured solar and battery power are added back to recover the load-side signal. Generation moves independently of the machines, and a cloud crossing otherwise looks exactly like a motor starting.",
        "Detect — an adaptive-threshold change-point detector turns the power trace into discrete switching events, each carrying a (ΔP, ΔQ) signature.",
        "Disaggregate — those signatures are clustered online into individual machines. One meter, many machines, no labels.",
        "Diagnose — each machine's start events are scored against a density model of its own learned normal; drift in power factor, draw and inrush is tracked separately.",
        "Forecast — plant load from the site's own history; solar from an Open-Meteo feed through a clear-sky physics model with a learned site correction.",
        "Decide — dynamic programming over discretised battery state of charge, re-solved every 15 minutes in a receding-horizon MPC loop.",
        "Act — idle cutoff and demand shedding through the hardware interlock, in advisory mode until an operator deliberately enables actuation.",
        "Account — a shadow ledger runs the same day with no battery movement and no idle cutoff, so the reported saving is a measured difference rather than a claim.",
      ].map((t) => new Paragraph({
        numbering: { reference: "steps", level: 0 },
        spacing: { after: 90, line: 264 },
        children: [new TextRun({ text: t, size: 20, font: "Calibri" })],
      })),

      SPACER(160),
      H2("Block Diagram"),
      MONO("   Selec EM2M ──RS485──►┌──────────────────────────────────┐"),
      MONO("   100 A, 1-phase       │  STM32U585   (real time)         │"),
      MONO("   Modbus RTU           │   • Modbus master, 1 Hz          │"),
      MONO("                        │   • contactor interlock          │"),
      MONO("                        │   • watchdog, fail-safe closed   │"),
      MONO("                        └───────────┬──────────────────────┘"),
      MONO("                                    │ Bridge RPC"),
      MONO("                        ┌───────────┴──────────────────────┐"),
      MONO("   Open-Meteo ─────────►│  QRB2210 / Debian                │"),
      MONO("   (cached, optional)   │   pipeline → NILM → health       │"),
      MONO("                        │   forecasts → DP dispatch + MPC  │"),
      MONO("                        │   tariff, CO₂, savings ledger    │"),
      MONO("                        │   SQLite historian · FastAPI     │"),
      MONO("                        └───────────┬──────────────────────┘"),
      MONO("                                    │ LAN only"),
      MONO("                      contactor ◄───┴───► mobile PWA dashboard"),
      SPACER(180),

      H2("Circuit / Wiring"),
      TBL(
        ["Signal", "UNO Q pin", "STM32 pin", "Note"],
        [
          ["RS485 TX", "D1", "PB6", "usart1 TX per the Zephyr device tree"],
          ["RS485 RX", "D0", "PB7", "usart1 RX"],
          ["RS485 DE/RE", "D2", "PB3", "DE and RE tied together; HIGH transmits"],
          ["Contactor", "D7", "PB2", "Most relay boards are active-low"],
          ["3V3 / GND", "—", "—", "Powers the isolated converter's logic side"],
        ],
        [1900, 1500, 1500, 4460]),
      SPACER(140),
      CALLOUT("One unresolved hardware question, handled in software", [
        "The Zephyr device tree maps the UNO Q's D0/D1 header pins to usart1 and aliases it arduino_serial, " +
        "and puts the MPU↔MCU router on lpuart1 (PG5–PG8, hardware flow control, not brought out to the " +
        "headers). At least one published tutorial states that Serial1 is reserved for the router, which " +
        "contradicts the device tree.",
        "This is not resolvable from documentation alone, so it is a single #define in the sketch, and the " +
        "meter transport is a config key with three backends — MCU-mastered, Linux-mastered over a USB-RS485 " +
        "adapter, or simulated. Whichever way the bring-up lands, it is a one-line change rather than a rewrite.",
      ]),
      SPACER(120),
      PLACEHOLDER("[ Insert photograph of the assembled hardware — UNO Q, meter, isolated RS485 converter and contactor on DIN rail ]", 2100),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 4. AI / ML ---------------- */
      H1("4. AI / ML Model Details"),
      P("Five models run on the board. The organising constraint is that there is no labelled data and there " +
        "never will be — nobody is going to instrument a workshop to record which machine produced which step, " +
        "and a model trained on another factory would not transfer, because the whole point is that these are " +
        "this site's machines. Every model below is therefore unsupervised or self-supervised."),
      SPACER(80),
      TBL(
        ["Purpose", "Model", "Trained on", "Result"],
        [
          ["Machine discovery (NILM)", "Online leader clustering of (ΔP, ΔQ) switching signatures", "Unlabelled — machines are discovered, not classified", "5 of 7 machines recovered, step size within 3.5%"],
          ["Predictive maintenance", "Linear autoencoder (PCA reconstruction) + Isolation Forest, per machine", "Each machine's own first ~80 start events", "Healthy 77–82/100; alert at 15% degradation"],
          ["Load forecasting", "HistGradientBoostingRegressor, direct multi-horizon", "Self-supervised on site history", "15-min MAE 213 W vs 506 W persistence"],
          ["Solar forecasting", "Clear-sky physics + learned gradient-boosted residual", "Weather feed vs measured generation", "Solar elevation within 0.5° of true"],
          ["Battery dispatch", "Dynamic programming over discretised SoC + MPC", "Optimisation, not learning", "96-block plan solved in 4.6 ms"],
        ],
        [2000, 2700, 2200, 2460]),

      SPACER(180),
      H2("Training Platform"),
      P("scikit-learn, running on the UNO Q itself. Models are trained and retrained on-device from the " +
        "SQLite historian — nothing is trained in a cloud notebook and shipped down."),
      P("XGBoost was deliberately not used, despite being the obvious choice for the forecasting work. " +
        "HistGradientBoostingRegressor is the same algorithm family, ships inside scikit-learn, and installs " +
        "on the board's aarch64 Debian without a compiler. XGBoost would have meant building from source on a " +
        "2 GHz Cortex-A53, and a dependency that will not install on the target is not a dependency, it is a " +
        "bug. PyTorch and TensorFlow were excluded for the same reason, which is why the anomaly detector is " +
        "a linear autoencoder rather than a neural one."),

      H2("Dataset"),
      P("There is no downloaded dataset. Each model manufactures its own supervision:"),
      BULLET("NILM and health learn from the site's own switching events — roughly 600,000 samples over a week of operation."),
      BULLET("The load forecaster's target is simply the plant's own demand a few blocks later, which the historian already holds."),
      BULLET("The solar model's target is measured generation against the weather that was forecast for that hour."),
      P("For development and evaluation, a physically-grounded simulator of a single-phase workshop provides " +
        "known ground truth — seven machines with distinct real and reactive signatures, motor inrush, jittered " +
        "duty cycling, supply sag under load, and injectable degradation. Two loads are deliberately placed " +
        "close in real power (1500 W and 1350 W) so that reactive power is the only thing separating them, " +
        "because that is the case NILM has to get right."),

      H2("Brief Description & Limitations"),
      P("How the pieces fit: NILM must come before diagnosis. This was not obvious and cost a rewrite. The " +
        "first health model scored the plant's aggregate feature windows and appeared to work — until it was " +
        "measured on held-out data, where it scored healthy operation at 0.767 mean anomaly against 0.498 for " +
        "genuinely degraded operation. Inverted. An aggregate window changes far more when a different mix of " +
        "machines happens to be running than when one machine degrades, so the model had learned the shift " +
        "roster: a quiet afternoon looked more alarming than a failing compressor. Health is now scored per " +
        "machine on events NILM has already attributed, each of which belongs to exactly one machine."),
      SPACER(80),
      CALLOUT("Where this degrades or fails — stated plainly", [
        "This is not motor current signature analysis. MCSA resolves sidebands around the supply frequency to " +
        "identify broken rotor bars and bearing defects, and needs current sampled in the kilohertz. At 1 Hz " +
        "those sidebands do not exist in our data at any resolution. What is claimed here is trend and anomaly " +
        "detection on aggregate electrical parameters — real, useful, and a different technique.",
        "NILM cannot see a small load hiding under a large running one. The detection threshold scales with " +
        "local noise, so while a 2 kW motor runs, a 340 W fan switching is below the floor. In testing, the fan " +
        "and the lighting circuit were never recovered for exactly this reason. This is a structural property " +
        "of single-point disaggregation, not a tuning failure.",
        "Machines that switch rarely take proportionally longer to discover. Across seeds, four simulated days " +
        "recovered four of five target machines; five days recovered all of them. A machine used twice a day " +
        "takes correspondingly longer, and that is the honest cost of having no labels.",
        "Two machines that always switch together will be reported as one, and simultaneous switching produces " +
        "composite clusters. The dashboard shows these as unnamed candidates for the operator to confirm or " +
        "ignore, rather than asserting they are machines.",
        "Battery dispatch is advisory unless the inverter accepts external commands. Where it does not, the " +
        "node still reports the saving it would have captured, and still performs load-side actuation.",
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 5. Code ---------------- */
      H1("5. Code Structure"),
      P("The repository is an Arduino App Lab project (app.yaml, python/, sketch/) wrapping an importable " +
        "Python package, so the models can be developed and tested off-board."),
      SPACER(60),
      TBL(
        ["Module", "Responsibility"],
        [
          ["sketch/sketch.ino", "STM32 firmware. Modbus RTU master with CRC and DE turnaround, contactor interlock (minimum dwell, switching-rate cap, fail-safe closed), Bridge RPC surface."],
          ["udyogiq/transport/", "Three interchangeable meter backends — bridge, serial, sim — plus inverter adapters for measured or estimated source telemetry."],
          ["udyogiq/meter/", "Selec EM2M register map, float decoding, and a physical sanity check on every frame."],
          ["udyogiq/pipeline/", "Ring buffer, windowed feature extraction, adaptive-threshold change-point detection."],
          ["udyogiq/ml/", "nilm · health · forecast · solar · battery."],
          ["udyogiq/policy/", "Dynamic-programming dispatch optimiser, MPC loop, and the decision engine that issues actions with reasons."],
          ["udyogiq/sustain/", "Time-of-day tariff engine, weather client, CO₂ and counterfactual savings accounting."],
          ["udyogiq/store/", "SQLite historian with batched writes, one-minute rollup and retention."],
          ["udyogiq/api/", "FastAPI + WebSocket server."],
          ["udyogiq/runtime.py", "Orchestrator: one acquisition thread, everything else on a cooperative timer."],
          ["web/", "Mobile-first PWA dashboard."],
          ["sim/", "Synthetic workshop with solar, battery and injectable faults."],
          ["tools/probe_meter.py", "Hardware bring-up: verifies the register map against a real meter."],
          ["tests/", "27 regression tests, each corresponding to a bug found by measurement."],
        ],
        [2600, 6760]),

      SPACER(160),
      H2("Key functions"),
      BULLET_R([["EdgeDetector.push() ", { font: "Consolas", bold: true }],
        ["— three-state machine turning the power trace into confirmed switching events; transients that return to origin emit nothing."]]),
      BULLET_R([["NILMEngine.push() ", { font: "Consolas", bold: true }],
        ["— attributes an event to a discovered machine or founds a new cluster."]]),
      BULLET_R([["ApplianceHealth.push_edge() ", { font: "Consolas", bold: true }],
        ["— scores one start event against that machine's learned normal."]]),
      BULLET_R([["DispatchOptimiser.solve() ", { font: "Consolas", bold: true }],
        ["— backward induction over 96 stages × 41 SoC levels × 21 actions."]]),
      BULLET_R([["PolicyEngine.evaluate_idle() ", { font: "Consolas", bold: true }],
        ["— finds machines running at standby draw and cuts them, subject to the interlock."]]),
      BULLET_R([["UdyogIQ.warmup() ", { font: "Consolas", bold: true }],
        ["— replays simulated history at CPU speed so the node comes up already knowing the plant."]]),

      SPACER(200),
      FIELDS([
        FIELD("GitHub Repository", "https://github.com/vecvedant/Physical-AI-Challange"),
        FIELD("Demo Video Link", null, "— paste the public YouTube / Drive link —"),
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 6. Testing ---------------- */
      H1("6. Testing & Results"),
      P("Testing was done against a simulator with known ground truth, because it is the only way to ask " +
        "whether the system found the right machines rather than merely plausible ones. Every figure below is " +
        "from that simulator and is labelled as such; meter frames carry a source field end to end so simulated " +
        "data can never be presented as measured."),

      SPACER(100),
      H2("Machine discovery — 5 of 7 machines, zero labels"),
      TBL(
        ["Machine (ground truth)", "Discovered", "Error"],
        [
          ["Air compressor — 1500 W / 1047 VAr", "1511 W / 1054 VAr", "+0.7%"],
          ["Coolant pump — 1350 W / 1227 VAr", "1361 W / 1236 VAr", "+0.8%"],
          ["Lathe — 2200 W / 1305 VAr", "2268 W / 1347 VAr", "+3.1%"],
          ["Bench grinder — 750 W / 405 VAr", "760 W / 411 VAr", "+1.3%"],
          ["Office / server — 180 W / 71 VAr", "174 W / 84 VAr", "−3.3%"],
          ["Exhaust fan — 340 W", "not recovered", "below threshold"],
          ["Shop lighting — 420 W", "not recovered", "below threshold"],
        ],
        [3800, 3060, 2500]),
      SPACER(100),
      P("The two misses are structural, not a tuning failure: both switch at shift boundaries when the large " +
        "motors have already raised the adaptive detection threshold above their step size. The unexplained " +
        "residual is reported on the dashboard rather than hidden, because a disaggregation system that quietly " +
        "under-reports is worse than one that admits what it missed.", { color: MUTED }),

      SPACER(160),
      H2("Predictive maintenance — detected at 15% wear, with no fault data"),
      TBL(
        ["Condition", "Compressor health", "Alert", "Other machines"],
        [
          ["Healthy — baseline period", "81 / 100", "no", "76–80"],
          ["Healthy — held out", "82 / 100", "no", "76–80"],
          ["15% degradation", "0 / 100", "ALERT", "76–80"],
          ["30% degradation", "0 / 100", "ALERT", "76–80"],
          ["45% degradation", "0 / 100", "ALERT", "76–80"],
        ],
        [2900, 2400, 1560, 2500]),
      SPACER(100),
      P("The alarm is specific rather than a general drift: every other machine stayed in its healthy band " +
        "throughout. Operator-facing output is a sentence, not a score — “power factor falling 0.0061/day, " +
        "check for a degrading motor or a failing capacitor” is actionable in a way that “anomaly score 0.42” " +
        "is not.", { color: MUTED }),

      SPACER(160),
      H2("Forecasting — walk-forward on 30% held-out history"),
      TBL(
        ["Horizon", "Model MAE", "Persistence baseline", "% of mean load"],
        [
          ["15 minutes", "213 W", "506 W", "12.0%"],
          ["1 hour", "226 W", "615 W", "12.7%"],
          ["24 hours", "277 W", "307 W", "15.6%"],
        ],
        [2400, 2320, 2320, 2320]),
      SPACER(100),
      P("The 24-hour margin is thin because “same time yesterday” is genuinely strong for a scheduled " +
        "workshop. The model earns its place on the short horizons that drive charge decisions, and by " +
        "handling weekends.", { color: MUTED }),

      SPACER(160),
      H2("Dispatch economics — and the most interesting result in the project"),
      TBL(
        ["Scenario", "Saving", "Battery throughput"],
        [
          ["Surplus solar, standard battery", "₹0.00 — declines to cycle", "0.00 kWh"],
          ["Weekday load, standard battery", "₹0.00 — declines to cycle", "0.00 kWh"],
          ["Surplus solar, cheaper battery", "₹10.56", "5.66 kWh"],
          ["Demand spike against 3 kVA limit", "₹7,087.57 — 98% of the bill", "5.06 kWh"],
        ],
        [3800, 3060, 2500]),
      SPACER(100),
      P("At realistic Indian LFP economics — about ₹15,000/kWh installed over 4,000 cycles — a kilowatt-hour " +
        "of battery throughput costs ₹5.56 in consumed cycle life, so arbitrage only pays if the tariff spread " +
        "beats ₹6.44/kWh. On the default schedule the spread is ₹3.20. The optimiser therefore correctly " +
        "declines to cycle, and says so."),
      P("Almost all the money is in demand-charge shaving instead. A device that can tell an owner which of " +
        "those two applies to them is worth considerably more than one that assumes the answer — and an " +
        "optimiser that leaves the battery alone is very easy to mistake for one that is broken."),

      SPACER(160),
      H2("System performance"),
      TBL(
        ["Measure", "Result"],
        [
          ["Dispatch solve time (96 blocks × 41 SoC levels × 21 actions)", "4.6–7.0 ms"],
          ["Acquisition reliability over a continuous run", "100% — 0 errors"],
          ["Warm-start replay: 7 days of plant history", "65 s, 604,800 samples"],
          ["Storage after rollup", "14,400 raw samples → 240 minute aggregates"],
          ["Dashboard update rate over WebSocket", "1 Hz, verified live on mobile viewport"],
          ["Regression tests", "27 passed"],
        ],
        [5600, 3760]),

      SPACER(200),
      H2("Project Images"),
      PLACEHOLDER("[ Photo 1 — the assembled node: UNO Q, Selec meter, isolated RS485 converter, contactor ]", 1700),
      SPACER(100),
      PLACEHOLDER("[ Photo 2 — the mobile dashboard in use, showing discovered machines and live power ]", 1700),
      SPACER(100),
      PLACEHOLDER("[ Photo 3 — the bench setup with the test load running ]", 1700),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 7. Challenges ---------------- */
      H1("7. Challenges, Learnings & Future Improvements"),

      H2("Challenges Faced"),
      BULLET_R([["A fixed detection threshold cannot work. ", { bold: true }],
        ["The first change-point detector produced 479 edges against 71 real switching events, because a " +
         "2.2 kW lathe with 6% ripple swings ±130 W while doing nothing unusual. No single threshold catches a " +
         "340 W fan at 2 a.m. without drowning during the day. The threshold is now the larger of an absolute " +
         "floor and a multiple of the locally measured noise, and the noise estimate only updates on " +
         "sub-threshold movement so a real step cannot teach the detector to ignore steps its own size."]]),
      BULLET_R([["The obvious health model was inverted. ", { bold: true }],
        ["Scoring aggregate windows gave 0.767 mean anomaly on held-out healthy data against 0.498 on degraded " +
         "data. It had learned the shift roster. This forced the architecture: disaggregate first, diagnose " +
         "per machine."]]),
      BULLET_R([["Degradation broke machine identity. ", { bold: true }],
        ["As a motor wears it draws more power at worse power factor, drifts outside its own cluster's " +
         "tolerance, and gets filed as a brand-new appliance — taking its health history with it. The symptom " +
         "was the compressor's event count frozen at 515 while replacement clusters appeared. Cluster centroids " +
         "now track slow drift while the health baseline stays frozen."]]),
      BULLET_R([["Solar contaminates disaggregation at the grid tie. ", { bold: true }],
        ["With the meter measuring import, a cloud crossing looks exactly like a machine switching. Over a week " +
         "this produced six clusters spanning 1243–1828 W where two machines existed. Generation is now added " +
         "back to recover the load-side signal before detection."]]),
      BULLET_R([["Calibrating a health score took three attempts. ", { bold: true }],
        ["Thresholds taken from the training data are optimistic and left healthy machines reading 57/100 " +
         "permanently; a 99th percentile of a 20-point calibration slice is just its maximum, so one unusual " +
         "start flattened everything to 43/100. The scale is now median + 3×MAD on held-out data."]]),
      BULLET_R([["A silent WebSocket 403. ", { bold: true }],
        ["The dashboard ran on its single startup fetch and never updated. FastAPI resolves parameter types at " +
         "runtime, the module used deferred annotations, and the FastAPI imports were function-local — so the " +
         "socket parameter was treated as a missing query field. Found by watching the browser console, not by " +
         "reading the code."]]),

      H2("What You Learned"),
      P("The recurring lesson is that a model can look correct and be inverted, and that only measurement " +
        "against ground truth tells the difference. Every significant fix in this project came from a number " +
        "that disagreed with expectation — 479 edges, a 0.767 anomaly score on healthy data, an event counter " +
        "that stopped moving, a residual of −3781 W. None of them raised an error. All of them would have " +
        "shipped."),
      P("The second lesson is that constraints are where the design comes from. Having one meter forced " +
        "disaggregation, which turned out to be the interesting part. Having no labels forced everything to be " +
        "self-supervised, which is what makes it deployable in a workshop nobody will ever instrument. Having " +
        "to install on aarch64 ruled out XGBoost and PyTorch, which is why the models are small enough to " +
        "retrain on the board. Having a battery with a finite cycle life is what makes “do nothing” a valid and " +
        "frequently correct answer."),

      H2("Future Improvements"),
      BULLET_R([["Multi-drop the inverter. ", { bold: true }],
        ["The RS485 bus takes 32 devices. Putting a hybrid inverter on it at a second slave address turns solar " +
         "and battery state from estimated into measured, with no new wiring, and directly improves both " +
         "disaggregation and dispatch."]]),
      BULLET_R([["A high-rate current channel for real MCSA. ", { bold: true }],
        ["A split-core CT on the STM32's ADC sampled in the kilohertz would give the microcontroller genuine " +
         "spectral work to do and turn trend detection into fault identification — naming a bearing defect " +
         "rather than reporting that something has changed."]]),
      BULLET_R([["Operator confirmation of discovered machines. ", { bold: true }],
        ["The node can tell that a 1.5 kW motor with a 3× inrush exists; only the person who works there knows " +
         "it is the compressor. Renaming already works; a guided first-run flow that walks an operator through " +
         "switching each machine once would collapse the discovery period from days to minutes."]]),
      BULLET_R([["Three-phase support. ", { bold: true }],
        ["The meter and the model are single-phase today, which fits MSME machine shops. Three balanced " +
         "channels of the same pipeline extends it to larger plants, and phase imbalance becomes an additional " +
         "and quite sensitive health signal."]]),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 8. Declaration ---------------- */
      H1("8. Declaration"),
      P("We confirm that this is our original, unpublished work. The Arduino® UNO™ Q is the primary board in " +
        "this project. All team members have reviewed and agree to this report."),
      SPACER(100),
      P("We further confirm that every quantitative result in this report was produced against a simulated " +
        "plant with known ground truth, is labelled as such, and has not been presented as a measurement taken " +
        "on physical hardware. Meter frames carry a provenance field end to end specifically to prevent that " +
        "confusion."),
      SPACER(260),
      FIELDS([
        FIELD("Date", null, "— date of submission —"),
        FIELD("Team Leader signature", null, "— sign —"),
      ]),
      SPACER(360),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        border: { top: { style: BorderStyle.SINGLE, size: 4, color: RULE, space: 10 } },
        spacing: { before: 200 },
        children: [new TextRun({
          text: "Arduino Physical AI Challenge India 2026 · Robu.in × Arduino · contest@robu.in",
          size: 17, color: MUTED, font: "Calibri",
        })],
      }),
    ],
  }],
});

Packer.toBuffer(doc).then((buf) => {
  const out = process.argv[2] || "Udyog_IQ_Project_Report.docx";
  fs.writeFileSync(out, buf);
  console.log("wrote", out, (buf.length / 1024).toFixed(1) + " KB");
});
