# Proxmox GUI replicating Azure Labs functionality
Basic python + flask webapp to replicate azure labs functionality with proxmox

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
