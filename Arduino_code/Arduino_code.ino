/*
 * Backpack Tilt Detection & Alert Device
 * Arduino Uno — Main Code
 * ─────────────────────────────────────────────────────────────
 * WHAT THIS CODE DOES (step by step):
 *   1. On startup, calibrate the MPU-6050 so the current angle = 0°.
 *   2. Every 50 ms, read the tilt angle from the MPU-6050.
 *   3. If the angle stays above TILT_THRESHOLD for 2+ seconds, fire an alert.
 *   4. During an alert: flash LED 1 + activate buzzer OR vibration
 *      depending on the toggle switch position.
 *   5. Every 100 ms, send live data to the website through USB serial.
 *   6. If the recalibration button is pressed, reset the 0° reference.
 *
 * PIN MAP:
 *   A4 (SDA) / A5 (SCL) ── MPU-6050 (I2C)
 *   D2  ── HM-10 TX → Arduino RX
 *   D3  ── HM-10 RX ← Arduino TX
 *   D5  ── LED 1 (alert)
 *   D6  ── LED 2 (device ON/OFF status)
 *   D7  ── Toggle switch  (HIGH = buzzer mode, LOW = vibration mode)
 *   D8  ── Push button    (press to recalibrate)
 *   D9  ── Piezo buzzer
 *   D10 ── Vibration motor (ROB-08449)
 */

#include <Wire.h>           // I2C library — needed to talk to the MPU-6050
#include <SoftwareSerial.h> // Lets us use D2/D3 as a second serial port for HM-10
#include <math.h>           // Needed for sqrt() and atan2()

// ── Pin Numbers ──────────────────────────────────────────────────
#define PIN_BT_RX     2    // HM-10 TX  → Arduino (receives BLE data)
#define PIN_BT_TX     3    // HM-10 RX  ← Arduino (sends BLE data)
#define PIN_LED_1     5    // Alert LED 1
#define PIN_LED_2     6    // Device ON/OFF status LED
#define PIN_TOGGLE    7    // Toggle switch (HIGH = buzzer, LOW = vibration)
#define PIN_BUTTON    8    // Recalibration button (active LOW)
#define PIN_BUZZER    9    // Piezo buzzer
#define PIN_VIBRATION 10   // Vibration motor

// ── MPU-6050 Registers ───────────────────────────────────────────
// These are the internal addresses we write to in order to configure the sensor.
#define MPU_ADDR      0x68   // Default I2C address of the MPU-6050
#define REG_PWR       0x6B   // Power management register (write 0 to wake up)
#define REG_ACCEL_CFG 0x1C   // Accelerometer range config (0x00 = ±2 g)
#define REG_ACCEL_OUT 0x3B   // First accelerometer data register

// ── Settings (change these to tune behaviour) ────────────────────
#define TILT_THRESHOLD    15.0   // Degrees above calibrated zero to trigger alert
#define ALERT_SUSTAIN_MS  2000   // Tilt must last this long (ms) before alert fires
#define LED_FLASH_MS       250   // How fast the LEDs blink during an alert
#define BUZZER_FREQ        100  // Buzzer tone frequency in Hz
#define BT_INTERVAL_MS     100   // How often to send data to the website (ms)
#define CAL_SAMPLES         50   // Number of readings averaged during calibration
#define DEBOUNCE_MS        250   // Prevents button bouncing (ignore re-press within 250 ms)
#define LOOP_DELAY_MS       50   // Main loop runs ~20 times per second

// ── Objects ──────────────────────────────────────────────────────
SoftwareSerial btSerial(PIN_BT_RX, PIN_BT_TX);  // Serial port for the HM-10 BLE module

// ── Calibration ──────────────────────────────────────────────────
// When we calibrate, we store the "resting" accelerometer readings.
// All future angle calculations subtract these offsets so that 0° = upright.
float offsetX = 0.0;
float offsetY = 0.0;

// ── State Variables ──────────────────────────────────────────────
float         currentTilt     = 0.0;    // Most recent tilt angle (degrees)
bool          isTilted        = false;  // True if currently above threshold
bool          alertActive     = false;  // True if the 2-second timer fired
unsigned long tiltStartMs     = 0;      // When the tilt first exceeded threshold
unsigned long currentAlertMs  = 0;      // When the current alert began
bool          ledOn           = false;  // Tracks LED flash state
unsigned long lastFlashMs     = 0;      // Last time the LED toggled
unsigned long lastSendMs      = 0;      // Last time we sent data to website
unsigned long lastButtonMs    = 0;      // For button debounce

// ── Cumulative stats (sent to website) ───────────────────────────
unsigned long alertCount    = 0;   // Total number of alert events this session
unsigned long totalAlertMs  = 0;   // Total milliseconds spent in alert this session


