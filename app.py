"""
app.py — Flask backend for the Backpack Tilt Monitor
=====================================================
HOW IT WORKS:
  1. The Arduino sends sensor data through USB serial (9600 baud).
  2. A background thread reads each line from the serial port.
  3. When the website opens, it connects to /api/stream.
  4. Flask pushes live updates to the website in real time.

HOW TO RUN:
  1. Plug in the Arduino.
  2. Run: python app.py
  3. Open: http://localhost:8000
"""
git init
git add .
git commit -m "123"
git branch -M main
git remote add origin https://github.com/fl0926-dev/design.git
git push -u origin main


import glob
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from serial import Serial, SerialException
from serial.tools import list_ports

# ── Settings ──────────────────────────────────────────────────────
BAUD_RATE  = 9600    # Must match Arduino's Serial.begin(9600)
FLASK_PORT = 8000    # The website will be at http://localhost:8000

# ── File paths for saved history ──────────────────────────────────
BASE_DIR     = Path(__file__).resolve().parent
DATA_DIR     = BASE_DIR / "data"
HISTORY_PATH = DATA_DIR / "weekly_history.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUGGING=True
# ── Flask app ─────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")


def classify_error_code(exc):
    """Map Python exceptions to short debug classification codes."""
    if isinstance(exc, SerialException):
        return "E_SERIAL"
    if isinstance(exc, json.JSONDecodeError):
        return "E_JSON"
    if isinstance(exc, (OSError, IOError)):
        return "E_IO"
    if isinstance(exc, (ConnectionError, socket.error)):
        return "E_NETWORK"
    if isinstance(exc, ValueError):
        return "E_VALUE"
    if isinstance(exc, KeyError):
        return "E_KEY"
    if isinstance(exc, TypeError):
        return "E_TYPE"
    if isinstance(exc, RuntimeError):
        return "E_RUNTIME"
    return "E_UNKNOWN"


def debug_log_error(exc, location, context=None):
    """Print detailed error info only when DEBUGGING is enabled."""
    if not DEBUGGING:
        return
    code = classify_error_code(exc)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [DEBUG-ERROR:{code}] {location}")
    print(f"  Type: {exc.__class__.__name__}")
    print(f"  Message: {exc}")
    if context is not None:
        print(f"  Context: {context}")
    print("  Traceback:")
    traceback.print_exc()


def _threading_excepthook(args):
    """Capture uncaught exceptions from background threads."""
    debug_log_error(
        args.exc_value,
        location=f"thread:{getattr(args.thread, 'name', 'unknown')}",
        context={"exc_type": getattr(args.exc_type, "__name__", str(args.exc_type))},
    )


def _sys_excepthook(exc_type, exc_value, exc_tb):
    """Capture uncaught exceptions from the main thread."""
    if DEBUGGING:
        code = classify_error_code(exc_value)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] [DEBUG-ERROR:{code}] main-thread")
        print(f"  Type: {exc_type.__name__}")
        print(f"  Message: {exc_value}")
        print("  Traceback:")
        traceback.print_exception(exc_type, exc_value, exc_tb)
        return
    sys.__excepthook__(exc_type, exc_value, exc_tb)


threading.excepthook = _threading_excepthook
sys.excepthook = _sys_excepthook


