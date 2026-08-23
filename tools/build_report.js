const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat, PageBreak, convertInchesToTwip, ImageRun,
} = require("docx");

/* ------------------------------------------------------------------ */
/* Style helpers                                                       */
/* ------------------------------------------------------------------ */
const INK = "1A1A1A";
const MUTED = "5A6270";
const ACCENT = "0B5394";
const RULE = "C9CFD8";
const PANEL = "F2F5F9";
const TABLE_W = 9360;

const P = (text, opts = {}) =>
  new Paragraph({
    spacing: { after: opts.after ?? 120, line: opts.line ?? 264 },
    alignment: opts.align,
    children: [new TextRun({
      text, size: opts.size ?? 20, color: opts.color ?? INK,
      bold: opts.bold, italics: opts.italics, font: "Calibri",
    })],
  });

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

const BULLET = (text) => new Paragraph({
  numbering: { reference: "bullets", level: 0 },
  spacing: { after: 70, line: 264 },
  children: [new TextRun({ text, size: 20, color: INK, font: "Calibri" })],
});

const BULLET_R = (runs) => new Paragraph({
  numbering: { reference: "bullets", level: 0 },
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
        value ? [[value, { size: 19 }]]
              : [[hint || "to be completed", { size: 19, color: "B03A2E", italics: true }]],
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

const DIAGRAMS = "D:/AI-Challange/Physical-AI-Challange/docs/diagrams";

/** Full content width figure. 6.5in at 96 dpi is 624 px. */
const FIGURE = (file, aspect, caption) => [
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 60, after: 60 },
    children: [new ImageRun({
      data: fs.readFileSync(`${DIAGRAMS}/${file}`),
      type: "png",
      transformation: { width: 624, height: Math.round(624 / aspect) },
    })],
  }),
  PR([[caption, { size: 17, color: MUTED, italics: true }]],
    { align: AlignmentType.CENTER, after: 140 }),
];

