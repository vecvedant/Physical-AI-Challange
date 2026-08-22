/*
 * Udyog IQ - real-time half, running on the STM32U585.
 *
 * This sketch owns two things that must never depend on Linux being
 * responsive: the RS485 bus, and the contactor.
 *
 * Why the MCU masters Modbus
 * --------------------------
 * RS485 is a half-duplex bus with a turnaround deadline. The transceiver's
 * driver has to be disabled within a character time of the last stop bit or it
 * holds the line and the slave's reply collides with our own echo. A Linux
 * scheduler will meet that deadline almost every time, and "almost" produces a
 * corrupt frame every few minutes that looks exactly like a wiring fault. Here
 * the turnaround is a few instructions after the UART flushes.
 *
 * Why the MCU owns the contactor
 * ------------------------------
 * The interlock below - minimum dwell, switching-rate cap, fail-safe state - is
 * the actual safety mechanism. The Python policy engine checks the same rules
 * before asking, but that is a courtesy so it does not issue requests that will
 * be refused. If the MPU hangs, crashes, runs out of disk or is being updated,
 * these rules still hold, because they are running on a microcontroller that is
 * doing nothing else. A single-brain board cannot make that promise.
 *
 * Failure posture: the contactor fails CLOSED. Losing communications must never
 * strand a machine without power - an unexpectedly dead compressor is a
 * production incident, and an unexpectedly live one is the state it was already
 * in. Anything that genuinely must open on fault belongs behind a hardware
 * safety relay, not behind this.
 */

#include "Arduino_RouterBridge.h"

/* ------------------------------------------------------------------------ */
/* Wiring                                                                    */
/* ------------------------------------------------------------------------ */

/*
 * Which UART reaches the RS485 transceiver.
 *
 * The Zephyr device tree for this board maps the Arduino header's D0/D1 to
 * usart1 (D0 = PB7 = RX, D1 = PB6 = TX) and aliases it as arduino_serial, while
 * the link to the Qualcomm MPU runs on lpuart1 (PG5-PG8, with hardware flow
 * control, not brought out to the headers). At least one published tutorial
 * claims Serial1 is reserved for the router, which disagrees with the device
 * tree.
 *
 * Rather than gamble, this is one #define and tools/probe_meter.py can verify
 * the bus from the Linux side independently. If Serial1 turns out to be taken,
 * change it here and rebuild; nothing else in the project cares.
 */
#define RS485_SERIAL   Serial1
#define RS485_BAUD     9600

/* Transceiver direction control. DE and RE are usually strapped together on a
 * breakout, so one pin drives both: HIGH transmits, LOW receives. */
#define RS485_DE_PIN   2
/* Set to 1 if the module handles direction automatically. */
#define RS485_AUTO_DIR 0

#define CONTACTOR_PIN  7
/* Most relay boards are active-low. Wrong polarity here energises the
 * contactor at boot, so check before connecting anything that matters. */
#define CONTACTOR_ACTIVE_LOW 1

#define STATUS_LED     LED_BUILTIN

/* ------------------------------------------------------------------------ */
/* Meter                                                                     */
/* ------------------------------------------------------------------------ */
#define METER_SLAVE_ID     1
/* Selec EM2M-1P-C: 32-bit floats in input registers (function 4) from 30001.
 * Offsets are zero-based on the wire, so 30009 (active power) is offset 8. */
#define METER_BLOCK_START  0
#define METER_BLOCK_LEN    34      /* 17 floats */

#define POLL_INTERVAL_MS   1000
#define MODBUS_TIMEOUT_MS  500
#define MODBUS_RETRIES     2

/* ------------------------------------------------------------------------ */
/* Interlock - the authoritative copy                                        */
/* ------------------------------------------------------------------------ */
#define MIN_ON_MS              30000UL      /* 30 s */
#define MIN_OFF_MS             60000UL      /* 60 s */
#define MAX_SWITCHES_PER_HOUR  10

/* If the MPU stops asking for data for this long, assume it is gone and
 * restore the contactor. See the failure posture note above. */
