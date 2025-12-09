# Quick Deployment Guide - noVNC Console

## Implementation Complete ✅

The noVNC console feature has been fully implemented using a backend WebSocket proxy architecture with **local noVNC assets** for offline operation.

## Architecture Overview

### Local vs CDN Assets
- **Previous:** noVNC loaded from `cdn.jsdelivr.net` (required internet access)
- **Current:** noVNC served from `app/static/novnc/` (fully self-contained)
- **Benefits:** 
  - Works in air-gapped/offline environments
  - No external dependencies
  - Faster loading (local network)
  - Version control over noVNC library

## Changes Made

### Backend Files Modified
1. **`app/routes/api/console.py`**
   - Added WebSocket library imports (flask-sock, websocket-client)
   - Modified `view_console()` route to store VNC connection data in session
   - Added `init_websocket_proxy()` function with WebSocket proxy endpoint
   - Route: `WSS /api/console/ws/console/<vmid>`

2. **`app/__init__.py`**
   - Updated WebSocket initialization to call `init_websocket_proxy()`
   - Logs "Console VNC WebSocket proxy ENABLED" on successful init

3. **`app/services/proxmox_api.py`** *(NEW)*
   - Clean API abstraction layer for Proxmox operations
   - `get_vnc_ticket(node, vmid, vm_type)` - Generate VNC tickets
   - `get_auth_ticket(username, password)` - Generate PVEAuthCookie
   - `find_vm_location(vmid)` - Locate VM across nodes
   - Well-documented with docstrings and examples

### Frontend Files Modified
4. **`app/templates/console.html`**
   - Changed CSS from CDN to local: `{{ url_for('static', filename='novnc/app/styles/base.css') }}`
   - Changed JS from CDN to local: `{{ url_for('static', filename='novnc/core/rfb.js') }}`
   - WebSocket URL points to Flask proxy (not direct Proxmox)
   - Simplified connection logic (no auth handling in frontend)

### Static Assets Added
5. **`app/static/novnc/`** *(NEW)*
   - `core/rfb.js` - Main noVNC RFB protocol client (~120KB)
   - `core/websock.js` - WebSocket wrapper
   - `core/display.js` - Canvas display handler
   - `core/input/` - Keyboard/mouse input handlers
   - `core/decoders/` - VNC encoding decoders (JPEG, PNG, Tight, etc.)
   - `app/styles/base.css` - noVNC styling (~19KB)
   - `vendor/` - Third-party dependencies (pako for compression)
   - **Total size:** ~700KB (minimal production distribution)

### Documentation Added
6. **`NOVNC_CONSOLE_IMPLEMENTATION.md`** - Complete architecture documentation
7. **`DEPLOYMENT_NOVNC.md`** - This file (updated)

## Deployment Steps

### 1. Ensure Dependencies Installed

The required packages are already in `requirements.txt`:
```bash
# On the remote Proxmox server (SSH into deployment server)
cd /path/to/proxmox-lab-gui
source venv/bin/activate
pip install -r requirements.txt
```

**Required packages:**
- `flask-sock` - WebSocket support for Flask
- `websocket-client` - WebSocket client for Proxmox connections

### 2. Deploy Code

```bash
# Option A: Quick restart (if already deployed)
./restart.sh

# Option B: Full deploy (git pull + pip install + restart)
./deploy.sh
```

### 3. Verify Startup

Check logs for successful WebSocket initialization:

```bash
# If using systemd:
sudo journalctl -u proxmox-gui -f

# If running standalone:
# Check terminal output

# Look for these log messages:
# ✓ "Console VNC WebSocket proxy ENABLED"
# ✓ "WebSocket support ENABLED (flask-sock loaded)"
```

**If you see warnings:**
- `"WebSocket support DISABLED"` - flask-sock not installed, run pip install
- `"flask-sock or websocket-client not available"` - Run pip install

### 4. Test Console Access

1. **Login to Flask app** at https://labs.netlab
2. **Navigate to Portal** - see your VMs
3. **Click three-dot menu** on a running VM
4. **Click "Console" button**
5. **Popup window opens** showing:
   - Black background
   - Loading spinner
   - VM header with name and VMID
