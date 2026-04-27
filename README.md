# Backpack Tilt Monitor (Flask Backend)

This project runs your monitor UI with a Flask backend that talks to Arduino over serial.

## What changed

- Frontend is now served by Flask (`templates/index.html`)
- Real-time updates use Server-Sent Events (`/api/stream`)
- Device commands use REST (`/api/command`)
- Weekly history is persisted on server (`data/weekly_history.json`)

## Setup

1. Create and activate a virtual environment (optional but recommended).
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run server:

```bash
python app.py
```

Default port is `8000` (to avoid macOS port 5000 conflicts).

Open:

- http://127.0.0.1:8000

## Serial usage

- Connect Arduino to your Mac by USB.
- Upload the updated sketch in [Arduino_code/Arduino_code.ino](Arduino_code/Arduino_code.ino) so `DATA`, `STATUS`, and `ALERT` are mirrored to USB serial.
- In the web page, select the serial port (for example `/dev/cu.usbmodem...`) and click Connect.
- Buttons send commands: `CAL`, `STATUS`, `RESET`.

## API endpoints

- `GET /api/state`
- `GET /api/ports`
- `POST /api/connect` body: `{ "port": "/dev/cu.usbmodem..." }`
- `POST /api/disconnect`
- `POST /api/command` body: `{ "command": "CAL|STATUS|RESET" }`
- `POST /api/history/clear`
- `GET /api/stream`
