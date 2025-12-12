#!/bin/bash
#
# Proxmox Lab GUI - Uninstaller
# Usage: sudo bash uninstall.sh
# Or: sudo bash /opt/proxmox-lab-gui/uninstall.sh
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
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
BACKUP_DIR="${APP_DIR}/backups"

echo -e "${RED}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║       Proxmox Lab GUI - Uninstaller                  ║${NC}"
echo -e "${RED}╚════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}✗ This script must be run as root${NC}"
   echo -e "${YELLOW}  Please run: sudo bash uninstall.sh${NC}"
   exit 1
fi

echo -e "${GREEN}✓ Running as root${NC}"

# Check if installation exists
if [ ! -d "$APP_DIR" ]; then
    echo -e "${YELLOW}⚠ Installation directory not found: ${APP_DIR}${NC}"
    echo -e "${YELLOW}  Checking for service anyway...${NC}"
fi

# Confirmation prompt
echo ""
echo -e "${YELLOW}⚠️  WARNING: This will completely remove Proxmox Lab GUI${NC}"
echo ""
echo -e "${RED}The following will be removed:${NC}"
echo -e "  • Application files: ${APP_DIR}"
echo -e "  • Systemd service: ${SERVICE_FILE}"
echo -e "  • Database (all users, classes, VMs): ${APP_DIR}/app/lab_portal.db"
echo -e "  • Configuration: ${APP_DIR}/.env"
echo ""
echo -e "${GREEN}The following will be kept:${NC}"
echo -e "  • System dependencies (Python, git, etc.)"
echo -e "  • Backups directory (if exists): ${BACKUP_DIR}"
echo ""
read -p "Are you sure you want to uninstall? (yes/NO): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo -e "${BLUE}Uninstall cancelled${NC}"
    exit 0
fi

echo ""
echo -e "${BLUE}Starting uninstallation...${NC}"
echo ""

# Stop service if running
if systemctl is-active --quiet ${SERVICE_NAME} 2>/dev/null; then
    echo -e "${BLUE}→ Stopping service...${NC}"
    systemctl stop ${SERVICE_NAME}
    echo -e "${GREEN}✓ Service stopped${NC}"
else
    echo -e "${YELLOW}⚠ Service is not running${NC}"
fi

# Disable service
if systemctl is-enabled --quiet ${SERVICE_NAME} 2>/dev/null; then
    echo -e "${BLUE}→ Disabling service...${NC}"
    systemctl disable ${SERVICE_NAME}
    echo -e "${GREEN}✓ Service disabled${NC}"
else
    echo -e "${YELLOW}⚠ Service was not enabled${NC}"
fi

# Remove service file
if [ -f "$SERVICE_FILE" ]; then
    echo -e "${BLUE}→ Removing systemd service file...${NC}"
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    echo -e "${GREEN}✓ Service file removed${NC}"
else
    echo -e "${YELLOW}⚠ Service file not found${NC}"
fi

# Offer to backup database before removal
if [ -f "${APP_DIR}/app/lab_portal.db" ]; then
    echo ""
    read -p "Do you want to backup the database before removal? (Y/n): " BACKUP_DB
    BACKUP_DB=${BACKUP_DB:-Y}
    
    if [[ $BACKUP_DB =~ ^[Yy]$ ]]; then
        BACKUP_NAME="pre-uninstall-backup-$(date +%Y%m%d-%H%M%S)"
        BACKUP_PATH="/tmp/${BACKUP_NAME}"
        mkdir -p "$BACKUP_PATH"
        
        echo -e "${BLUE}→ Creating backup...${NC}"
        cp "${APP_DIR}/app/lab_portal.db" "${BACKUP_PATH}/" 2>/dev/null || true
        cp "${APP_DIR}/.env" "${BACKUP_PATH}/" 2>/dev/null || true
        
        echo -e "${GREEN}✓ Backup created at: ${BACKUP_PATH}${NC}"
        echo -e "${YELLOW}  Save this backup if you want to reinstall later!${NC}"
    fi
fi