def get_local_ip():
    """Return the machine's LAN IP so phones on the same Wi-Fi can connect."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def get_all_ports():
    """Return serial ports including BLE virtual ports that pyserial may miss."""
    ports = [{"device": p.device, "description": p.description}
             for p in list_ports.comports()]
    seen = {p["device"] for p in ports}
    for pattern in ["/dev/tty.HM*", "/dev/cu.HM*",
                    "/dev/tty.Bluetooth*", "/dev/cu.Bluetooth*"]:
        for path in glob.glob(pattern):
            if path not in seen:
                ports.append({"device": path, "description": "Bluetooth Serial (HM-10)"})
                seen.add(path)
    return ports

# ─────────────────────────────────────────────────────────────────
#  SHARED STATE
#  These variables are read by Flask routes AND the serial thread,
#  so they need a lock to prevent race conditions.
# ─────────────────────────────────────────────────────────────────
serial_conn     = None    # The open Serial object (or None)
is_connected    = False   # True when Arduino is plugged in and open
session_start   = None    # When this session started (milliseconds)

last_tilt       = 0.0    # Most recent tilt angle (degrees)
alert_active    = False  # Whether an alert is happening right now
alert_count     = 0      # How many alerts fired this session
total_alert_ms  = 0      # Total milliseconds spent in alert

alert_events    = []     # List of past alert events — newest first, max 200
log_messages    = []     # Communication log lines, max 300

# weekly_history stores alert counts per calendar day:
#   { "2026-04-10": { "count": 5, "totalMs": 12000 }, ... }
weekly_history  = {}

# Each browser tab that loads the page gets its own queue.
# Flask pushes updates into these queues, and the SSE stream
# sends them to the browser.
subscribers = []
lock        = threading.Lock()   # Protects all shared variables above
stop_flag   = threading.Event()  # Set to True to stop the reader thread


# ─────────────────────────────────────────────────────────────────
#  HISTORY FILE  (saves data across server restarts)
# ─────────────────────────────────────────────────────────────────

def load_history():
    """Read weekly_history.json from disk. Returns {} if not found."""
    try:
        with HISTORY_PATH.open("r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        debug_log_error(e, "load_history", {"path": str(HISTORY_PATH)})
        return {}

def save_history():
    """Write weekly_history to disk so data survives a server restart."""
    try:
        with HISTORY_PATH.open("w") as f:
            json.dump(weekly_history, f, indent=2)
    except OSError as e:
        debug_log_error(e, "save_history", {"path": str(HISTORY_PATH)})
        raise

weekly_history = load_history()


# ─────────────────────────────────────────────────────────────────
#  BROADCAST  (push updates to all open browser tabs)
# ─────────────────────────────────────────────────────────────────

def broadcast(event_type, payload):
    """Put an event into every subscriber's queue."""
    with lock:
        subs = list(subscribers)
    for q in subs:
        try:
            q.put_nowait((event_type, payload))
        except queue.Full:
            pass  # If a tab is too slow, drop the update

def add_log(text):
    """Add a timestamped line to the comm log and broadcast it."""
    line = f"{datetime.now().strftime('%H:%M:%S')}  {text}"
    log_messages.append(line)
    if len(log_messages) > 300:
        log_messages.pop(0)
    broadcast("log", {"text": line})


# ─────────────────────────────────────────────────────────────────
#  SERIAL READER THREAD
#  Runs in the background. Reads lines from the Arduino and calls
#  parse_line() for each complete message.
# ─────────────────────────────────────────────────────────────────

def reader_loop():
    """Background thread: reads from serial and parses each line."""
    buffer = ""
    while not stop_flag.is_set():
        with lock:
            ser = serial_conn
        if ser is None:
            time.sleep(0.1)
            continue
        try:
            raw = ser.read(128)  # Read up to 128 bytes (non-blocking with timeout)
        except SerialException as e:
            debug_log_error(e, "reader_loop:ser.read")
            add_log(f"Serial error: {e}")
            do_disconnect()
            return
        if not raw:
            continue
        # Decode bytes to text and accumulate in a buffer
        buffer += raw.decode("utf-8", errors="ignore")
        # Process every complete line (ending with \n)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if line:
                parse_line(line)


# ─────────────────────────────────────────────────────────────────
#  MESSAGE PARSER
#  Called for every complete line received from the Arduino.
#
#  Arduino sends these formats (defined in Arduino_code.ino):
#    DATA:<tilt>,<alertActive 0|1>,<alertCount>,<totalAlertMs>
#    ALERT:<angle>,<durationMs>
#    STATUS:<tilt>,<alertActive>,<alertCount>,<totalAlertMs>
#    CAL:OK | RESET:OK | INIT:OK
# ─────────────────────────────────────────────────────────────────

