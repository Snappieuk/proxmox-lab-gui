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

## Production Deployment with Gunicorn

### Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run with the provided start script
chmod +x start.sh
./start.sh
```

### Manual Gunicorn Start

```bash
# With default config (from gunicorn.conf.py)
gunicorn --config gunicorn.conf.py --chdir rdp-gen app:app

# Or specify options directly
gunicorn -w 4 -b 0.0.0.0:8080 --chdir rdp-gen app:app
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

- **gunicorn.conf.py** - Gunicorn server settings (workers, ports, logging)
- **.env** - Environment variables (see `.env.example`)
- **rdp-gen/config.py** - Application configuration

### Auto-restart on Git Push

Add to your deployment script:

```bash
cd /path/to/proxmox-lab-gui
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart proxmox-gui
```
