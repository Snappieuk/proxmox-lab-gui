# noVNC Console Implementation

## Overview
This document describes the WebSocket proxy-based noVNC console implementation that enables browser-based VNC access to Proxmox VMs without certificate warnings, authentication issues, or cross-domain restrictions.

## Architecture

### Problem Context
Direct browser → Proxmox VNC connections fail due to:
1. **Cross-domain restrictions**: Browser at `labs.netlab` cannot send cookies to `10.220.15.249`
2. **Authentication requirements**: Proxmox vncwebsocket endpoint requires BOTH PVEAuthCookie + vncticket
3. **Certificate issues**: Self-signed certificates cause browser warnings
4. **noVNC limitations**: RFB library doesn't support custom Authorization headers

### Solution: Backend WebSocket Proxy

```
┌─────────┐    WSS     ┌─────────┐    WSS + Auth    ┌──────────┐
│ Browser │ ←────────→ │  Flask  │ ←──────────────→ │ Proxmox  │
│ (noVNC) │            │  Proxy  │  (PVEAuthCookie) │    VM    │
└─────────┘            └─────────┘                   └──────────┘
```

**Flow:**
1. User clicks console button → opens `/api/console/<vmid>/view2`
2. Flask generates VNC ticket + PVEAuthCookie, stores in session
3. Browser loads `console.html` with noVNC library
4. noVNC connects to `wss://labs.netlab/api/console/ws/console/<vmid>`
5. Flask proxy retrieves session data, connects to Proxmox with authentication
6. Flask bidirectionally forwards VNC data between browser ↔ Proxmox

## Implementation Details

### Backend Components

#### 1. VNC Ticket Generation (`app/routes/api/console.py`)

**Route:** `GET /api/console/<vmid>/view2`

**Process:**
```python
# 1. Generate VNC ticket from Proxmox
vnc_data = proxmox.nodes(node).qemu(vmid).vncproxy.post(websocket=1)
ticket = vnc_data['ticket']
vnc_port = vnc_data['port']

# 2. Generate PVEAuthCookie for authentication
auth_result = proxmox.access.ticket.post(username, password)
pve_auth_cookie = auth_result['ticket']
csrf_token = auth_result['CSRFPreventionToken']

# 3. Store connection data in session (ticket valid ~2 minutes)
session[f'vnc_{vmid}_node'] = vm_node
session[f'vnc_{vmid}_type'] = vm_type  # 'qemu' or 'lxc'
session[f'vnc_{vmid}_port'] = vnc_port
session[f'vnc_{vmid}_ticket'] = ticket
session[f'vnc_{vmid}_host'] = proxmox_host
session[f'vnc_{vmid}_proxmox_port'] = proxmox_port
session[f'vnc_{vmid}_auth_cookie'] = pve_auth_cookie
session[f'vnc_{vmid}_csrf'] = csrf_token

# 4. Render console.html
return render_template('console.html', vmid=vmid, vm_name=vm_name, 
                      node=vm_node, vm_type=vm_type)
```

**Key Points:**
- VNC tickets expire in ~2 minutes - session must be fresh
- Both QEMU VMs and LXC containers supported
- PVEAuthCookie required for Proxmox authentication
- Session storage avoids exposing credentials to frontend

#### 2. WebSocket Proxy (`app/routes/api/console.py`)

**Route:** `WSS /api/console/ws/console/<vmid>`

**Function:** `init_websocket_proxy(app, sock_instance)`