/* ================================================================== */
const doc = new Document({
  creator: "Vedant Charegaonkar",
  title: "Udyog IQ, Arduino Physical AI Challenge India 2026",
  description: "Edge AI energy intelligence for small industry on the Arduino UNO Q",
  numbering: {
    config: [{
      reference: "bullets",
      levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 340, hanging: 200 } } } },
      ],
    }, {
      reference: "steps",
      levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 380, hanging: 240 } } } },
      ],
    }],
  },
  styles: { default: { document: { run: { font: "Calibri", size: 20, color: INK } } } },
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
      PR([["ARDUINO PHYSICAL AI CHALLENGE INDIA 2026", { bold: true, size: 19, color: MUTED }]], { after: 60 }),
      PR([["Project Report", { size: 19, color: MUTED }]], { after: 240 }),

      new Paragraph({
        spacing: { after: 60 },
        children: [new TextRun({ text: "UDYOG IQ", size: 52, bold: true, color: ACCENT, font: "Calibri" })],
      }),
      PR([["Edge AI energy intelligence for small industry. One meter, one board, no cloud.",
        { size: 24, color: INK }]], { after: 300 }),

      FIELDS([
        FIELD("Project Title", "Udyog IQ: Edge AI Energy Intelligence for Small Industry"),
        FIELD("Team Name", null, "enter your registered team name"),
        FIELD("Registration / Team ID", null, "enter your portal team ID"),
        FIELD("Contest Track", "Industrial and Sustainability AI"),
        FIELD("Institution & City", null, "enter your institution and city"),
      ]),

      SPACER(220),
      H2("Team Members"),
      TBL(["Role", "Name", "Email"], [
        ["Team Leader", "Vedant Charegaonkar", "vec.vedant@gmail.com"],
        ["Member 2 (optional)", "", ""],
        ["Member 3 (optional)", "", ""],
        ["Member 4 (optional)", "", ""],
      ], [2200, 3580, 3580]),

      SPACER(200),
      CALLOUT("In one sentence", [
        "A small factory has a dozen machines, one electricity connection, and a bill nobody understands. " +
        "Udyog IQ puts a single energy meter on the incoming supply and an Arduino UNO Q beside it, and works out " +
        "by itself, with no labelled training data, which machines are running, which one is starting to fail, " +
        "how much money is being burned on idle, and when to use solar, battery or grid so the plant pays less.",
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 1. Overview ---------------- */
      H1("1. Project Overview"),

      H2("Problem Statement"),
      P("India has millions of MSME workshops: machine shops, textile units, cold stores, small fabricators. " +
        "Each runs a handful of motors on one electricity connection and receives a bill it cannot explain, " +
        "because metering every machine costs more than the electricity it would save. Two large costs therefore " +
        "go unmanaged. Utilities bill the highest average demand in any half hour of the month, priced per kVA, " +
        "so one careless half hour where several motors start together sets a charge paid every month afterwards. " +
        "Separately, compressors and pumps fail without warning even though their electrical signature had been " +
        "drifting for weeks, because nobody was watching it."),

      H2("How Your Project Works"),
      P("A single Selec EM2M energy meter sits on the incoming supply and is read over RS485 Modbus by the " +
        "Arduino UNO Q. The STM32 microcontroller masters the bus at 1 Hz and drives a contactor behind a " +
        "hardware interlock. The Qualcomm processor, running Debian, does everything else on the board."),
      P("Every time a machine switches on or off it leaves a step in real and reactive power. The node clusters " +
        "those step signatures and recovers the individual machines from the aggregate, so one meter serves the " +
        "whole workshop. Each machine's start events are then scored against a model of its own learned normal, " +
        "which flags degradation before failure. In parallel the node forecasts plant load from its own history " +
        "and solar generation from a weather feed, then solves a 24 hour battery schedule against time of day " +
        "tariffs every fifteen minutes. Idle machines are cut, demand peaks are shaved before they are set, and " +
        "a shadow ledger records what the same day would have cost without the device. Everything runs on the " +
        "board, and a dashboard is served from it over the local network."),

      H2("Why Arduino UNO Q?"),
      P("This project needs two different kinds of computer at once, and the UNO Q is one board that is both."),
      TBL(["Brain", "What it runs", "Why it must be this one"], [
        [["STM32U585", "Cortex-M33"],
          "Modbus RTU master, contactor interlock, watchdog",
          "RS485 has a turnaround deadline. The driver must release the line within a character time of the last stop bit or the reply collides with our own echo. Linux meets that deadline almost always, and almost means a corrupt frame every few minutes that looks exactly like a wiring fault."],
        [["Qualcomm QRB2210", "quad Cortex-A53, Debian"],
          "Disaggregation, anomaly detection, forecasting, dispatch optimiser, historian, dashboard",
          "scikit-learn, a SQLite historian, a 96 step optimisation recomputed every 15 minutes, and a web server. This is not microcontroller work."],
      ], [1900, 2700, 4760]),
      SPACER(140),
      P("The division also matters for safety. Minimum contactor dwell times and the switching rate cap are " +
        "enforced on the microcontroller, so they hold even when the Linux side hangs, fills its disk, or is " +
        "being updated. The Python policy engine checks the same rules first, but only so that it does not " +
        "issue requests which would be refused. It is not the safety mechanism."),
      SPACER(60),
      CALLOUT("The honest version of this claim", [
        "An ESP32 could read the meter but could not run the learning half. A Raspberry Pi could run the " +
        "learning half but could not promise the real time half. Using both would mean two boards, two power " +
        "supplies and a link between them. One UNO Q replaces a PLC, a protocol gateway and an edge PC, and " +
        "that is the specific reason this project exists on this hardware rather than being ported to it.",
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 2. BOM ---------------- */
      H1("2. Components Used (BOM)"),
      TBL(["Component", "Qty", "Notes"], [
        ["Arduino UNO Q (ABX00087)", "1", "4 GB / 32 GB variant. Qualcomm Dragonwing QRB2210 with STM32U585. Purchase proof uploaded separately."],
        ["Selec EM2M-1P-C-100A energy meter", "1", "Class 1, single phase, direct connected to 100 A with no external CT. RS485 Modbus RTU."],
        ["Isolated RS485 to UART converter", "1", "Galvanic isolation between the mains referenced meter and the board."],
        ["Relay or contactor module", "1", "Opto isolated input, sized for the controlled circuit."],
        ["5 V USB C power supply, 3 A or better", "1", "The UNO Q draws well past a phone charger's rating under load."],
        ["Screened 3 core cable, ferrules, DIN rail, enclosure", "as needed", "Screen earthed at one end only."],
        ["120 ohm termination resistors", "2", "Only for RS485 runs beyond a few metres."],
      ], [3500, 900, 4960]),
      SPACER(140),
      CALLOUT("Part number check", [
        "This template lists the UNO Q as ABX00087, while Arduino's store lists ABX00162 for the 2 GB board. " +
        "Confirm against the invoice so that the BOM and the purchase proof agree.",
      ]),
      SPACER(120),
      P("A split core current transformer sampled at kilohertz on the STM32 ADC was considered and deliberately " +
        "left out. It would have enabled genuine motor current signature analysis, but no result in this report " +
        "depends on it, and the limitation is more useful stated plainly than hidden behind hardware added to " +
        "make a claim sound better.", { color: MUTED }),

      /* ---------------- 3. Architecture ---------------- */
      H1("3. System Architecture & Circuit"),

      H2("Step by Step Workflow"),
      ...[
        "Acquire. The STM32 polls the Selec meter over RS485 at 1 Hz and caches the decoded block. Python pulls that snapshot across the Arduino Bridge and rejects it if stale, because a repeated stale frame is indistinguishable from a genuinely steady load.",
        "Compensate. With the meter at the grid tie, measured solar and battery power are added back to recover the load side signal. Generation moves independently of the machines, and a cloud crossing otherwise looks exactly like a motor starting.",
        "Detect. An adaptive threshold change point detector turns the power trace into discrete switching events, each carrying a signature of its step in real and reactive power.",
        "Disaggregate. Those signatures are clustered online into individual machines. One meter, many machines, no labels.",
        "Diagnose. Each machine's start events are scored against a density model of its own learned normal, and drift in power factor, draw and inrush is tracked separately.",
        "Forecast. Plant load comes from the site's own history. Solar comes from an Open-Meteo feed through a clear sky physics model with a learned site correction.",
        "Decide. Dynamic programming over discretised battery state of charge, recomputed every 15 minutes in a receding horizon control loop.",
        "Act. Idle cutoff and demand shedding through the hardware interlock, in advisory mode until an operator deliberately enables actuation.",
        "Account. A shadow ledger runs the same day with no battery movement and no idle cutoff, so the reported saving is a measured difference rather than a claim.",
      ].map((t) => new Paragraph({
        numbering: { reference: "steps", level: 0 },
        spacing: { after: 90, line: 264 },
        children: [new TextRun({ text: t, size: 20, font: "Calibri" })],
      })),

      SPACER(160),
      H2("Block Diagram"),
      P("The whole installation, not only the controller. Power runs left to " +
        "right along the top: the three sources meet at the inverter, pass " +
        "through the meter, pass through the contactor, and reach the machines. " +
        "The contactor really is in series there. The node sits underneath and " +
        "touches the power path at exactly two points, reading the meter and " +
        "commanding the contactor."),
      ...FIGURE("system_block_diagram.png", 2272 / 1255,
        "Figure 1. Udyog IQ system block diagram. Thick arrows carry power, thin dashed arrows carry signals."),
      SPACER(180),

      H2("Circuit and Wiring"),
      TBL(["Signal", "UNO Q pin", "STM32 pin", "Note"], [
        ["RS485 TX", "D1", "PB6", "usart1 TX per the Zephyr device tree"],
        ["RS485 RX", "D0", "PB7", "usart1 RX"],
        ["RS485 DE and RE", "D2", "PB3", "Tied together. HIGH transmits."],
        ["Contactor", "D7", "PB2", "Most relay boards are active low"],
        ["3V3 and GND", "", "", "Powers the isolated converter's logic side"],
      ], [1900, 1500, 1500, 4460]),
      SPACER(140),
      CALLOUT("One unresolved hardware question, handled in software", [
        "The Zephyr device tree maps the UNO Q's D0 and D1 header pins to usart1 and aliases it arduino_serial, " +
        "and puts the link to the Qualcomm side on lpuart1 (pins PG5 to PG8, with hardware flow control, not " +
        "brought out to the headers). At least one published tutorial states that Serial1 is reserved, which " +
        "contradicts the device tree.",
        "This is not resolvable from documentation alone, so it is a single compile time definition in the " +
        "sketch, and the meter transport is a configuration key with three backends: mastered by the MCU, " +
        "mastered by Linux over a USB to RS485 adapter, or simulated. Whichever way the bring up lands, it is a " +
        "one line change rather than a rewrite.",
      ]),
      SPACER(140),
      ...FIGURE("wiring_diagram.png", 2207 / 964,
        "Figure 2. Circuit detail. The isolation barrier is the important part: the meter terminals sit at mains potential and the board does not."),
      SPACER(120),
      PLACEHOLDER("[ Insert photograph of the assembled hardware: UNO Q, meter, isolated RS485 converter and contactor on DIN rail ]", 2100),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 4. AI / ML ---------------- */
      H1("4. AI / ML Model Details"),
      FIELDS([
        FIELD("Model Used", "Five models. Online leader clustering for machine discovery; linear autoencoder (PCA reconstruction) with Isolation Forest for health; HistGradientBoostingRegressor for load and solar forecasting; dynamic programming with receding horizon control for dispatch."),
        FIELD("Training Platform", "scikit-learn, trained and retrained on the UNO Q itself from the on board SQLite historian. Nothing is trained in a cloud notebook and shipped down."),
        FIELD("Accuracy", "Not yet measured on hardware. The proof of concept bench described in section 6 is the first opportunity to quote a figure, and none is claimed until it has been taken."),
        FIELD("Dataset", "No external dataset and no pre trained weights. Every model builds its own supervision from the site's own unlabelled stream, one sample per second from the meter. Nothing is downloaded."),
      ]),

      SPACER(200),
      H2("How the models fit together"),
      P("The organising constraint is that there is no labelled data and there never will be. Nobody is going to " +
        "instrument a workshop to record which machine produced which step, and a model trained on another " +
        "factory would not transfer, because the whole point is that these are this site's machines. Every model " +
        "is therefore unsupervised or self supervised."),
      SPACER(60),
      TBL(["Purpose", "Method", "Where the labels come from"], [
        ["Machine discovery", "Online leader clustering of switching signatures in real and reactive power", "Nowhere. Machines are discovered, not classified."],
        ["Predictive maintenance", "Linear autoencoder plus Isolation Forest, one per machine", "Each machine's own first 80 start events define its normal."],
        ["Load forecasting", "Gradient boosting, direct multi horizon", "The plant's own demand a few blocks later, already in the historian."],
        ["Solar forecasting", "Clear sky physics with a learned residual correction", "Measured generation against the weather forecast for that hour."],
        ["Battery dispatch", "Dynamic programming over discretised state of charge", "Optimisation, not learning. No training involved."],
      ], [2100, 3400, 3860]),

      SPACER(180),
      H2("Why not XGBoost, and why not a neural network"),
      P("HistGradientBoostingRegressor is the same algorithm family as XGBoost, ships inside scikit-learn, and " +
        "installs on the board's aarch64 Debian without a compiler. XGBoost would have meant building from " +
        "source on a 2 GHz Cortex-A53, and a dependency that will not install on the target is not a dependency, " +
        "it is a bug. PyTorch and TensorFlow were excluded for the same reason, which is why the anomaly " +
        "detector is a linear autoencoder rather than a neural one. Every model here retrains on the board in " +
        "seconds, which is what makes on device learning practical rather than aspirational."),

      H2("Brief Description and Limitations"),
      P("Disaggregation must come before diagnosis. This was not obvious and it cost a rewrite. An early health " +
        "model scored the plant's aggregate feature windows and appeared to work, until it was checked against " +
        "data it had not seen, where it ranked ordinary healthy operation as more anomalous than genuinely " +
        "degraded operation. The ranking was inverted. An aggregate window changes far more when a different " +
        "mix of machines happens to be running than it does when one machine degrades, so the model had learned " +
        "the shift roster rather than the machines, and a quiet afternoon looked more alarming than a failing " +
        "compressor. Health is now scored per machine, on events that disaggregation has already attributed, " +
        "each of which belongs to exactly one machine."),
      SPACER(80),
      CALLOUT("Where this degrades or fails, stated plainly", [
        "This is not motor current signature analysis. That technique resolves sidebands around the supply " +
        "frequency to identify broken rotor bars and bearing defects, and it needs current sampled in the " +
        "kilohertz. At one sample per second those sidebands do not exist in our data at any resolution. What " +
        "is claimed here is trend and anomaly detection on aggregate electrical parameters: a real technique, " +
        "and a different one.",
        "Disaggregation cannot see a small load hiding under a large running one. The detection threshold " +
        "scales with local noise, so while a large motor runs, a small fan switching sits below the floor. " +
        "This is a structural property of measuring at a single point, not a tuning failure.",
        "Machines that switch rarely take proportionally longer to discover, because a machine only becomes " +
        "knowable once it has switched often enough to form a cluster. That is the honest cost of having no " +
        "labels.",
        "Two machines that always switch together will be reported as one, and simultaneous switching produces " +
        "composite clusters. The dashboard shows these as unnamed candidates for the operator to confirm or " +
        "ignore, rather than asserting that they are machines.",
        "Battery dispatch is advisory unless the inverter accepts external commands. Where it does not, the " +
        "node still reports the saving it would have captured, and still performs load side actuation.",
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 5. Code ---------------- */
      H1("5. Code Structure"),
      P("The repository is an Arduino App Lab project (app.yaml, python/, sketch/) wrapping an importable Python " +
        "package, so that the models can be developed and tested away from the board."),
      SPACER(60),
      TBL(["Module", "Responsibility"], [
        ["sketch/sketch.ino", "STM32 firmware. Modbus RTU master with CRC and bus turnaround, contactor interlock (minimum dwell, switching rate cap, fail safe closed), and the Bridge RPC surface."],
        ["python/main.py", "App Lab entry point on the Qualcomm side. Starts the node, handles shutdown signals so the historian flushes cleanly."],
        ["udyogiq/transport/", "Three interchangeable meter backends (bridge, serial, sim) plus inverter adapters for measured or estimated source telemetry."],
        ["udyogiq/meter/", "Selec EM2M register map, float decoding, and a physical sanity check on every frame."],
        ["udyogiq/pipeline/", "Ring buffer, windowed feature extraction, adaptive threshold change point detection."],
        ["udyogiq/ml/", "Disaggregation, health, load forecast, solar forecast, battery model."],
        ["udyogiq/policy/", "Dynamic programming dispatch optimiser, receding horizon loop, and the decision engine that issues actions with reasons."],
        ["udyogiq/sustain/", "Time of day tariff engine, weather client, carbon and counterfactual savings accounting."],
        ["udyogiq/store/", "SQLite historian with batched writes, one minute rollup and retention."],
        ["udyogiq/api/", "FastAPI and WebSocket server."],
        ["udyogiq/runtime.py", "Orchestrator. One acquisition thread, everything else on a cooperative timer."],
        ["web/", "Dashboard with two views, Overview and Forecast, served from the board."],
        ["sim/", "Synthetic workshop with solar, battery and injectable faults."],
        ["tools/probe_meter.py", "Hardware bring up. Verifies the register map against a real meter."],
        ["tests/", "27 regression tests, each corresponding to a fault found by measurement."],
      ], [2600, 6760]),

      SPACER(160),
      H2("Key functions"),
      BULLET_R([["EdgeDetector.push()", { font: "Consolas", bold: true }],
        [" turns the power trace into confirmed switching events. Transients that return to their origin emit nothing."]]),
      BULLET_R([["NILMEngine.push()", { font: "Consolas", bold: true }],
        [" attributes an event to a discovered machine, or founds a new cluster."]]),
      BULLET_R([["ApplianceHealth.push_edge()", { font: "Consolas", bold: true }],
        [" scores one start event against that machine's learned normal."]]),
      BULLET_R([["DispatchOptimiser.solve()", { font: "Consolas", bold: true }],
        [" backward induction over 96 stages, 41 states of charge and 21 actions."]]),
      BULLET_R([["PolicyEngine.evaluate_idle()", { font: "Consolas", bold: true }],
        [" finds machines running at standby draw and cuts them, subject to the interlock."]]),
      BULLET_R([["UdyogIQ.warmup()", { font: "Consolas", bold: true }],
        [" replays simulated history at processor speed so the node starts already knowing a plant."]]),

      SPACER(200),
      FIELDS([
        FIELD("GitHub Repository", "https://github.com/vecvedant/Physical-AI-Challange"),
        FIELD("Demo Video Link", null, "paste the public YouTube or Drive link"),
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 6. Testing ---------------- */
      H1("6. Testing & Results"),

      H2("What has been tested so far"),
      P("Testing to date is of the software, end to end, and of the bench build. " +
        "The pipeline was exercised continuously from acquisition through " +
        "disaggregation, health scoring, forecasting and dispatch to the " +
        "dashboard, with the policy engine in advisory mode so that every " +
        "decision was logged and none was executed."),
      P("A regression suite of 27 tests covers the parts that broke during " +
        "development: register decoding and the physical sanity check that " +
        "rejects a swapped word order, buffer ordering, event detection against " +
        "load ripple, expiry of stale machine state, the battery charge floor, " +
        "the dispatch plan never scoring worse than doing nothing, demand " +
        "averaged over the billing window rather than instantaneously, advisory " +
        "mode never actuating, critical loads never shed, and the interlock " +
        "refusing a switch that comes too soon. Each test corresponds to a fault " +
        "that was found by measurement rather than by reading the code."),

      SPACER(120),
      CALLOUT("No performance figures are quoted in this report", [
        "Accuracy, savings and detection rates all describe how the system " +
        "behaves against real machines on a real supply, and that measurement " +
        "has not been taken yet. Numbers produced during development came from a " +
        "simulator written to exercise the code, and a simulator can only " +
        "confirm that the software does what it was told to do. Quoting them " +
        "here would describe the simulator, not the plant.",
        "The table below is left blank deliberately. It is filled in from the " +
        "bench described next, and not before.",
      ]),

      SPACER(160),
      H2("Proof of concept bench"),
      P("The bench is the meter wired to a single phase supply feeding a motor " +
        "load, with the isolated converter and the contactor as drawn in figure " +
        "2. The procedure is ordered so that each step removes a class of failure " +
        "before the next step can be confused by it."),
      ...[
        "Verify the meter answers on RS485 and that the register map is correct. The probe tool sweeps baud rates and addresses, decides word order from physics that must hold on any single phase supply, and prints readings to check against the meter's own display. A wrong register offset does not raise an error, it returns a plausible wrong number, so this step is not optional.",
        "Record a baseline with the motor switching normally, and confirm that the node discovers it as a machine and that its step size matches what the meter reports.",
        "Let the health model observe enough starts to leave its learning state, then change the machine's condition, for example by loading it more heavily or restricting its airflow, and record whether and when the health score responds.",
        "Compare the forecast against what the plant actually drew over the following period.",
        "Exercise the contactor through the interlock: confirm minimum dwell is enforced, confirm the switching rate cap holds, and confirm the contactor returns to closed when the Linux side is stopped.",
      ].map((t) => new Paragraph({
        numbering: { reference: "steps", level: 0 },
        spacing: { after: 90, line: 264 },
        children: [new TextRun({ text: t, size: 20, font: "Calibri" })],
      })),

      SPACER(160),
      H2("Results"),
      P("To be completed from the bench above. Record what was measured, not what " +
        "was expected.", { color: MUTED, italics: true }),
      SPACER(60),
      TBL(["Measurement", "Method", "Result"], [
        ["Meter register map verified against the display", "tools/probe_meter.py", ""],
        ["Machine discovered from the aggregate signal", "run the motor, watch the dashboard", ""],
        ["Step size reported against meter reading", "compare with the meter display", ""],
        ["Time taken for the machine to be confirmed", "from first start to confirmation", ""],
        ["Health score response to a changed condition", "load or restrict the machine", ""],
        ["Forecast error over the following period", "compare against measured demand", ""],
        ["Contactor interlock enforced", "attempt a switch inside the dwell time", ""],
        ["Contactor restored when the host is stopped", "stop the application, observe", ""],
        ["Acquisition reliability over a continuous run", "successful reads divided by attempts", ""],
      ], [3500, 3200, 2660]),

      SPACER(200),
      H2("Project Images"),
      PLACEHOLDER("[ Photo 1: the assembled node. UNO Q, Selec meter, isolated RS485 converter, contactor ]", 1700),
      SPACER(100),
      PLACEHOLDER("[ Photo 2: the dashboard in use, showing the discovered machine and live power ]", 1700),
      SPACER(100),
      PLACEHOLDER("[ Photo 3: the bench with the motor load running ]", 1700),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 7. Challenges ---------------- */
      H1("7. Challenges, Learnings & Future Improvements"),

      H2("Challenges Faced"),
      BULLET_R([["A fixed detection threshold cannot work. ", { bold: true }],
        ["The first change point detector fired constantly, because a large machine under load ripples by more " +
         "than a small machine draws in total. No single threshold catches a small fan switching at night " +
         "without drowning during the day. The threshold is now the larger of an absolute floor and a multiple " +
         "of the locally measured noise, and the noise estimate updates only on movement below threshold, so a " +
         "real step cannot teach the detector to ignore steps of its own size."]]),
      BULLET_R([["The obvious health model was inverted. ", { bold: true }],
        ["Scoring the plant's aggregate windows ranked healthy operation as more anomalous than degraded " +
         "operation on data the model had not seen. It had learned the shift roster rather than the machines. " +
         "This forced the architecture: disaggregate first, then diagnose per machine."]]),
      BULLET_R([["Degradation broke machine identity. ", { bold: true }],
        ["As a motor wears it draws more power at worse power factor, drifts outside its own cluster tolerance, " +
         "and is filed as a brand new appliance, taking its health history with it. The symptom was a machine " +
         "whose event count stopped rising while replacement clusters appeared beside it. Cluster centroids now " +
         "track slow drift while the health baseline stays frozen."]]),
      BULLET_R([["Solar contaminates disaggregation at the grid tie. ", { bold: true }],
        ["With the meter measuring import, a cloud crossing looks exactly like a machine switching, and a " +
         "machine starting while irradiance falls registers a step of the wrong size. Generation is now added " +
         "back to recover the load side signal before detection."]]),
      BULLET_R([["Calibrating a health score took three attempts. ", { bold: true }],
        ["Thresholds taken from the training data are optimistic, and left healthy machines reading as " +
         "permanently unwell. Taking a high percentile from a small calibration slice is barely different from " +
         "taking its maximum, so a single unusual start flattened every score afterwards. The scale is now a " +
         "robust median and deviation estimate on data the model has not seen."]]),
      BULLET_R([["A silent WebSocket rejection. ", { bold: true }],
        ["The dashboard ran on its single startup fetch and never updated. The web framework resolves parameter " +
         "types at runtime, the module used deferred annotations, and its imports were function local, so the " +
         "socket parameter was treated as a missing query field. Found by watching the browser console, not by " +
         "reading the code."]]),

      H2("What You Learned"),
      P("The recurring lesson is that a model can look correct and be inverted, and that only measurement " +
        "against something you already know the answer to will tell you the difference. Every significant fix " +
        "in this project came from a number that disagreed with expectation rather than from an error message. " +
        "None of those faults raised an exception. All of them would have shipped."),
      P("The second lesson is that constraints are where the design comes from. Having one meter forced " +
        "disaggregation, which turned out to be the interesting part. Having no labels forced everything to be " +
        "self supervised, which is what makes it deployable in a workshop nobody will ever instrument. Having " +
        "to install on the board's processor ruled out the heavier libraries, which is why the models are small " +
        "enough to retrain on the board itself. Having a battery with a finite cycle life is what makes doing " +
        "nothing a valid and frequently correct answer."),
      P("The third is about honesty in reporting. A simulator was written to exercise the pipeline, and it was " +
        "genuinely useful for finding faults. It cannot tell you how the system performs on a real supply, and " +
        "this report quotes no figure from it, because a number with the wrong provenance is worse than no " +
        "number at all."),

      new Paragraph({ children: [new PageBreak()] }),

      /* ---------------- 8. Declaration ---------------- */
      H1("8. Declaration"),
      P("We confirm that this is our original, unpublished work. The Arduino UNO Q is the primary board in this " +
        "project. All team members have reviewed and agree to this report."),
      SPACER(100),
      P("We further confirm that this report quotes no performance figure that has not been measured. Results " +
        "from the proof of concept bench are recorded in section 6 as they are taken, and nothing produced by " +
        "the development simulator is presented as a measurement. Meter readings carry a provenance field " +
        "through the whole system specifically to keep that distinction enforceable in software rather than by " +
        "memory."),
      SPACER(260),
      // The template asks for a date and nothing else here. An invented
      // signature row would be a field the entrant has to explain rather than
      // one the organisers requested.
      FIELDS([
        FIELD("Date", null, "date of submission"),
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
