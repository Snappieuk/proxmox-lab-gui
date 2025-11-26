# Proxmox GUI replicating Azure Labs functionality
Basic python + flask webapp to replicate azure labs functionality with proxmox

## Development Setup

Run locally by creating a Python virtualenv and starting the Flask app:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export PVE_HOST=<host>
export PVE_ADMIN_USER=root@pam
export PVE_ADMIN_PASS=<secret>
export MAPPINGS_FILE=rdp-gen/mappings.json
python3 rdp-gen/app.py
```

## Production Deployment

### Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run with the provided start script
chmod +x start.sh
./start.sh
```

### Manual Start

```bash
# Run directly with Python (recommended for lab/internal use)
cd rdp-gen
python3 app.py
```

### Alternative: Waitress WSGI Server

For production deployments requiring a WSGI server, you can use Waitress:

```bash
# Install waitress
pip install waitress

# Run with waitress
waitress-serve --host=0.0.0.0 --port=8080 --call 'app:create_app'
# Or simpler:
# waitress-serve --host=0.0.0.0 --port=8080 app:app
```

### Systemd Service (Recommended for Production)

1. Edit `proxmox-gui.service` and update:
   - `User=` and `Group=` to your username/group
   - All `/path/to/proxmox-lab-gui` paths to your actual installation path

2. Install and enable the service:

```bash
sudo cp proxmox-gui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable proxmox-gui
sudo systemctl start proxmox-gui
```

3. Check status and logs:

```bash
sudo systemctl status proxmox-gui
sudo journalctl -u proxmox-gui -f
```

4. Restart after code changes:

```bash
sudo systemctl restart proxmox-gui
```

### Configuration

- **.env** - Environment variables (see `.env.example`)
- **rdp-gen/config.py** - Application configuration

Key environment variables:
- `PROXMOX_CACHE_TTL` - Short-lived cache TTL in seconds (default: 10)
- `VM_CACHE_TTL` - VM list cache TTL in seconds (default: 120)

### Auto-restart on Git Push

Add to your deployment script:

```bash
cd /path/to/proxmox-lab-gui
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart proxmox-gui
```
