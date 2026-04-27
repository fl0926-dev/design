# Backpack Tilt Monitor — Website Code

## Project Overview

**IB MYP Design, Grade 10 — Chadwick International School**
**Unit 2: Inventing with Arduino — Criterion C (Creating the Product)**
**Team:** Freddy Lee, Colby Drolet, Aidan Kim
**Due Date:** Tuesday, May 12, 2026 (start of class)

This folder contains the Flask web dashboard that is the software component of our Backpack Tilt Alert Device. The device attaches to a student's backpack shoulder strap, uses an MPU-6050 accelerometer to detect tilt, and alerts the user via LED and buzzer/vibration motor when tilt exceeds 15° for 2+ seconds. The website displays live sensor data, alert history, and weekly statistics received from the Arduino.

---

## Hardware System

### Components (from Design Specification)
| Component      | Quantity | Purpose |
|----------------|----------|---------|
| Arduino Uno    | 1        | Main microcontroller |
| MPU-6050       | 1        | Tilt/accelerometer sensor (I2C: A4/A5) |
| HM-10          | 1        | Bluetooth LE module (SoftwareSerial: D2/D3) |
| ROB-08449      | 1        | Vibration motor (D10) |
| Piezo buzzer   | 1        | Audio alert (D9) |
| LED (alert)    | 1        | Flashing alert indicator (D5) |
| LED (status)   | 1        | Device on/off indicator (D6) |
| Toggle switch  | 1        | Buzzer vs. vibration mode (D7) |
| Push button    | 1        | Recalibrate zero position (D8) |
| 9V Battery     | 1        | Power (safe range: 7–9V per spec) |
| Breadboard     | 1        | Component wiring |
| Resistors      | 3        | LED current limiting |

### Alert Logic (firmware)
- Tilt angle calculated via `atan2(lateral, vertical)` from raw I2C accelerometer registers
- Alert fires when tilt > 15° sustained for **2 seconds** (prevents false alerts during walking)
- Calibration: averages 50 readings as the "zero" reference; button D8 triggers recalibration
- Toggle switch D7: HIGH = piezo buzzer, LOW = vibration motor

---

## Serial Communication Protocol

Arduino sends over USB serial **and** mirrors to HM-10 Bluetooth (9600 baud):

| Message Format | Meaning |
|---|---|
| `DATA:<tilt>,<alertActive>,<alertCount>,<totalAlertMs>` | Live sensor update (~10 Hz) |
| `ALERT:<peakAngle>,<durationMs>` | One alert event just ended |
| `STATUS:<tilt>,<alertActive>,<alertCount>,<totalAlertMs>` | Response to STATUS command |
| `CAL:OK` | Calibration completed |
| `RESET:OK` | Counters reset |
| `INIT:OK` | Device booted |

Flask sends to Arduino (commands):
- `CAL\n` — recalibrate
- `RESET\n` — reset session counters
- `STATUS\n` — request a status dump

---

## Software Architecture

### Backend: `app.py` (Flask, 409 lines)

**Key design decisions:**
- Runs on **port 8000** (avoids macOS port 5000 conflict)
- Uses **Server-Sent Events (SSE)** at `/api/stream` — not WebSockets — for real-time push
- A **background thread** (`reader_loop`) reads serial data continuously
- All shared state protected by `threading.Lock()`
- Each browser tab gets its own `queue.Queue` in the `subscribers` list
- Weekly alert history persists to `data/weekly_history.json` across server restarts

**API endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Serve dashboard HTML |
| GET | `/api/state` | Full state snapshot (JSON) |
| GET | `/api/ports` | List available serial ports |
| POST | `/api/connect` | Connect to Arduino |
| POST | `/api/disconnect` | Disconnect |
| POST | `/api/command` | Send CAL / RESET / STATUS |
| POST | `/api/history/clear` | Erase alert history |
| GET | `/api/stream` | SSE real-time event stream |

**SSE event types the browser receives:**
- `snapshot` — full state dump on first connection
- `live` — tilt + alert status (every DATA line)
- `connection` — connected/disconnected status change
- `history` — alert events + weekly totals updated
- `ack` — Arduino acknowledgement (CAL:OK etc.)
- `log` — communication log line
- `keepalive` — sent every 20 s to prevent timeout

### Frontend: `templates/index.html` (580 lines)

Single-file HTML/CSS/JS dashboard. All styles and scripts are embedded (no external dependencies).

