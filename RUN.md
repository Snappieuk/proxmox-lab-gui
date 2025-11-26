# How to Run Proxmox Lab GUI

## Simple Method (Recommended)

Just run:
```bash
./start.sh
```

Or manually:
```bash
cd rdp-gen
python3 app.py
```

The server will start on `http://0.0.0.0:8080`

## What This Does

- Uses Flask's built-in threaded server
- Handles multiple concurrent requests
- No Gunicorn/SSL fork issues
- Perfect for lab/internal use

## Production Notes

Flask's built-in server with `threaded=True` is fine for:
- Internal lab environments
- Small to medium user counts (< 100 concurrent users)
- Your specific use case (lab VM portal)

If you need more performance later, you can switch back to Gunicorn with proper SSL configuration.

## Systemd Service

Update `proxmox-gui.service`:

```ini
[Service]
ExecStart=/path/to/proxmox-lab-gui/start.sh
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl restart proxmox-gui
```