**Process:**
```python
@sock.route('/ws/console/<int:vmid>')
def vnc_websocket_proxy(ws, vmid):
    # 1. Retrieve connection info from session
    node = session.get(f'vnc_{vmid}_node')
    vm_type = session.get(f'vnc_{vmid}_type')
    vnc_port = session.get(f'vnc_{vmid}_port')
    ticket = session.get(f'vnc_{vmid}_ticket')
    host = session.get(f'vnc_{vmid}_host')
    proxmox_port = session.get(f'vnc_{vmid}_proxmox_port')
    auth_cookie = session.get(f'vnc_{vmid}_auth_cookie')
    
    # 2. Build Proxmox WebSocket URL
    proxmox_ws_url = (
        f"wss://{host}:{proxmox_port}/api2/json/nodes/{node}/"
        f"{vm_type}/{vmid}/vncwebsocket?port={vnc_port}&vncticket={ticket}"
    )
    
    # 3. Connect to Proxmox with authentication cookie
    proxmox_ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
    proxmox_ws.connect(
        proxmox_ws_url,
        cookie=f"PVEAuthCookie={auth_cookie}",
        suppress_origin=True
    )
    
    # 4. Bidirectional forwarding
    # Thread: Proxmox → Browser
    def forward_to_browser():
        while True:
            data = proxmox_ws.recv()
            if not data: break
            ws.send(data)
    
    threading.Thread(target=forward_to_browser, daemon=True).start()
    
    # Main loop: Browser → Proxmox
    while True:
        data = ws.receive()
        if data is None: break
        proxmox_ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
    
    # 5. Cleanup
    proxmox_ws.close()
```

**Key Points:**
- Uses `flask-sock` for browser WebSocket handling
- Uses `websocket-client` for Proxmox WebSocket connection
- SSL verification disabled for self-signed certificates (`cert_reqs: CERT_NONE`)
- Binary data forwarding preserves VNC protocol integrity
- Daemon thread prevents blocking on Proxmox → Browser forwarding

#### 3. App Initialization (`app/__init__.py`)

```python
# Initialize WebSocket support
try:
    from flask_sock import Sock
    sock = Sock(app)
    
    # Initialize console WebSocket proxy
    from app.routes.api.console import init_websocket_proxy
    init_websocket_proxy(app, sock)
    logger.info("Console VNC WebSocket proxy ENABLED")
    
except ImportError as e:
    logger.warning(f"WebSocket support DISABLED: {e}")
```

### Frontend Components

#### console.html Template

**Location:** `app/templates/console.html`

**Key Code:**
```javascript
import RFB from 'https://cdn.jsdelivr.net/npm/@novnc/novnc@1.4.0/core/rfb.js';

const vmid = {{ vmid }};
const node = "{{ node }}";
const vmType = "{{ vm_type }}";

// Connect to Flask WebSocket proxy (NOT directly to Proxmox)
const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const wsUrl = `${protocol}//${window.location.host}/api/console/ws/console/${vmid}`;

console.log('Connecting through Flask WebSocket proxy:', wsUrl);

// Initialize noVNC client
rfb = new RFB(document.getElementById('screen'), wsUrl, {
    credentials: { password: '' }
});

// Event handlers
rfb.addEventListener('connect', () => {
    document.getElementById('loading').style.display = 'none';
    console.log('✓ Connected to VM console');
});

rfb.addEventListener('disconnect', (e) => {
    console.error('✗ Disconnected:', e.detail);
    showError(`Connection closed: ${e.detail.clean ? 'normal' : 'error'}`);
});
```

**Key Points:**
- No direct Proxmox connection - all through Flask proxy
- No cookies needed - authentication handled by backend
- No certificate warnings - single trusted domain (labs.netlab)
- Auto-detects HTTP/HTTPS for ws:// or wss:// protocol

## Dependencies

### Python Packages (requirements.txt)
```
flask-sock        # WebSocket support for Flask
websocket-client  # WebSocket client for connecting to Proxmox
```

### Frontend Libraries
```
@novnc/novnc@1.4.0  # CDN-hosted noVNC VNC client
```

## Configuration

### No Special Configuration Required
- Uses existing Proxmox admin credentials from `app/config.py`
- No Proxmox server modifications needed
- No certificate management required
- No CORS/proxy configuration needed

## Security Considerations

### Authentication Flow
1. User authenticates to Flask app (existing login system)
2. Flask authenticates to Proxmox using admin credentials
3. VNC ticket generated per-session (short-lived, ~2 minutes)
4. Browser never sees Proxmox credentials or PVEAuthCookie

### Network Isolation
- Users only connect to Flask app (labs.netlab)
- Proxmox remains isolated (10.220.15.249)
- No direct user access to Proxmox required

### Session Security
- VNC tickets stored in Flask session (server-side)
- Session cookie is httpOnly, secure (HTTPS)
- Tickets expire quickly (~2 minutes)

## Troubleshooting

### WebSocket Connection Fails (code 1006)

**Symptoms:** Console shows "Connection closed (code 1006)"

**Possible Causes:**
1. **Session expired** - VNC tickets only valid ~2 minutes
   - Solution: Refresh console page to generate new ticket
   
2. **Flask backend can't reach Proxmox** - Network/firewall issue
   - Check: `curl -k https://10.220.15.249:8006/api2/json/version`
   - Verify Flask server can reach Proxmox on port 8006
   