#define MPU_SILENCE_TIMEOUT_MS 120000UL     /* 2 minutes */

/* Interlock status bits, mirrored in transport/bridge.py STATUS_ORDER. */
#define FLAG_DWELL_BLOCK    (1 << 0)
#define FLAG_RATE_BLOCK     (1 << 1)
#define FLAG_MPU_SILENT     (1 << 2)
#define FLAG_METER_FAULT    (1 << 3)
#define FLAG_MANUAL_LOCK    (1 << 4)

/* ------------------------------------------------------------------------ */
/* State                                                                     */
/* ------------------------------------------------------------------------ */
static float    gMeter[17];               /* newest decoded parameter block */
static uint32_t gMeterStampMs   = 0;      /* millis() of last good read */
static uint32_t gModbusErrors   = 0;
static uint32_t gModbusReads    = 0;
static bool     gMeterValid     = false;

static bool     gContactorClosed = true;
static uint32_t gLastSwitchMs    = 0;
static uint8_t  gSwitchCount     = 0;
static uint32_t gSwitchWindowMs  = 0;
static uint32_t gLastMpuCallMs   = 0;
static uint16_t gFlags           = 0;
static bool     gManualLock      = false;

static uint8_t  gRxBuf[80];

/* ------------------------------------------------------------------------ */
/* Modbus RTU                                                                */
/* ------------------------------------------------------------------------ */
static uint16_t modbusCRC(const uint8_t *buf, uint8_t len) {
  uint16_t crc = 0xFFFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= (uint16_t)buf[i];
    for (uint8_t b = 0; b < 8; b++) {
      if (crc & 1) { crc >>= 1; crc ^= 0xA001; }
      else         { crc >>= 1; }
    }
  }
  return crc;
}

static inline void rs485Transmit() {
#if !RS485_AUTO_DIR
  digitalWrite(RS485_DE_PIN, HIGH);
#endif
}

static inline void rs485Receive() {
#if !RS485_AUTO_DIR
  /* flush() waits for the shift register to empty. Dropping DE before that
   * truncates our own last byte and the slave never sees a valid frame - the
   * single most common RS485 bring-up fault, and it presents as silence rather
   * than as an error. */
  RS485_SERIAL.flush();
  digitalWrite(RS485_DE_PIN, LOW);
#endif
}

/* Read `count` input registers into gRxBuf. Returns bytes of payload, or 0. */
static uint8_t modbusReadInput(uint8_t slave, uint16_t addr, uint16_t count) {
  uint8_t req[8];
  req[0] = slave;
  req[1] = 0x04;                 /* read input registers */
  req[2] = (uint8_t)(addr >> 8);
  req[3] = (uint8_t)(addr & 0xFF);
  req[4] = (uint8_t)(count >> 8);
  req[5] = (uint8_t)(count & 0xFF);
  uint16_t crc = modbusCRC(req, 6);
  req[6] = (uint8_t)(crc & 0xFF);
  req[7] = (uint8_t)(crc >> 8);

  while (RS485_SERIAL.available()) RS485_SERIAL.read();   /* drop stale bytes */

  rs485Transmit();
  RS485_SERIAL.write(req, 8);
  rs485Receive();

  const uint8_t expect = 5 + count * 2;   /* addr, fn, len, data, crc16 */
  uint8_t got = 0;
  uint32_t deadline = millis() + MODBUS_TIMEOUT_MS;
  while (got < expect && (int32_t)(millis() - deadline) < 0) {
    if (RS485_SERIAL.available()) {
      uint8_t b = (uint8_t)RS485_SERIAL.read();
      if (got < sizeof(gRxBuf)) gRxBuf[got++] = b;
      /* An exception response is 5 bytes and would otherwise sit here until
       * the timeout, turning a clear error into an apparent dead bus. */
      if (got == 2 && (gRxBuf[1] & 0x80)) {
        while (got < 5 && (int32_t)(millis() - deadline) < 0) {
          if (RS485_SERIAL.available()) gRxBuf[got++] = (uint8_t)RS485_SERIAL.read();
        }
        return 0;
      }
    }
  }

  if (got < expect)              return 0;
  if (gRxBuf[0] != slave)        return 0;
  if (gRxBuf[1] != 0x04)         return 0;
  if (gRxBuf[2] != count * 2)    return 0;

  uint16_t want = modbusCRC(gRxBuf, expect - 2);
  uint16_t have = (uint16_t)gRxBuf[expect - 2] | ((uint16_t)gRxBuf[expect - 1] << 8);
  if (want != have)              return 0;

  return count * 2;
}

