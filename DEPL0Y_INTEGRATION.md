# Depl0y Integration

Automatically installs and runs [Depl0y](https://github.com/agit8or1/Depl0y) alongside your Lab Portal for enhanced VM management capabilities.

## Features

- **Auto-Install**: Automatically clones Depl0y from GitHub on first startup
- **Auto-Start**: Runs Depl0y backend and frontend as background services
- **Seamless Integration**: Adds Depl0y link to admin sidebar (only visible to admins)
- **Smart Detection**: Checks if Depl0y is already running before starting
- **Configurable**: Can be disabled or pointed to existing installation

## What is Depl0y?

Depl0y is an advanced VM deployment and management panel for Proxmox VE that provides:
- Ultra-fast cloud image deployment (30 seconds)
- Traditional ISO-based VM creation
- High-availability management
- Update management for VMs
- Multi-cluster support
- User roles and permissions

## Quick Start

### Default Behavior (Auto-Install)

Just start your Lab Portal normally:

```bash
python run.py
```

On first startup, the system will:
1. Clone Depl0y to `../depl0y/` directory
2. Install Python backend dependencies
3. Install Node.js frontend dependencies (if npm available)
4. Start backend on port 8000
5. Start frontend on port 3000
6. Add link to admin sidebar

Access Depl0y by clicking **"Depl0y"** in the admin sidebar!

### Manual Installation (Recommended for Production)

If you want more control, install Depl0y manually:

```bash
# Clone Depl0y
cd /opt
git clone https://github.com/agit8or1/Depl0y.git depl0y
cd depl0y

# Install backend
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 &

# Install frontend
cd ../frontend
npm install
npm run build
npm run preview -- --port 3000 --host &
```

Then configure your Lab Portal to use it:

```bash
# In .env file
ENABLE_DEPL0Y=true
DEPL0Y_URL=http://your-server:3000
```

## Configuration Options

### Environment Variables

Add to `.env` file or export before starting:

```bash
# Enable/disable Depl0y integration
ENABLE_DEPL0Y=true          # Default: true

# Use existing Depl0y installation
DEPL0Y_URL=http://10.220.15.249:3000  # Default: auto-detect
```

### Disable Depl0y Integration

```bash
# In .env
ENABLE_DEPL0Y=false
```

Or in `app/config.py`:
```python
ENABLE_DEPL0Y = False
```

### Point to Existing Installation

```bash
# In .env
DEPL0Y_URL=http://192.168.1.100:3000
```

## Ports Used

- **Backend**: 8000 (Uvicorn/FastAPI)
- **Frontend**: 3000 (Vite dev server or preview)

Make sure these ports are available or Depl0y won't start.

## Troubleshooting

### Depl0y Link Not Appearing

Check logs for initialization errors:
```bash
# Look for these messages in startup logs
"Depl0y service initializing at http://..."
"Depl0y initialization failed: ..."
```

If disabled:
```bash
"Depl0y integration disabled"
```

### Port Already in Use

If port 3000 or 8000 is taken:

**Option 1**: Stop conflicting service
```bash
# Find what's using port 3000
lsof -i :3000
# Kill it
kill -9 <PID>
```

**Option 2**: Manually install Depl0y on different ports
```bash
# Start on different ports
uvicorn main:app --port 8001 &
npm run preview -- --port 3001 &

# Configure Lab Portal
export DEPL0Y_URL=http://localhost:3001
```

### Installation Fails

**Missing Git**:
```bash
# Install git
sudo apt install git  # Debian/Ubuntu
sudo yum install git  # RHEL/CentOS
```

**Missing Node.js/npm** (frontend won't work but backend will):
```bash
# Install Node.js 18+
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install nodejs
```

**Missing Python packages**:
```bash
# Install required packages
pip install uvicorn fastapi
```

### Manual Verification

Check if Depl0y is running:
```bash
# Check backend
curl http://localhost:8000/docs

# Check frontend
curl http://localhost:3000
```

If not responding, check process:
```bash
ps aux | grep uvicorn
ps aux | grep node
```

## Architecture

```
Lab Portal (Flask)
├── Port 8080 (default)
├── Auto-starts on boot
└── Manages:
    └── Depl0y Service
        ├── Backend: Uvicorn (port 8000)
        ├── Frontend: Vite (port 3000)
        └── Directory: ../depl0y/
```

**Process Tree:**
```
python run.py
  └── Depl0y Manager Thread
      ├── git clone (first run)
      ├── pip install (first run)
      ├── npm install (first run)
      └── Background Processes:
          ├── uvicorn main:app
          └── npm run dev
```

## Security Considerations

### Network Access

Depl0y runs on `0.0.0.0` (all interfaces) by default. Consider:

1. **Firewall**: Restrict ports 3000 and 8000 to trusted networks
   ```bash
   sudo ufw allow from 10.220.0.0/16 to any port 3000
   sudo ufw allow from 10.220.0.0/16 to any port 8000
   ```

2. **Nginx Reverse Proxy**: Run behind proxy with SSL
   ```nginx
   location /depl0y/ {
       proxy_pass http://localhost:3000/;
   }
   ```

3. **VPN Only**: Only allow access from VPN-connected users

### Authentication

- Depl0y has its own user system separate from Lab Portal
- Lab Portal admins must create separate Depl0y accounts
- No automatic SSO between systems (feature for future)

### Proxmox Credentials

- Depl0y needs Proxmox credentials to function
- Store in Depl0y's config, not Lab Portal
- Use API tokens instead of passwords when possible

## Systemd Service (Production)

For production, run as systemd services instead of background threads:

**depl0y-backend.service**:
```ini
[Unit]
Description=Depl0y Backend
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/depl0y/backend
ExecStart=/usr/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

**depl0y-frontend.service**:
```ini
[Unit]
Description=Depl0y Frontend
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/depl0y/frontend
ExecStart=/usr/bin/npm run preview -- --port 3000 --host
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable services:
```bash
sudo systemctl enable depl0y-backend depl0y-frontend
sudo systemctl start depl0y-backend depl0y-frontend
```

Then disable auto-start in Lab Portal:
```bash
echo "ENABLE_DEPL0Y=false" >> .env
echo "DEPL0Y_URL=http://localhost:3000" >> .env
```

## Uninstalling

### Remove Depl0y Integration

```bash
# Disable in config
echo "ENABLE_DEPL0Y=false" >> .env

# Remove Depl0y directory
rm -rf ../depl0y/

# Restart Lab Portal
sudo systemctl restart proxmox-gui
```

### Keep Depl0y, Remove Link

Just disable integration but keep Depl0y running:
```bash
echo "ENABLE_DEPL0Y=false" >> .env
```

Link disappears from sidebar, but Depl0y keeps running if started manually.

## Performance Impact

- **Startup Time**: +5-10 seconds (first run: +2-5 minutes for install)
- **Memory**: +200-400 MB (Node.js + Python processes)
- **CPU**: Negligible when idle
- **Disk**: ~500 MB for Depl0y installation

## Comparison: Lab Portal vs Depl0y

| Feature | Lab Portal | Depl0y |
|---------|-----------|--------|
| **Primary Use** | Student lab VMs | General VM management |
| **Authentication** | Proxmox + Local | Local users |
| **VM Assignment** | Class-based | User-based |
| **Deployment** | Terraform | Direct API |
| **Speed** | Fast (Terraform) | Ultra-fast (Cloud images) |
| **Templates** | Auto-replicate | Manual setup |
| **Updates** | No | Yes |
| **HA** | No | Yes |
| **Best For** | Education | Production |

**Recommendation**: Use both!
- Lab Portal: For student labs and class management
- Depl0y: For instructor VMs and production workloads

## Future Enhancements

Planned improvements:
- [ ] SSO integration (share authentication)
- [ ] Unified VM view (show Depl0y VMs in Lab Portal)
- [ ] Cross-launch (start Depl0y VM from Lab Portal)
- [ ] Embedded iframe mode (Depl0y inside Lab Portal)

## Support

- Lab Portal Issues: https://github.com/Snappieuk/proxmox-lab-gui/issues
- Depl0y Issues: https://github.com/agit8or1/Depl0y/issues