3. **WebSocket libraries not installed**
   - Check logs for "WebSocket support DISABLED"
   - Run: `pip install flask-sock websocket-client`
   
4. **Authentication failed** - Invalid Proxmox credentials
   - Verify `PVE_ADMIN_USER` and `PVE_ADMIN_PASS` in config
   - Test: Login to Proxmox UI with same credentials

### Console Shows Black Screen

**Symptoms:** Console connects but shows black screen

**Possible Causes:**
1. **VM not running** - VNC requires running VM
   - Solution: Start VM first, then open console
   
2. **VM has no display** - Headless or display=none
   - Check VM hardware config in Proxmox
   
3. **Graphics driver issue** - VM display not initialized
   - Solution: Wait for VM to fully boot

### "Session expired - please reload"

**Symptoms:** Console immediately closes with session error

**Possible Causes:**
1. **User opened console in new tab** - Session data in different tab
   - Solution: Open console from same browser window
   
2. **Session cleared** - Cookies deleted or expired
   - Solution: Re-login to Flask app

### Performance Issues

**Symptoms:** Slow/laggy console response

**Possible Causes:**
1. **Network latency** - Flask server far from Proxmox
   - Deploy Flask on same network as Proxmox
   
2. **High load** - Many concurrent console sessions
   - Each session uses 2 threads (forwarding loops)
   - Monitor server resources
   
3. **VM performance** - VM itself is slow
   - Check VM CPU/memory usage in Proxmox

## Testing Checklist

- [ ] Console button appears in VM three-dot menu
- [ ] Clicking console opens new popup window with black background
- [ ] Loading spinner displays while connecting
- [ ] Console shows VM display after connection
- [ ] Keyboard input works in console
- [ ] Mouse clicks work in console
- [ ] Console survives VM reboot
- [ ] Multiple concurrent console sessions work
- [ ] Console closes cleanly on window close
- [ ] Error messages display for failed connections

## Comparison with Alternatives

### ❌ Direct Browser → Proxmox WebSocket
- **Problem:** Cross-domain cookie restrictions
- **Problem:** noVNC can't send Authorization headers
- **Problem:** Certificate warnings for users

### ❌ Nginx WebSocket Proxy
- **Problem:** Can't forward authentication cookies
- **Problem:** Ticket-only auth insufficient for Proxmox

### ❌ Proxmox Native Console (iframe/window)
- **Problem:** "No ticket" errors
- **Problem:** Users see Proxmox UI/branding
- **Problem:** Requires direct Proxmox access

### ❌ Proxmox Server Modification
- **Problem:** Breaks other features (UI console, shell)
- **Problem:** Requires root access to Proxmox
- **Problem:** Breaks on Proxmox updates

### ✅ Backend WebSocket Proxy (This Implementation)
- **Advantage:** All authentication handled server-side
- **Advantage:** Single trusted domain (no certificate warnings)
- **Advantage:** No Proxmox modifications required
- **Advantage:** Users never see Proxmox directly
- **Advantage:** Works with existing infrastructure

## References

- [Proxmox VE API Documentation](https://pve.proxmox.com/wiki/Proxmox_VE_API)
- [noVNC GitHub Repository](https://github.com/novnc/noVNC)
- [flask-sock Documentation](https://flask-sock.readthedocs.io/)
- [websocket-client Documentation](https://websocket-client.readthedocs.io/)

## Credits

Implementation follows the proper Proxmox VNC workflow as documented in Proxmox forums and API documentation, using a backend WebSocket proxy to solve authentication and cross-domain challenges.