/* Two big-endian registers -> float. Matches decode_float() in the Python
 * meter module; if a unit turns out to be word-swapped, both sides change. */
static float decodeFloat(const uint8_t *p) {
  uint32_t raw = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
                 ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
  float f;
  memcpy(&f, &raw, sizeof(f));
  return f;
}

static void pollMeter() {
  for (uint8_t attempt = 0; attempt <= MODBUS_RETRIES; attempt++) {
    uint8_t payload = modbusReadInput(METER_SLAVE_ID, METER_BLOCK_START,
                                      METER_BLOCK_LEN);
    if (payload) {
      for (uint8_t i = 0; i < 17; i++) gMeter[i] = decodeFloat(&gRxBuf[3 + i * 4]);
      gMeterStampMs = millis();
      gMeterValid   = true;
      gModbusReads++;
      gFlags &= ~FLAG_METER_FAULT;
      return;
    }
    delay(20 * (attempt + 1));
  }
  gModbusErrors++;
  /* One dropped frame next to a switching contactor is routine. Only a run of
   * them means the bus is actually down. */
  if (millis() - gMeterStampMs > 5000UL) {
    gMeterValid = false;
    gFlags |= FLAG_METER_FAULT;
  }
}

/* ------------------------------------------------------------------------ */
/* Contactor interlock                                                       */
/* ------------------------------------------------------------------------ */
static void driveContactor(bool closed) {
#if CONTACTOR_ACTIVE_LOW
  digitalWrite(CONTACTOR_PIN, closed ? LOW : HIGH);
#else
  digitalWrite(CONTACTOR_PIN, closed ? HIGH : LOW);
#endif
}

static bool interlockPermits(bool wantClosed) {
  uint32_t now = millis();
  gFlags &= (uint16_t)~(FLAG_DWELL_BLOCK | FLAG_RATE_BLOCK);

  if (gManualLock) { gFlags |= FLAG_MANUAL_LOCK; return false; }
  gFlags &= (uint16_t)~FLAG_MANUAL_LOCK;

  if (wantClosed == gContactorClosed) return false;   /* nothing to do */

  uint32_t dwell = now - gLastSwitchMs;
  if (gContactorClosed && dwell < MIN_ON_MS)  { gFlags |= FLAG_DWELL_BLOCK; return false; }
  if (!gContactorClosed && dwell < MIN_OFF_MS){ gFlags |= FLAG_DWELL_BLOCK; return false; }

  if (now - gSwitchWindowMs >= 3600000UL) { gSwitchWindowMs = now; gSwitchCount = 0; }
  if (gSwitchCount >= MAX_SWITCHES_PER_HOUR) { gFlags |= FLAG_RATE_BLOCK; return false; }

  return true;
}

static bool applyContactor(bool wantClosed) {
  if (!interlockPermits(wantClosed)) return false;
  gContactorClosed = wantClosed;
  gLastSwitchMs    = millis();
  gSwitchCount++;
  driveContactor(wantClosed);
  Monitor.print("contactor -> ");
  Monitor.println(wantClosed ? "CLOSED" : "OPEN");
  return true;
}

/* ------------------------------------------------------------------------ */
/* Bridge surface - Linux always initiates                                   */
/* ------------------------------------------------------------------------ */

/* Order must match FIELD_ORDER then STATUS_ORDER in transport/bridge.py.
 * Changing one side alone silently scrambles every reading rather than
 * failing, which is why both files name the contract explicitly. */
static float gReply[15];

