# How to Run Proxmox Lab GUI

## Simple Method (Recommended)

Just run:
```bash
./start.sh
```

Or manually using the new modular entry point:
```bash
python3 run.py
```

The server will start on `http://0.0.0.0:8080`

## What This Does

- Uses Flask's built-in threaded server
- Handles multiple concurrent requests
- Perfect for lab/internal use

## Production Notes

Flask's built-in server with `threaded=True` is fine for:
- Internal lab environments
- Small to medium user counts (< 100 concurrent users)
- Your specific use case (lab VM portal)

If you need more performance later, you can use Waitress as a production WSGI server:
```bash
pip install waitress
waitress-serve --host=0.0.0.0 --port=8080 run:app
```

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

## Project Structure

The application has been refactored into a modular blueprint-based structure:

```
app/
  __init__.py           # Application factory
  routes/
    auth.py             # Authentication routes (login, register, logout)
    portal.py           # Main portal routes
    admin/
      mappings.py       # Admin user-VM mappings
      clusters.py       # Cluster settings
      diagnostics.py    # Admin view and diagnostics
    api/
      vms.py            # VM list and power control API
      mappings.py       # Mappings API
      clusters.py       # Cluster switching API
      rdp.py            # RDP file generation
      ssh.py            # SSH terminal
  services/
    proxmox_service.py  # Proxmox API integration
    cluster_manager.py  # Cluster configuration
    user_manager.py     # User authentication/authorization
    ssh_service.py      # SSH WebSocket handler
    rdp_service.py      # RDP file builder
  utils/
    caching.py          # Thread-safe caching utilities
    decorators.py       # Authentication decorators
    logging.py          # Logging configuration
rdp-gen/
  templates/            # Jinja2 templates
  static/               # CSS, JS, images
  config.py             # Configuration
  proxmox_client.py     # Legacy Proxmox client (used by services)
  arp_scanner.py        # Network ARP scanner
run.py                  # Application entry point
```