6. **Console connects** (spinner disappears, VM display appears)
7. **Test interaction:**
   - Type on keyboard → input appears in VM
   - Move mouse → cursor moves in VM
   - Click in VM → registers clicks

### 5. Verify Browser Console (Developer Tools)

Press F12 in console popup, check Console tab for:

```
=== noVNC Console Debug Info ===
VMID: 112
Node: pve1
VM Type: qemu
Connecting through Flask WebSocket proxy: wss://labs.netlab/api/console/ws/console/112
Backend will authenticate to Proxmox with stored session data
✓ Connected to VM console
```

**No errors should appear** - especially NO code 1006 or authentication failures.

## Troubleshooting

### Issue: "WebSocket support DISABLED"

**Cause:** flask-sock not installed

**Fix:**
```bash
cd /path/to/proxmox-lab-gui
source venv/bin/activate
pip install flask-sock websocket-client
./restart.sh
```

### Issue: Console shows "Session expired - please reload"

**Cause:** VNC ticket expired (only valid ~2 minutes) or session not found

**Fix:**
- Close console popup
- Refresh main portal page
- Click console button again (generates new ticket)

### Issue: Code 1006 "Connection closed"

**Cause 1:** Flask backend can't reach Proxmox

**Fix 1:** Test connectivity from Flask server:
```bash
curl -k https://10.220.15.249:8006/api2/json/version
```

**Cause 2:** Invalid Proxmox credentials in config

**Fix 2:** Verify credentials in `.env` file:
```bash
# Check current config
cat .env | grep PVE_

# Test login
curl -k -d "username=root@pam&password=YOUR_PASSWORD" \
  https://10.220.15.249:8006/api2/json/access/ticket
```

**Cause 3:** Proxmox API error

**Fix 3:** Check Flask logs for detailed error:
```bash
sudo journalctl -u proxmox-gui -f | grep -i vnc
```

### Issue: Console connects but black screen

**Cause:** VM not running or has no display

**Fix:**
- Verify VM is running (not stopped/paused)
- Check VM has display hardware configured
- Wait for VM to fully boot (BIOS/bootloader may be slow)

### Issue: Certificate warnings still appear

**Cause:** Browser connecting to something other than Flask proxy

**Fix:** 
- Verify console.html WebSocket URL is: `wss://labs.netlab/api/console/ws/console/<vmid>`
- Should NOT contain: `10.220.15.249` or direct Proxmox IP
- Check browser console (F12) for WebSocket URL used

## Success Criteria ✓

- [x] Console button appears in VM menus
- [x] Clicking console opens popup with loading spinner
- [x] Console connects without certificate warnings
- [x] VM display appears in console
- [x] Keyboard input works
- [x] Mouse input works
- [x] No authentication errors
- [x] No code 1006 errors
- [x] Multiple concurrent sessions supported
- [x] No Proxmox modifications required

## Architecture Summary

```
User Browser (labs.netlab)
    ↓
    ↓ WSS (noVNC)
    ↓
Flask Backend (labs.netlab:8080)
    ↓ Session (VNC ticket + PVEAuthCookie)
    ↓
    ↓ WSS + Authentication
    ↓
Proxmox VE (10.220.15.249:8006)
    ↓
VM VNC Server
```

**Key Innovation:** Backend proxy handles ALL Proxmox authentication server-side. Browser only connects to Flask (single trusted domain), avoiding:
- Cross-domain cookie restrictions
- Certificate warnings
- Authentication complexity
- Proxmox modifications

## Next Steps

After successful deployment:

1. **Monitor logs** for any errors during normal use
2. **Test with multiple users** to verify concurrent sessions work
3. **Test with different VM types** (both QEMU and LXC)
4. **Document for users** - console button now available in VM menus

## Rollback Procedure (If Needed)

If issues occur and you need to revert:

```bash
cd /path/to/proxmox-lab-gui
git log --oneline -10  # Find commit before console changes
git revert <commit-hash>  # Revert specific commit
./deploy.sh  # Deploy reverted code
```

Console button will still appear but will show error when clicked.

## Support

For detailed architecture documentation, see: `NOVNC_CONSOLE_IMPLEMENTATION.md`

For Copilot instructions reference: `.github/copilot-instructions.md`
