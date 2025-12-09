# VNC Console Authentication Troubleshooting

## Overview
This document explains how authentication works for the noVNC console feature and how to troubleshoot "401 no token" errors and other authentication issues.

## Authentication Flow

### Step 1: User Opens Console
When a user clicks the console button, they access `/api/console/<vmid>/view2`:

```
User clicks "Console" button
  ↓
GET /api/console/<vmid>/view2
  ↓
Flask checks user permissions (login_required)
  ↓
Flask loads cluster credentials from config
```

### Step 2: VNC Ticket Generation
Flask generates **two** authentication tokens from Proxmox:

```python
# 1. VNC Ticket (short-lived, ~2 minutes)
vnc_data = proxmox.nodes(node).qemu(vmid).vncproxy.post(websocket=1)
ticket = vnc_data['ticket']      # VNC authentication token
vnc_port = vnc_data['port']      # Dynamic port number (5900-5999)

# 2. PVEAuthCookie (Proxmox session cookie)
auth_result = proxmox.access.ticket.post(username=admin_user, password=admin_pass)
pve_auth_cookie = auth_result['ticket']      # HTTP authentication cookie
csrf_token = auth_result['CSRFPreventionToken']
```

### Step 3: Session Storage
Both tokens are stored in Flask session for the WebSocket proxy:

```python
session[f'vnc_{vmid}_cluster_id'] = cluster_id
session[f'vnc_{vmid}_node'] = vm_node
session[f'vnc_{vmid}_type'] = vm_type           # 'qemu' or 'lxc'
session[f'vnc_{vmid}_vnc_port'] = vnc_port      # VNC-specific port
session[f'vnc_{vmid}_ticket'] = ticket          # VNC ticket
session[f'vnc_{vmid}_host'] = proxmox_host      # Proxmox server IP
session[f'vnc_{vmid}_proxmox_port'] = proxmox_port  # Proxmox API port (8006)
session[f'vnc_{vmid}_auth_cookie'] = pve_auth_cookie
session[f'vnc_{vmid}_csrf'] = csrf_token
session[f'vnc_{vmid}_user'] = admin_user        # Stored for re-auth
session[f'vnc_{vmid}_password'] = admin_pass    # Stored for re-auth
```

**Why store credentials?** The WebSocket connection happens later and needs to regenerate tickets (they expire in ~2 minutes).

### Step 4: Browser Connects to WebSocket Proxy
Browser loads `console.html` and connects to Flask WebSocket proxy:

```
Browser: ws://flask-server/api/console/ws/console/<vmid>
  ↓
Flask WebSocket Proxy (init_websocket_proxy)
  ↓
Retrieves credentials from session
  ↓
Generates FRESH VNC ticket (just-in-time)
  ↓
Generates FRESH PVEAuthCookie
  ↓
Connects to: wss://proxmox:8006/.../vncwebsocket?port=<vnc_port>&vncticket=<ticket>
  with Cookie: PVEAuthCookie=<cookie>
```

### Step 5: Bidirectional Forwarding
Flask proxies all VNC traffic between browser and Proxmox:

```
Browser ←→ Flask WebSocket Proxy ←→ Proxmox VNC WebSocket
           (No auth needed)           (Authenticated with cookie + ticket)
```

## Common Authentication Errors

### Error: "401 no token"

**Symptom:** Console shows "Connection failed: 401" or logs show "401 Unauthorized"

**Cause:** Proxmox rejected the VNC ticket or PVEAuthCookie

**Debug Steps:**

1. **Check Flask logs** for VNC ticket generation:
   ```bash
   journalctl -u proxmox-gui -f | grep -i vnc
   ```
   
   Look for:
   - ✅ `"VNC ticket generated: port=5900"`
   - ✅ `"PVEAuthCookie generated successfully"`
   - ❌ `"Failed to generate VNC ticket"`
   - ❌ `"Failed to generate PVEAuthCookie"`

2. **Verify Proxmox admin credentials** in `app/config.py` or `.env`:
   ```bash
   # Check that these are correct:
   PVE_ADMIN_USER=root@pam
   PVE_ADMIN_PASS=your_password
   ```
   
   Test credentials manually:
   ```bash
   curl -k -d "username=root@pam&password=YOUR_PASSWORD" \
        https://PROXMOX_IP:8006/api2/json/access/ticket
   ```
   
   Should return JSON with `"ticket"` and `"CSRFPreventionToken"`

3. **Check ticket expiration timing:**
   - VNC tickets expire in ~2 minutes
   - If user opens console page and waits >2min before WebSocket connects, ticket is expired
   - Solution: WebSocket proxy regenerates tickets just-in-time (already implemented)

4. **Verify session is not expired:**
   ```python
   # In Flask logs, look for:
   "Missing VNC session data for VM {vmid}"
   ```
   
   If present, Flask session expired. User needs to:
   - Close console window
   - Refresh main portal page
   - Open console again

### Error: "Session expired - please reload console page"

**Symptom:** Console immediately closes with this message

**Cause:** Flask session data is missing for this VM

**Solutions:**

1. **User opened console in new tab/window with different session:**
   - Open console from same browser window/tab where user is logged in
   - Don't copy console URL to new private/incognito window

2. **Flask session storage issue:**
   - Check that `SECRET_KEY` is set in config (required for secure sessions)
   - Verify session cookie is being sent (check browser DevTools → Network → Cookies)

3. **Browser blocked cookies:**
   - Ensure browser allows cookies from Flask domain
   - Check for strict cookie policies (SameSite, Secure flags)

### Error: "Connection to Proxmox failed"

