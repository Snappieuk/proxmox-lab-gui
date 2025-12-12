#!/bin/bash
#
# Proxmox Lab GUI - One-Command Installer
# Usage: curl -sSL https://raw.githubusercontent.com/Snappieuk/proxmox-lab-gui/main/install.sh | bash
# Or: wget -qO- https://raw.githubusercontent.com/Snappieuk/proxmox-lab-gui/main/install.sh | bash
# Or: bash install.sh
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
VENV_DIR="${APP_DIR}/venv"
REPO_URL="https://github.com/Snappieuk/proxmox-lab-gui.git"

echo -e "${BLUE}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║      Proxmox Lab GUI - Automated Installer           ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}✗ This script must be run as root${NC}"
   echo -e "${YELLOW}  Please run: sudo bash install.sh${NC}"
   exit 1
fi

echo -e "${GREEN}✓ Running as root${NC}"

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
    OS_VERSION=$VERSION_ID
    echo -e "${GREEN}✓ Detected OS: ${OS} ${OS_VERSION}${NC}"
else
    echo -e "${RED}✗ Cannot detect OS${NC}"
    exit 1
fi

# Check if already installed
if [ -d "$APP_DIR" ]; then
    echo -e "${YELLOW}⚠ Installation directory already exists: ${APP_DIR}${NC}"
    read -p "Do you want to remove and reinstall? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${BLUE}→ Stopping existing service...${NC}"
        systemctl stop ${SERVICE_NAME} 2>/dev/null || true
        systemctl disable ${SERVICE_NAME} 2>/dev/null || true
        echo -e "${BLUE}→ Removing existing installation...${NC}"
        rm -rf "$APP_DIR"
        rm -f "$SERVICE_FILE"
        systemctl daemon-reload
    else
        echo -e "${RED}✗ Installation cancelled${NC}"
        exit 1
    fi
fi

# Install system dependencies
echo ""
echo -e "${BLUE}→ Installing system dependencies...${NC}"

if [[ "$OS" == "ubuntu" ]] || [[ "$OS" == "debian" ]]; then
    apt-get update -qq
    apt-get install -y -qq \
        python3 \
        python3-pip \
        python3-venv \
        git \
        nmap \
        sqlite3 \
        curl \
        wget \
        openssh-client \
        > /dev/null 2>&1
    echo -e "${GREEN}✓ System dependencies installed${NC}"
    
elif [[ "$OS" == "centos" ]] || [[ "$OS" == "rhel" ]] || [[ "$OS" == "fedora" ]]; then
    if [[ "$OS" == "fedora" ]]; then
        dnf install -y -q \
            python3 \
            python3-pip \
            git \
            nmap \
            sqlite \
            curl \
            wget \
            openssh-clients \
            > /dev/null 2>&1
    else
        yum install -y -q \
            python3 \
            python3-pip \
            git \
            nmap \
            sqlite \
            curl \
            wget \
            openssh-clients \
            > /dev/null 2>&1
    fi
    echo -e "${GREEN}✓ System dependencies installed${NC}"
else
    echo -e "${YELLOW}⚠ Unknown OS, attempting to continue...${NC}"
fi

# Create application directory
echo -e "${BLUE}→ Creating application directory...${NC}"
mkdir -p "$APP_DIR"
cd "$APP_DIR"
echo -e "${GREEN}✓ Application directory created: ${APP_DIR}${NC}"

# Clone repository
echo -e "${BLUE}→ Cloning repository...${NC}"
git clone --depth 1 "$REPO_URL" .
echo -e "${GREEN}✓ Repository cloned${NC}"

# Create Python virtual environment
echo -e "${BLUE}→ Creating Python virtual environment...${NC}"
python3 -m venv "$VENV_DIR"
echo -e "${GREEN}✓ Virtual environment created${NC}"

# Activate virtual environment and install dependencies
echo -e "${BLUE}→ Installing Python dependencies (this may take a minute)...${NC}"
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt > /dev/null 2>&1
echo -e "${GREEN}✓ Python dependencies installed${NC}"

