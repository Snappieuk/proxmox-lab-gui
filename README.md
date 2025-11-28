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

## Class-Based Lab Management

This system supports a class-based lab management workflow similar to Azure Labs:

### Features

- **User Roles**: Three role types - `adminer` (full admin), `teacher` (can create/manage classes), `user` (student)
- **Classes**: Teachers create classes linked to Proxmox VM templates
- **VM Pools**: VMs are cloned from templates and assigned to students
- **Invite Links**: Generate invite tokens (7-day expiry or never expire) for students to join
- **Student VM Control**: Students can start/stop their assigned VM and revert to baseline
- **Fast Revert**: Uses Proxmox snapshots for quick VM restoration

### Database

The system uses SQLite for persistent storage:
- Users (with password hashing and roles)
- Classes (linked to templates and teachers)
- Templates (registered from Proxmox cluster 10.220.15.249)
- VM Assignments (tracks which VMs belong to which classes/users)

The database file is stored at `rdp-gen/lab_portal.db` and is created automatically on first run.

### Workflow

1. **Setup**: Admin creates teacher accounts by setting `role='teacher'`
2. **Class Creation**: Teachers create classes and select a template from Proxmox
3. **VM Pool**: Teachers add VMs to the class pool (clones template to multiple VMs)
4. **Invite Students**: Generate an invite link and share with students
5. **Student Join**: Students click invite link to auto-assign a VM
6. **Lab Use**: Students start/stop their VM and can revert to baseline anytime
7. **Push Updates**: Teachers can push updates to all class VMs (creates new baseline snapshot)

### Migration from mappings.json

The existing `mappings.json` system continues to work for legacy VM-to-user mappings.
The new class-based system operates independently and uses the SQLite database.
To migrate existing users, create local user accounts with matching usernames.
