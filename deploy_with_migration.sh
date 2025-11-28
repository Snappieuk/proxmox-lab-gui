#!/bin/bash
# Remote server deployment script with database migration
# Run this on your Proxmox server after pulling code changes

set -e  # Exit on error

echo "================================"
echo "Proxmox Lab GUI - Deploy & Migrate"
echo "================================"
echo ""

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if database exists
DB_PATH="$SCRIPT_DIR/app/lab_portal.db"

if [ ! -f "$DB_PATH" ]; then
    echo "⚠️  Database not found at $DB_PATH"
    echo "   It will be created on first run."
    echo ""
else
    echo "✓ Database found: $DB_PATH"
    echo ""
    
    # Backup database
    BACKUP_PATH="$DB_PATH.backup.$(date +%Y%m%d_%H%M%S)"
    echo "Creating backup: $BACKUP_PATH"
    cp "$DB_PATH" "$BACKUP_PATH"
    echo "✓ Backup created"
    echo ""
    
    # Run migration
    echo "Running database migration..."
    python3 migrate_database.py
    echo ""
fi

# Check if running via systemd
if systemctl is-active --quiet proxmox-gui 2>/dev/null; then
    echo "Detected systemd service, restarting..."
    sudo systemctl restart proxmox-gui
    echo "✓ Service restarted"
    echo ""
    echo "Check status with: sudo systemctl status proxmox-gui"
    echo "View logs with: sudo journalctl -u proxmox-gui -f"
else
    echo "No systemd service detected."
    echo "Restart manually if app is running."
fi

echo ""
echo "================================"
echo "✓ Deployment complete!"
echo "================================"