static float *readMeter() {
  gLastMpuCallMs = millis();

  gReply[0]  = gMeter[4];    /* 30009 active power      W    */
  gReply[1]  = gMeter[5];    /* 30011 reactive power    VAr  */
  gReply[2]  = gMeter[6];    /* 30013 apparent power    VA   */
  gReply[3]  = gMeter[7];    /* 30015 voltage           V    */
  gReply[4]  = gMeter[8];    /* 30017 current           A    */
  gReply[5]  = gMeter[9];    /* 30019 power factor           */
  gReply[6]  = gMeter[10];   /* 30021 frequency         Hz   */
  gReply[7]  = gMeter[11];   /* 30023 import energy     kWh  */
  gReply[8]  = gMeter[12];   /* 30025 export energy     kWh  */
  gReply[9]  = gMeter[0];    /* 30001 total energy      kWh  */
  gReply[10] = gMeter[15];   /* 30031 max demand        W    */

  gReply[11] = gMeterValid ? (float)(millis() - gMeterStampMs) : 1.0e9f;
  gReply[12] = (float)gModbusErrors;
  gReply[13] = gContactorClosed ? 1.0f : 0.0f;
  gReply[14] = (float)gFlags;
  return gReply;
}

static bool setContactor(int closed) {
  gLastMpuCallMs = millis();
  return applyContactor(closed != 0);
}

static float *readStatus() {
  gLastMpuCallMs = millis();
  static float s[4];
  s[0] = gMeterValid ? (float)(millis() - gMeterStampMs) : 1.0e9f;
  s[1] = (float)gModbusErrors;
  s[2] = gContactorClosed ? 1.0f : 0.0f;
  s[3] = (float)gFlags;
  return s;
}

static bool setManualLock(int engaged) {
  gLastMpuCallMs = millis();
  gManualLock = (engaged != 0);
  return gManualLock;
}

/* ------------------------------------------------------------------------ */
void setup() {
  pinMode(CONTACTOR_PIN, OUTPUT);
  driveContactor(true);          /* fail-safe: energised before anything else */
  pinMode(STATUS_LED, OUTPUT);

#if !RS485_AUTO_DIR
  pinMode(RS485_DE_PIN, OUTPUT);
  digitalWrite(RS485_DE_PIN, LOW);
#endif

  RS485_SERIAL.begin(RS485_BAUD, SERIAL_8N1);

  Bridge.begin();
  Monitor.begin();

  /* provide_safe defers execution to the main loop instead of running inside
   * the RPC callback, so a Modbus transaction can never block the bridge. */
  Bridge.provide_safe("read_meter",      readMeter);
  Bridge.provide_safe("set_contactor",   setContactor);
  Bridge.provide_safe("read_status",     readStatus);
  Bridge.provide_safe("set_manual_lock", setManualLock);

  gSwitchWindowMs = millis();
  gLastSwitchMs   = millis();
  gLastMpuCallMs  = millis();

  Monitor.println("Udyog IQ MCU ready - Modbus master, contactor interlock");
}

void loop() {
  static uint32_t lastPoll = 0;
  static uint32_t lastBlink = 0;
  uint32_t now = millis();

  if (now - lastPoll >= POLL_INTERVAL_MS) {
    lastPoll = now;
    pollMeter();
  }

  /* If Linux has gone quiet, restore the contactor. An operator must not come
   * back to a plant left off because a Python process died overnight. */
  if (now - gLastMpuCallMs > MPU_SILENCE_TIMEOUT_MS) {
    gFlags |= FLAG_MPU_SILENT;
    if (!gContactorClosed && !gManualLock) {
      Monitor.println("MPU silent - restoring contactor");
      gContactorClosed = true;
      gLastSwitchMs    = now;
      driveContactor(true);
    }
  } else {
    gFlags &= (uint16_t)~FLAG_MPU_SILENT;
  }

  /* Heartbeat: steady when healthy, fast when the meter is not answering, so
   * the board can be diagnosed from across a workshop without a laptop. */
  uint32_t period = gMeterValid ? 1000 : 150;
  if (now - lastBlink >= period) {
    lastBlink = now;
    digitalWrite(STATUS_LED, !digitalRead(STATUS_LED));
  }
}