# Create .env file if it doesn't exist
if [ ! -f "${APP_DIR}/.env" ]; then
    echo -e "${BLUE}→ Creating .env configuration file...${NC}"
    # Generate random secret key
    SECRET_KEY=$(openssl rand -hex 32)
    
    if [ -f "${APP_DIR}/.env.example" ]; then
        cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
        # Replace placeholder with actual random key
        sed -i "s/change-me-to-random-string-in-production/${SECRET_KEY}/" "${APP_DIR}/.env"
        echo -e "${GREEN}✓ .env file created with random secret key${NC}"
    else
        # Create minimal .env
        cat > "${APP_DIR}/.env" <<EOF
# Flask Configuration
SECRET_KEY=${SECRET_KEY}
FLASK_ENV=production

# Note: Proxmox clusters are configured via /admin/settings UI
EOF
        echo -e "${GREEN}✓ .env file created${NC}"
    fi
else
    echo -e "${GREEN}✓ .env file already exists${NC}"
fi

# Initialize database
echo -e "${BLUE}→ Initializing database...${NC}"
python3 migrate_db.py
echo -e "${GREEN}✓ Database initialized${NC}"

# Create systemd service
echo -e "${BLUE}→ Creating systemd service...${NC}"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Proxmox Lab GUI - Web Portal for VM Management
After=network.target
Documentation=https://github.com/Snappieuk/proxmox-lab-gui

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
Environment="PATH=${VENV_DIR}/bin"
ExecStart=${VENV_DIR}/bin/python3 ${APP_DIR}/run.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Security settings
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

echo -e "${GREEN}✓ Systemd service created${NC}"

# Set proper permissions
echo -e "${BLUE}→ Setting permissions...${NC}"
chown -R root:root "$APP_DIR"
chmod +x "${APP_DIR}/run.py"
chmod 600 "${APP_DIR}/.env"
echo -e "${GREEN}✓ Permissions set${NC}"

# Reload systemd and enable service
echo -e "${BLUE}→ Enabling and starting service...${NC}"
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl start ${SERVICE_NAME}

# Wait a moment for service to start
sleep 2

# Check service status
if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo -e "${GREEN}✓ Service started successfully${NC}"
else
    echo -e "${RED}✗ Service failed to start${NC}"
    echo -e "${YELLOW}  Check logs with: journalctl -u ${SERVICE_NAME} -f${NC}"
    exit 1
fi

# Get IP address
IP_ADDR=$(hostname -I | awk '{print $1}')

# Installation complete
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║           Installation Complete! 🎉                   ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BLUE}Access the web interface at:${NC}"
echo -e "  ${GREEN}http://${IP_ADDR}:8080${NC}"
echo -e "  ${GREEN}http://localhost:8080${NC} (if accessing locally)"
echo ""
echo -e "${BLUE}Service Management:${NC}"
echo -e "  Start:    ${YELLOW}systemctl start ${SERVICE_NAME}${NC}"
echo -e "  Stop:     ${YELLOW}systemctl stop ${SERVICE_NAME}${NC}"
echo -e "  Restart:  ${YELLOW}systemctl restart ${SERVICE_NAME}${NC}"
echo -e "  Status:   ${YELLOW}systemctl status ${SERVICE_NAME}${NC}"
echo -e "  Logs:     ${YELLOW}journalctl -u ${SERVICE_NAME} -f${NC}"
echo ""
echo -e "${BLUE}Configuration:${NC}"
echo -e "  Edit:     ${YELLOW}nano ${APP_DIR}/.env${NC}"
echo -e "  Clusters: ${YELLOW}http://${IP_ADDR}:8080/admin/settings${NC}"
echo ""
echo -e "${BLUE}Next Steps:${NC}"
echo -e "  1. Login as admin (any Proxmox user)"
echo -e "  2. Configure clusters at http://${IP_ADDR}:8080/admin/settings"
echo -e "  3. Add your first cluster with credentials"
echo -e "  4. Create teacher/student accounts or classes"
echo ""
echo -e "${BLUE}Update:${NC}"
echo -e "  Run:      ${YELLOW}bash ${APP_DIR}/update.sh${NC}"
echo ""