# Remove application directory
if [ -d "$APP_DIR" ]; then
    echo -e "${BLUE}→ Removing application directory...${NC}"
    
    # Ask about backups directory
    if [ -d "$BACKUP_DIR" ]; then
        read -p "Remove backups directory? (y/N): " REMOVE_BACKUPS
        REMOVE_BACKUPS=${REMOVE_BACKUPS:-N}
        
        if [[ $REMOVE_BACKUPS =~ ^[Yy]$ ]]; then
            rm -rf "$APP_DIR"
            echo -e "${GREEN}✓ Application directory and backups removed${NC}"
        else
            # Move backups before removing app dir
            if [ -d "$BACKUP_DIR" ] && [ "$(ls -A $BACKUP_DIR)" ]; then
                TEMP_BACKUP="/tmp/proxmox-lab-gui-backups-$(date +%Y%m%d-%H%M%S)"
                echo -e "${BLUE}→ Preserving backups at: ${TEMP_BACKUP}${NC}"
                mv "$BACKUP_DIR" "$TEMP_BACKUP"
                echo -e "${GREEN}✓ Backups moved to: ${TEMP_BACKUP}${NC}"
            fi
            rm -rf "$APP_DIR"
            echo -e "${GREEN}✓ Application directory removed${NC}"
        fi
    else
        rm -rf "$APP_DIR"
        echo -e "${GREEN}✓ Application directory removed${NC}"
    fi
else
    echo -e "${YELLOW}⚠ Application directory not found${NC}"
fi

# Check if any remnants exist
REMNANTS=false

if systemctl list-units --all | grep -q ${SERVICE_NAME}; then
    echo -e "${YELLOW}⚠ Service unit still loaded in systemd (will be gone after reboot)${NC}"
    REMNANTS=true
fi

if [ -f "$SERVICE_FILE" ]; then
    echo -e "${YELLOW}⚠ Service file still exists: ${SERVICE_FILE}${NC}"
    REMNANTS=true
fi

if [ -d "$APP_DIR" ]; then
    echo -e "${YELLOW}⚠ Application directory still exists: ${APP_DIR}${NC}"
    REMNANTS=true
fi

# Uninstallation complete
echo ""
if [ "$REMNANTS" = false ]; then
    echo -e "${GREEN}╔════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║         Uninstallation Complete! ✓                    ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${YELLOW}╔════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║      Uninstallation Complete (with warnings)          ║${NC}"
    echo -e "${YELLOW}╚════════════════════════════════════════════════════════╝${NC}"
fi
echo ""
echo -e "${BLUE}What was removed:${NC}"
echo -e "  ✓ Application files"
echo -e "  ✓ Systemd service"
echo -e "  ✓ Database and configuration"
echo ""
echo -e "${BLUE}What was kept:${NC}"
echo -e "  • System dependencies (Python, git, nmap, etc.)"

if [ -n "$BACKUP_PATH" ]; then
    echo -e "  • Database backup: ${BACKUP_PATH}"
fi

if [ -n "$TEMP_BACKUP" ]; then
    echo -e "  • Previous backups: ${TEMP_BACKUP}"
fi

echo ""
echo -e "${BLUE}To reinstall:${NC}"
echo -e "  ${YELLOW}curl -sSL https://raw.githubusercontent.com/Snappieuk/proxmox-lab-gui/main/install.sh | sudo bash${NC}"
echo ""

# Optional: Offer to remove system dependencies
echo -e "${YELLOW}Note: System dependencies (Python, git, etc.) were not removed${NC}"
echo -e "${YELLOW}      as they may be used by other applications.${NC}"
echo ""
read -p "Do you want to remove Python packages that were installed? (y/N): " REMOVE_DEPS
REMOVE_DEPS=${REMOVE_DEPS:-N}

if [[ $REMOVE_DEPS =~ ^[Yy]$ ]]; then
    echo -e "${BLUE}→ Removing Python packages...${NC}"
    
    # Detect OS and remove packages
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
        
        if [[ "$OS" == "ubuntu" ]] || [[ "$OS" == "debian" ]]; then
            apt-get remove -y python3-pip python3-venv > /dev/null 2>&1 || true
            apt-get autoremove -y > /dev/null 2>&1 || true
            echo -e "${GREEN}✓ Python packages removed${NC}"
        elif [[ "$OS" == "centos" ]] || [[ "$OS" == "rhel" ]] || [[ "$OS" == "fedora" ]]; then
            if [[ "$OS" == "fedora" ]]; then
                dnf remove -y python3-pip > /dev/null 2>&1 || true
            else
                yum remove -y python3-pip > /dev/null 2>&1 || true
            fi
            echo -e "${GREEN}✓ Python packages removed${NC}"
        fi
    fi
fi

echo ""
echo -e "${GREEN}Thank you for using Proxmox Lab GUI!${NC}"
echo ""