// ════════════════════════════════════════════════════════════════
//  SETUP  — runs once when Arduino powers on
// ════════════════════════════════════════════════════════════════
void setup() {
  Serial.begin(9600);    // USB serial — talks to app.py on the laptop
  btSerial.begin(9600);  // Bluetooth serial — talks to HM-10 module

  Wire.begin();          // Start I2C bus
  initSensor();          // Wake up and configure the MPU-6050

  // Set pin directions
  pinMode(PIN_LED_1,     OUTPUT);
  pinMode(PIN_LED_2,     OUTPUT);
  pinMode(PIN_BUZZER,    OUTPUT);
  pinMode(PIN_VIBRATION, OUTPUT);
  pinMode(PIN_TOGGLE,    INPUT_PULLUP);  // High by default; low when switched
  pinMode(PIN_BUTTON,    INPUT_PULLUP);  // High by default; low when pressed

  allOff();        // Make sure all outputs start off
  delay(1000);     // Give the sensor time to stabilise after power-on
  calibrate();     // Set the current angle as the 0° reference

  Serial.println("INIT:OK");
  btSerial.println("INIT:OK");
}


// ════════════════════════════════════════════════════════════════
//  LOOP  — runs ~20 times per second
// ════════════════════════════════════════════════════════════════
void loop() {
  currentTilt = getTiltAngle();   // Step 1: read current angle
  checkButton();                  // Step 2: check if recalibrate was pressed
  checkTilt();                    // Step 3: manage 2-second sustained alert
  runAlertOutputs();              // Step 4: flash alert LED + sound/vibration
  sendData();                     // Step 5: send live data to website
  delay(LOOP_DELAY_MS);
}


// ════════════════════════════════════════════════════════════════
//  MPU-6050 SENSOR
// ════════════════════════════════════════════════════════════════

// Wake up the MPU-6050 and set it to the most sensitive range (±2 g).
void initSensor() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_PWR);   // Point to power management register
  Wire.write(0x00);      // 0x00 = clear sleep bit = wake up
  Wire.endTransmission(true);

  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_CFG);  // Point to accelerometer config
  Wire.write(0x00);            // 0x00 = ±2 g range (highest sensitivity)
  Wire.endTransmission(true);
}

// Read raw acceleration from the MPU-6050 over I2C.
// The sensor gives 16-bit integers; we convert them to g-units (gravity).
void readAccel(float &ax, float &ay, float &az) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_OUT);    // Tell sensor we want to read from this address
  Wire.endTransmission(false);
  Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)6, (uint8_t)true);  // Read 6 bytes (2 per axis)

  // Each axis is two bytes: high byte first, then low byte
  int16_t rawX = (Wire.read() << 8) | Wire.read();
  int16_t rawY = (Wire.read() << 8) | Wire.read();
  int16_t rawZ = (Wire.read() << 8) | Wire.read();

  // At ±2 g range, 16384 raw units = 1 g
  ax = rawX / 16384.0;
  ay = rawY / 16384.0;
  az = rawZ / 16384.0;
}

// Calculate the tilt angle (0° = upright, 90° = sideways).
// Subtracts calibration offsets, then uses trigonometry.
float getTiltAngle() {
  float ax, ay, az;
  readAccel(ax, ay, az);

  ax -= offsetX;   // Remove the "resting" offset set during calibration
  ay -= offsetY;

  // lateral = how much the device is leaning sideways (X + Y combined)
  float lateral = sqrt(ax * ax + ay * ay);

  // atan2 gives the angle between the lateral lean and vertical (Z axis)
  float angle = atan2(lateral, az) * 180.0 / PI;

  // Keep the result within the 0°–90° spec range
  return constrain(angle, 0.0, 90.0);
}


// ════════════════════════════════════════════════════════════════
//  CALIBRATION
// ════════════════════════════════════════════════════════════════

// Average CAL_SAMPLES readings and store them as offsets.
// After this, getTiltAngle() will read 0° when the device is in this position.
void calibrate() {
  allOff();   // Stop outputs immediately — don't wait until sampling is done
  float sumX = 0.0, sumY = 0.0;
  for (int i = 0; i < CAL_SAMPLES; i++) {
    float ax, ay, az;
    readAccel(ax, ay, az);
    sumX += ax;
    sumY += ay;
    delay(10);
  }
  offsetX = sumX / CAL_SAMPLES;
  offsetY = sumY / CAL_SAMPLES;

  // Reset all alert state so a fresh alert can fire after recalibration
  isTilted    = false;
  alertActive = false;
  tiltStartMs = 0;
  allOff();

  Serial.println("CAL:OK");
  btSerial.println("CAL:OK");
}


// ════════════════════════════════════════════════════════════════
//  RECALIBRATION BUTTON
// ════════════════════════════════════════════════════════════════

// Check if the push button is pressed. Uses debounce to avoid
// false triggers from electrical noise in the button contacts.
void checkButton() {
  if (digitalRead(PIN_BUTTON) == LOW) {            // LOW = pressed (pullup)
    unsigned long now = millis();
    if (now - lastButtonMs > DEBOUNCE_MS) {
      lastButtonMs = now;
      calibrate();
    }
  }
}