**Symptom:** Console shows "Connection to Proxmox failed" or WebSocket closes immediately

**Causes & Solutions:**

1. **Network connectivity:**
   ```bash
   # From Flask server, test Proxmox WebSocket endpoint:
   curl -k -v -i -N \
     -H "Connection: Upgrade" \
     -H "Upgrade: websocket" \
     https://PROXMOX_IP:8006/api2/json/nodes/NODE/qemu/VMID/vncwebsocket
   ```
   
   Should get `101 Switching Protocols` response

2. **SSL certificate issues:**
   - WebSocket proxy disables SSL verification: `sslopt={"cert_reqs": ssl.CERT_NONE}`
   - If still failing, check firewall rules between Flask server and Proxmox

3. **VM not running:**
   - VNC requires VM to be in "running" state
   - Check VM status in Proxmox UI first

### Error: "No ticket" in Proxmox logs

**Symptom:** Proxmox logs show "vncwebsocket: no ticket" or similar

**Cause:** VNC ticket not properly passed in WebSocket URL

**Debug:**

1. Check Flask logs for WebSocket URL construction:
   ```
   "Connecting to Proxmox VNC WebSocket..."
   "  URL: wss://10.220.15.249:8006/api2/json/nodes/pve1/qemu/112/vncwebsocket?port=5900&vncticket=..."
   ```

2. Verify ticket is URL-encoded:
   ```python
   from urllib.parse import quote_plus
   encoded_ticket = quote_plus(ticket)  # Essential for special characters
   ```

3. Check ticket is in URL query string, not as separate parameter

## Testing Authentication

### Test 1: Manual VNC Ticket Generation

```python
from app.services.proxmox_api import get_vnc_ticket

# Generate ticket
result = get_vnc_ticket(node='pve1', vmid=112, vm_type='kvm')
print(f"Ticket: {result['ticket'][:50]}...")
print(f"Port: {result['port']}")
```

Should print ticket and port number without errors.

### Test 2: Manual PVEAuthCookie Generation

```python
from app.services.proxmox_api import get_auth_ticket

# Generate auth cookie
cookie, csrf = get_auth_ticket(username='root@pam', password='YOUR_PASSWORD')
print(f"Cookie: {cookie[:50]}...")
print(f"CSRF: {csrf}")
```

Should print cookie and CSRF token.

### Test 3: WebSocket Connection

```bash
# Monitor Flask logs while opening console:
journalctl -u proxmox-gui -f

# Open console in browser
# Check logs for these messages:
# ✓ "WebSocket opened for VM {vmid} console"
# ✓ "Generating VNC ticket for VM {vmid}"
# ✓ "VNC ticket generated: port={port}"
# ✓ "Generating PVEAuthCookie for user {username}"
# ✓ "PVEAuthCookie generated successfully"
# ✓ "Connecting to Proxmox VNC WebSocket..."
# ✓ "Connected to Proxmox VNC for VM {vmid}"
```

## Security Considerations

### Why Store Passwords in Session?

**Problem:** VNC tickets expire in ~2 minutes, but console sessions can last hours.

**Solution:** Store Proxmox admin credentials in Flask session (server-side) to regenerate tickets just-in-time when WebSocket connects.

**Security:**
- Credentials stored server-side in Flask session (not visible to browser)
- Session cookies are httpOnly and secure (HTTPS)
- Only authenticated users can access console endpoint (`@login_required`)
- Session expires with Flask session timeout

**Alternative:** Use Proxmox API tokens (longer-lived, no password storage)

### Proxmox Permissions Required

The Proxmox user configured in `app/config.py` must have these permissions:

- `VM.Console` - Required to generate VNC tickets
- `VM.Audit` - Required to list VMs and find VM location
- `Sys.Audit` - Required to list nodes

Default `root@pam` user has all permissions.

## Configuration Checklist

Before deploying VNC console, verify:

- [ ] Proxmox admin credentials correct in `.env` or `config.py`
- [ ] Flask `SECRET_KEY` set (required for sessions)
- [ ] Dependencies installed: `flask-sock`, `websocket-client`
- [ ] Flask server can reach Proxmox on port 8006 (HTTPS)
- [ ] SSL verification disabled for self-signed certs (default)
- [ ] User has valid Flask session (logged in)
- [ ] VM is running (VNC requires running VM)

## Logging Best Practices

For troubleshooting authentication, enable DEBUG logging:

```python
# In app/__init__.py or run.py
import logging
logging.basicConfig(level=logging.DEBUG)

# Or specific to console module:
logging.getLogger('app.routes.api.console').setLevel(logging.DEBUG)
```

Key log messages to look for:

- ✅ `"Stored VNC session data for VM {vmid}"` - Session setup successful
- ✅ `"WebSocket opened for VM {vmid} console"` - WebSocket connected
- ✅ `"VNC ticket generated: port={port}"` - Ticket generation successful
- ✅ `"PVEAuthCookie generated successfully"` - Authentication successful
- ✅ `"Connected to Proxmox VNC for VM {vmid}"` - Final connection successful

- ❌ `"Missing VNC session data"` - Session expired or invalid
- ❌ `"Failed to generate VNC ticket"` - Proxmox API error
- ❌ `"Authentication failed"` - Invalid credentials
- ❌ `"Connection to Proxmox failed"` - Network or SSL error

## References

- [Proxmox VE API Documentation](https://pve.proxmox.com/pve-docs/api-viewer/index.html)
- [Proxmox VNC WebSocket Protocol](https://pve.proxmox.com/wiki/VNC_Web_Console)
- [noVNC Documentation](https://github.com/novnc/noVNC/blob/master/docs/API.md)
