#!/bin/bash
#
# Proxmox Lab GUI - One-Command Updater
# Usage: bash update.sh
# Or: bash /opt/proxmox-lab-gui/update.sh
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
APP_NAME="proxmox-lab-gui"
APP_DIR="/opt/proxmox-lab-gui"
SERVICE_NAME="proxmox-gui"
VENV_DIR="${APP_DIR}/venv"
BACKUP_DIR="${APP_DIR}/backups"

echo -e "${BLUE}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║       Proxmox Lab GUI - Automated Updater            ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}✗ This script must be run as root${NC}"
   echo -e "${YELLOW}  Please run: sudo bash update.sh${NC}"
   exit 1
fi

echo -e "${GREEN}✓ Running as root${NC}"

# Check if installation exists
if [ ! -d "$APP_DIR" ]; then
    echo -e "${RED}✗ Installation directory not found: ${APP_DIR}${NC}"
    echo -e "${YELLOW}  Please run install.sh first${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Installation found at ${APP_DIR}${NC}"

# Change to app directory
cd "$APP_DIR"

# Check if service is running
SERVICE_WAS_RUNNING=false
if systemctl is-active --quiet ${SERVICE_NAME}; then
    SERVICE_WAS_RUNNING=true
    echo -e "${GREEN}✓ Service is running${NC}"
else
    echo -e "${YELLOW}⚠ Service is not running${NC}"
fi

# Create backup directory
echo -e "${BLUE}→ Creating backup...${NC}"
mkdir -p "$BACKUP_DIR"
BACKUP_NAME="backup-$(date +%Y%m%d-%H%M%S)"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}"
mkdir -p "$BACKUP_PATH"

# Backup critical files
cp -r "${APP_DIR}/app/lab_portal.db" "${BACKUP_PATH}/" 2>/dev/null || echo "  No database to backup"
cp "${APP_DIR}/.env" "${BACKUP_PATH}/" 2>/dev/null || echo "  No .env to backup"
echo -e "${GREEN}✓ Backup created at ${BACKUP_PATH}${NC}"

# Clean up old backups (keep last 5)
echo -e "${BLUE}→ Cleaning old backups...${NC}"
cd "$BACKUP_DIR"
ls -t | tail -n +6 | xargs -r rm -rf
cd "$APP_DIR"
echo -e "${GREEN}✓ Old backups cleaned (kept last 5)${NC}"

# Stop service if running
if [ "$SERVICE_WAS_RUNNING" = true ]; then
    echo -e "${BLUE}→ Stopping service...${NC}"
    systemctl stop ${SERVICE_NAME}
    echo -e "${GREEN}✓ Service stopped${NC}"
fi

# Stash any local changes
echo -e "${BLUE}→ Checking for local changes...${NC}"
if git diff-index --quiet HEAD --; then
    echo -e "${GREEN}✓ No local changes${NC}"
else
    echo -e "${YELLOW}⚠ Stashing local changes...${NC}"
    git stash save "Auto-stash before update $(date +%Y%m%d-%H%M%S)"
    echo -e "${GREEN}✓ Local changes stashed${NC}"
fi

# Fetch latest changes
echo -e "${BLUE}→ Fetching latest updates...${NC}"
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git fetch origin

# Check if updates available
LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse origin/${CURRENT_BRANCH})

if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
    echo -e "${GREEN}✓ Already up to date!${NC}"
    UPDATE_AVAILABLE=false
else
    echo -e "${YELLOW}⚠ Updates available${NC}"
    UPDATE_AVAILABLE=true
fi

# Pull latest changes
if [ "$UPDATE_AVAILABLE" = true ]; then
    echo -e "${BLUE}→ Pulling latest changes...${NC}"
    git pull origin ${CURRENT_BRANCH}
    echo -e "${GREEN}✓ Code updated${NC}"
    
    # Update Python dependencies
    echo -e "${BLUE}→ Updating Python dependencies...${NC}"
    source "${VENV_DIR}/bin/activate"
    pip install --upgrade pip > /dev/null 2>&1
    pip install -r requirements.txt --upgrade > /dev/null 2>&1
    echo -e "${GREEN}✓ Dependencies updated${NC}"
    
    # Run database migrations
    echo -e "${BLUE}→ Running database migrations...${NC}"
    python3 migrate_db.py
    echo -e "${GREEN}✓ Database migrated${NC}"
else
    echo -e "${BLUE}→ Checking dependencies anyway...${NC}"
    source "${VENV_DIR}/bin/activate"
    pip install -r requirements.txt > /dev/null 2>&1
    echo -e "${GREEN}✓ Dependencies checked${NC}"
fi

# Restart service if it was running
if [ "$SERVICE_WAS_RUNNING" = true ]; then
    echo -e "${BLUE}→ Starting service...${NC}"
    systemctl start ${SERVICE_NAME}
    
    # Wait a moment for service to start
    sleep 2
    
    # Check service status
    if systemctl is-active --quiet ${SERVICE_NAME}; then
        echo -e "${GREEN}✓ Service started successfully${NC}"
    else
        echo -e "${RED}✗ Service failed to start${NC}"
        echo -e "${YELLOW}  Restoring from backup...${NC}"
        
        # Restore backup
        cp "${BACKUP_PATH}/lab_portal.db" "${APP_DIR}/app/" 2>/dev/null || true
        cp "${BACKUP_PATH}/.env" "${APP_DIR}/" 2>/dev/null || true
        
        systemctl start ${SERVICE_NAME}
        
        echo -e "${RED}✗ Update failed, check logs: journalctl -u ${SERVICE_NAME} -f${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}⚠ Service was not running, not starting it${NC}"
fi

# Get IP address
IP_ADDR=$(hostname -I | awk '{print $1}')

# Show service status
SERVICE_STATUS=$(systemctl is-active ${SERVICE_NAME} 2>/dev/null || echo "inactive")

# Update complete
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║             Update Complete! 🚀                        ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════╝${NC}"
echo ""

if [ "$UPDATE_AVAILABLE" = true ]; then
    echo -e "${BLUE}Changes applied:${NC}"
    echo -e "  From: ${YELLOW}${LOCAL_COMMIT:0:8}${NC}"
    echo -e "  To:   ${YELLOW}${REMOTE_COMMIT:0:8}${NC}"
    echo ""
fi

echo -e "${BLUE}Service Status:${NC} ${GREEN}${SERVICE_STATUS}${NC}"
echo ""
echo -e "${BLUE}Access the web interface at:${NC}"
echo -e "  ${GREEN}http://${IP_ADDR}:8080${NC}"
echo ""
echo -e "${BLUE}Useful Commands:${NC}"
echo -e "  Status:  ${YELLOW}systemctl status ${SERVICE_NAME}${NC}"
echo -e "  Logs:    ${YELLOW}journalctl -u ${SERVICE_NAME} -f${NC}"
echo -e "  Restart: ${YELLOW}systemctl restart ${SERVICE_NAME}${NC}"
echo ""
echo -e "${BLUE}Backup Location:${NC}"
echo -e "  ${YELLOW}${BACKUP_PATH}${NC}"
echo ""

# Show recent commits if updates were applied
if [ "$UPDATE_AVAILABLE" = true ]; then
    echo -e "${BLUE}Recent Changes:${NC}"
    git log --oneline --decorate -5 | sed 's/^/  /'
    echo ""
fi