def parse_line(line):
    global last_tilt, alert_active, alert_count, total_alert_ms
    add_log(f"RX  {line}")

    # DATA and STATUS have the same 4-field format
    if line.startswith("DATA:") or line.startswith("STATUS:"):
        parts = line.split(":", 1)[1].split(",")   # Split after the colon, then by comma
        if len(parts) == 4:
            try:
                last_tilt      = round(float(parts[0]), 1)
                alert_active   = int(parts[1]) == 1
                alert_count    = int(parts[2])
                total_alert_ms = int(parts[3])
                broadcast("live", live_payload())
            except ValueError as e:
                debug_log_error(e, "parse_line:DATA/STATUS", {"line": line})
                pass  # Ignore malformed lines
        return

    # ALERT signals that one tilt event just ended
    if line.startswith("ALERT:"):
        parts = line[6:].split(",")
        if len(parts) == 2:
            try:
                record_alert(float(parts[0]), int(parts[1]))
            except ValueError as e:
                debug_log_error(e, "parse_line:ALERT", {"line": line})
                pass
        return

    # Simple acknowledgements — forward straight to the browser
    if line in {"INIT:OK", "CAL:OK", "RESET:OK"}:
        broadcast("ack", {"message": line})


def record_alert(angle, duration_ms):
    """Save a completed alert event to memory and disk."""
    event = {
        "time":        datetime.now().strftime("%H:%M:%S"),
        "angle":       round(angle, 1),
        "duration_ms": duration_ms,
    }
    alert_events.insert(0, event)  # Newest at the top
    if len(alert_events) > 200:
        alert_events.pop()

    # Update today's count in weekly history
    today = datetime.now().date().isoformat()
    day = weekly_history.get(today, {"count": 0, "totalMs": 0})
    day["count"]   += 1
    day["totalMs"] += duration_ms
    weekly_history[today] = day
    save_history()

    broadcast("history", {"weekly": weekly_history, "alerts": alert_events})


# ─────────────────────────────────────────────────────────────────
#  CONNECTION HELPERS
# ─────────────────────────────────────────────────────────────────

def do_connect(port=None, baudrate=None):
    """Open the serial port and start the reader thread."""
    global serial_conn, is_connected, session_start
    if is_connected:
        return True, "already connected"

    chosen_port = (port or "").strip()
    chosen_baud = int(baudrate or BAUD_RATE)

    # Auto-pick the port if the user didn't specify and there's only one
    if not chosen_port:
        available = [p.device for p in list_ports.comports()]
        if len(available) == 1:
            chosen_port = available[0]
        else:
            return False, "please select a serial port"

    try:
        ser = Serial(chosen_port, chosen_baud, timeout=0.2)
    except SerialException as e:
        debug_log_error(
            e,
            "do_connect:Serial",
            {"port": chosen_port, "baudrate": chosen_baud},
        )
        return False, str(e)

    with lock:
        serial_conn  = ser
        is_connected = True
        session_start = int(time.time() * 1000)
        stop_flag.clear()

    threading.Thread(target=reader_loop, daemon=True).start()
    add_log(f"Connected: {chosen_port} @ {chosen_baud}")
    broadcast("connection", connection_payload())
    return True, "connected"


def do_disconnect():
    """Close the serial port and stop the reader thread."""
    global serial_conn, is_connected
    with lock:
        ser          = serial_conn
        serial_conn  = None
        is_connected = False
        stop_flag.set()

    if ser:
        try:
            ser.close()
        except SerialException:
            pass

    add_log("Disconnected")
    broadcast("connection", connection_payload())


def do_send(command):
    """Send a command string to the Arduino over serial."""
    cmd = command.strip().upper()
    if cmd not in {"CAL", "RESET", "STATUS"}:
        return False, "unknown command"
    if not is_connected or serial_conn is None:
        return False, "not connected"
    try:
        # CAL is sent 3× with 150 ms gaps: SoftwareSerial on the Arduino drops
        # incoming bytes during its ~26 ms btSerial.println() window. Retrying
        # across multiple DATA cycles reduces the miss rate from ~26% to ~2%.
        repeat = 3 if cmd == "CAL" else 1
        for i in range(repeat):
            serial_conn.write((cmd + "\n").encode("utf-8"))
            if i < repeat - 1:
                time.sleep(0.15)
        add_log(f"TX  {cmd}")
        return True, "sent"
    except SerialException as e:
        debug_log_error(e, "do_send:serial_conn.write", {"command": cmd})
        do_disconnect()
        return False, str(e)