// ════════════════════════════════════════════════════════════════
//  TILT DETECTION  — 2-second sustained timer
// ════════════════════════════════════════════════════════════════

void checkTilt() {
  unsigned long now = millis();

  if (currentTilt > TILT_THRESHOLD) {
    // Tilt is above the threshold right now
    if (!isTilted) {
      isTilted    = true;       // Mark that tilt just started
      tiltStartMs = now;        // Start the 2-second timer
    }
    else if (!alertActive && (now - tiltStartMs >= ALERT_SUSTAIN_MS)) {
      // Tilt has been sustained for 2+ seconds → fire the alert
      alertActive      = true;
      currentAlertMs   = now;
      alertCount++;
      Serial.println("ALERT:ON");
    }
  }
  else {
    // Tilt dropped back below the threshold
    if (alertActive) {
      // Record how long the alert lasted and report it to the website
      unsigned long duration = now - currentAlertMs;
      totalAlertMs += duration;
      sendAlertEvent(currentTilt, duration);
      Serial.println("ALERT:OFF");
    }
    isTilted    = false;
    alertActive = false;
    tiltStartMs = 0;
    allOff();
  }
}


// ════════════════════════════════════════════════════════════════
//  ALERT OUTPUTS  — LED 1 + buzzer or vibration
// ════════════════════════════════════════════════════════════════

void runAlertOutputs() {
  if (!alertActive) return;

  // Flash alert LED (LED 1) at LED_FLASH_MS interval
  unsigned long now = millis();
  if (now - lastFlashMs >= LED_FLASH_MS) {
    lastFlashMs = now;
    ledOn = !ledOn;   // Toggle between on and off
    digitalWrite(PIN_LED_1, ledOn);
  }

  // Toggle switch chooses the secondary alert:
  // HIGH (switch up) = buzzer,  LOW (switch down) = vibration
  if (digitalRead(PIN_TOGGLE) == HIGH) {
    tone(PIN_BUZZER, BUZZER_FREQ);    // Play a continuous tone
    digitalWrite(PIN_VIBRATION, LOW);
  } else {
    noTone(PIN_BUZZER);
    digitalWrite(PIN_VIBRATION, HIGH);
  }
}

// Turn off alert outputs; keep status LED on while running.
void allOff() {
  digitalWrite(PIN_LED_1,     LOW);
  digitalWrite(PIN_LED_2,     HIGH); // Keep status LED on while device is running
  noTone(PIN_BUZZER);
  digitalWrite(PIN_VIBRATION, LOW);
  ledOn = false;
}


// ════════════════════════════════════════════════════════════════
//  BLUETOOTH / SERIAL DATA  — sends data to app.py
// ════════════════════════════════════════════════════════════════

// Periodically broadcast live sensor data AND listen for commands.
void sendData() {
  unsigned long now = millis();

  // Send a status update every BT_INTERVAL_MS milliseconds
  if (now - lastSendMs >= BT_INTERVAL_MS) {
    lastSendMs = now;

    // Format: DATA:<tilt>,<alertActive 0|1>,<alertCount>,<totalAlertMs>
    // app.py splits this on commas to update the website dashboard.
    String msg = "DATA:";
    msg += String(currentTilt, 1);  msg += ",";
    msg += (alertActive ? 1 : 0);   msg += ",";
    msg += alertCount;               msg += ",";
    msg += totalAlertMs;

    Serial.println(msg);    // → app.py (USB)
    btSerial.println(msg);  // → HM-10 (Bluetooth)
  }

  // Listen for incoming commands on both USB serial and BLE
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    handleCommand(cmd);
  }
  if (btSerial.available()) {
    String cmd = btSerial.readStringUntil('\n');
    cmd.trim();
    handleCommand(cmd);
  }
}

// Respond to a command sent by the website via app.py.
void handleCommand(const String &cmd) {
  if (cmd == "RESET") {
    alertCount   = 0;
    totalAlertMs = 0;
    Serial.println("RESET:OK");
    btSerial.println("RESET:OK");
  }
  else if (cmd == "CAL") {
    calibrate();
  }
  else if (cmd == "STATUS") {
    // Same format as DATA — lets the website request an on-demand update
    String msg = "STATUS:";
    msg += String(currentTilt, 1);  msg += ",";
    msg += (alertActive ? 1 : 0);   msg += ",";
    msg += alertCount;               msg += ",";
    msg += totalAlertMs;
    Serial.println(msg);
    btSerial.println(msg);
  }
}

// Send a one-shot ALERT message when a tilt event ends.
// The website logs this as one row in the Alert Event Log table.
void sendAlertEvent(float angle, unsigned long durationMs) {
  // Format: ALERT:<angle>,<durationMs>
  String msg = "ALERT:";
  msg += String(angle, 1);  msg += ",";
  msg += durationMs;
  Serial.println(msg);
  btSerial.println(msg);
}