**UI sections:**
1. **Header** — title, connection dot, port dropdown, Connect button
2. **Live data cards (3-col grid)** — tilt gauge (SVG arc), alert badge, session stats
3. **Device controls** — Recalibrate, Status, Reset, Clear History buttons
4. **History row (2-col grid)** — alert event log table, weekly bar chart
5. **Communication log** — raw serial traffic

**Colour palette (CSS variables):**
```css
--bg:      #0f1117   /* dark navy page background */
--surface: #1a1d27   /* card background */
--accent:  #5b8af5   /* blue — buttons, gauge fill */
--green:   #34d399   /* posture OK */
--red:     #f87171   /* alert active */
--amber:   #fbbf24   /* warning */
```

**Real-time flow:** `init()` → fetches `/api/state` → `startStream()` opens EventSource → handles `snapshot`, `live`, `history`, `connection`, `ack`, `log` events.

### Firmware: `Arduino_code/Arduino_code.ino` (368 lines)

Raw I2C register manipulation for MPU-6050 (no external library). All `DATA` and `ALERT` messages sent to **both** USB serial (website) and HM-10 BLE (phone apps like nRF Connect).

---

## How to Run

```bash
# 1. Activate virtual environment
source .venv/bin/activate       # macOS/Linux
# .venv\Scripts\activate        # Windows

# 2. Install dependencies
pip install -r requirements.txt  # Flask==3.1.0, pyserial==3.5

# 3. Upload Arduino_code/Arduino_code.ino to the Arduino Uno

# 4. Start the server
python app.py                    # → http://localhost:8000

# 5. Open http://localhost:8000 in a browser
# 6. Select the Arduino's serial port from the dropdown and click Connect
```

The server binds to `0.0.0.0` so it is reachable from other devices on the same Wi-Fi network at `http://<your-mac-ip>:8000`.

---

## Planned Improvements (Not Yet Implemented)

These are the next features to be added. **Do not implement until instructed.**

### 1. UI Beautification
The current UI is functional but visually plain. Goals:
- Make it look more polished and modern without breaking existing functionality
- Improve typography, spacing, visual hierarchy
- Keep the existing dark theme and colour variables
- Target audience: middle/upper school students — modern, minimal, tech-inspired style

### 2. Phone Access (Mobile Browser Support)
When the Flask server is running, phones on the same Wi-Fi should be able to open the dashboard.
- The server already binds to `0.0.0.0` so it is network-accessible
- Need to ensure the UI is fully responsive on small screens
- May need to display the local network URL prominently

### 3. Bluetooth Connection in Port List + Live Sync
When the phone has connected to the Arduino's HM-10 module via nRF Connect (or similar BLE app), the HM-10 creates a virtual serial port on the Mac.
- The `/api/ports` endpoint already returns all `serial.tools.list_ports.comports()` entries — BLE virtual ports should appear there automatically once the phone establishes the BLE connection and the Mac's Bluetooth stack registers a serial port
- Goal: user selects this Bluetooth port from the dropdown, clicks Connect, and live data streams exactly as it does over USB
- The HM-10 mirrors the identical `DATA:` / `ALERT:` protocol, so no protocol changes are needed
- Key challenge: BLE ports may not enumerate on macOS the same way as USB — may need to check `/dev/tty.HM*` or `/dev/tty.Bluetooth*` patterns

---

## Academic Context (IB MYP Criterion C)

This website is the **software product** for Criterion C: Creating the Product. The rubric requires:

- **S1 (Project Plan):** Complete, detailed step-by-step plan with dates, tools, team responsibilities — must be finished *before* production starts
- **S2 (Technical Skills):** Demonstrate competent use of Flask, Python, HTML/CSS/JS; document in design diary with screenshots
- **S3 (Creating the Solution):** Final product must match Criterion B technical diagrams and function correctly
- **S4 (Changes to Plan):** Any deviations from the plan must be described (what changed), justified (why it was necessary), and explained (how it improved the solution)

**Teacher patterns to follow (from feedback analysis):**
- Evidence must be cited *inline*, not dumped at the end
- Avoid sweeping generalizations — link every claim to specific evidence
- Design diary entries: Date + Task + Tools Used + How skills were used + Screenshot evidence
- Change descriptions must explain what changed, how, and why — in enough detail for someone else to replicate

**Design Specification constraints relevant to the website:**
- Must support data transfer via Bluetooth connection with HM-10 (from Function spec)
- Summarizes weekly alerting history — fulfils the spec requirement for tracking posture over the school week