# ─────────────────────────────────────────────────────────────────
#  PAYLOAD BUILDERS  (format state as JSON-friendly dicts)
# ─────────────────────────────────────────────────────────────────

def connection_payload():
    return {
        "connected":        is_connected,
        "port":             (serial_conn.port if serial_conn else ""),
        "session_start_ms": session_start,
    }

def live_payload():
    # print("Building live payload with tilt =", last_tilt, "alert_active =", alert_active)
    return {
        "tilt":           last_tilt,
        "alert_active":   alert_active,
        "alert_count":    alert_count,
        "total_alert_ms": total_alert_ms,
    }

def snapshot():
    """Return the full current state (sent to a newly connected browser tab)."""
    today = datetime.now().date()
    weekly = {}
    for i in range(7):
        d   = today - timedelta(days=(6 - i))
        key = d.isoformat()
        weekly[key] = weekly_history.get(key, {"count": 0, "totalMs": 0})
    return {
        "connection": connection_payload(),
        "live":       live_payload(),
        "alerts":     alert_events,
        "weekly":     weekly,
        "logs":       log_messages[-100:],
        "ports":      get_all_ports(),
    }


# ─────────────────────────────────────────────────────────────────
#  FLASK ROUTES  (the URL endpoints the website talks to)
# ─────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    """Serve the main dashboard page."""
    return render_template("index.html")

@app.get("/api/state")
def api_state():
    """Return a full snapshot of the current state as JSON."""
    return jsonify(snapshot())

@app.get("/api/ports")
def api_ports():
    """Return a list of available serial ports including Bluetooth virtual ports."""
    return jsonify({"ports": get_all_ports()})

@app.post("/api/connect")
def api_connect():
    """Connect to the Arduino on the specified serial port."""
    body = request.get_json(silent=True) or {}
    ok, msg = do_connect(body.get("port"), body.get("baudrate"))
    return jsonify({"ok": ok, "message": msg, "connection": connection_payload()}), (200 if ok else 400)

@app.post("/api/disconnect")
def api_disconnect():
    """Disconnect from the Arduino."""
    do_disconnect()
    return jsonify({"ok": True, "connection": connection_payload()})

@app.post("/api/command")
def api_command():
    """Send a command (CAL, RESET, STATUS) to the Arduino."""
    body = request.get_json(silent=True) or {}
    ok, msg = do_send(body.get("command", ""))
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

@app.post("/api/history/clear")
def api_history_clear():
    """Erase all alert history from memory and disk."""
    global weekly_history
    weekly_history = {}
    alert_events.clear()
    save_history()
    broadcast("history", {"weekly": {}, "alerts": []})
    add_log("History cleared")
    return jsonify({"ok": True})

@app.get("/api/stream")
def api_stream():
    """
    Server-Sent Events (SSE) endpoint.
    The browser connects here once and receives real-time updates
    without needing to constantly poll the server.
    """
    q = queue.Queue(maxsize=200)
    with lock:
        subscribers.append(q)

    def generate():
        try:
            # Send the full current state immediately when the browser connects
            yield f"event: snapshot\ndata: {json.dumps(snapshot())}\n\n"
            while True:
                try:
                    event_type, payload = q.get(timeout=20)
                    yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    # Send a keepalive ping every 20 s to prevent timeout
                    yield "event: keepalive\ndata: {}\n\n"
        finally:
            with lock:
                if q in subscribers:
                    subscribers.remove(q)

    return Response(generate(), mimetype="text/event-stream")


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    """Global Flask error handler with debug classification output."""
    debug_log_error(
        e,
        "flask_request",
        {"method": request.method, "path": request.path},
    )
    return jsonify({
        "ok": False,
        "error_code": classify_error_code(e),
        "message": "internal server error",
    }), 500


if __name__ == "__main__":
    lan_ip = get_local_ip()
    print(f"Server running at:")
    print(f"  Local:   http://localhost:{FLASK_PORT}")
    print(f"  Network: http://{lan_ip}:{FLASK_PORT}")
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
