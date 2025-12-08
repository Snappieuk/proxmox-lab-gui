# WebSocket Support Through Reverse Proxy

The SSH terminal feature uses WebSockets and requires special configuration when running behind a reverse proxy (nginx, Apache, Caddy, etc.).

## The Problem

WebSocket connections need the HTTP connection to be "upgraded" from HTTP to WebSocket protocol. By default, reverse proxies don't forward these upgrade headers, causing connection failures like:

```
werkzeug.routing.exceptions.WebsocketMismatch: 400 Bad Request: The browser (or proxy) sent a request that this server could not understand.
```

## Solution: Configure Your Reverse Proxy

### Nginx Configuration

Add this location block to your nginx config:

```nginx
# WebSocket support for SSH terminal
location /ws/ {
    proxy_pass http://localhost:8080;  # Change port if your Flask app runs elsewhere
    proxy_http_version 1.1;
    
    # Critical WebSocket headers
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    
    # Standard proxy headers
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    
    # Timeouts (adjust as needed)
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}

# Regular HTTP traffic
location / {
    proxy_pass http://localhost:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### Apache Configuration

Enable required modules first:
```bash
sudo a2enmod proxy
sudo a2enmod proxy_http
sudo a2enmod proxy_wstunnel
```

Add to your VirtualHost:

```apache
# WebSocket support for SSH terminal
<Location /ws/>
    ProxyPass ws://localhost:8080/ws/
    ProxyPassReverse ws://localhost:8080/ws/
</Location>

# Regular HTTP traffic
ProxyPass / http://localhost:8080/
ProxyPassReverse / http://localhost:8080/
```

### Caddy Configuration

Caddy handles WebSockets automatically:

```caddy
your-domain.com {
    reverse_proxy localhost:8080
}
```

## Testing WebSocket Connection

1. Open browser developer tools (F12)
2. Go to Network tab
3. Filter by "WS" (WebSocket)
4. Try to connect via SSH terminal
5. Check if connection shows "101 Switching Protocols" (success) or error

## Common Issues

### 1. 400 Bad Request
**Cause**: Proxy not forwarding Upgrade headers  
**Fix**: Add WebSocket headers to proxy config (see above)

### 2. 502 Bad Gateway
**Cause**: Flask app not running or wrong port  
**Fix**: Check Flask app is running on the port specified in proxy config

### 3. Connection timeout
**Cause**: Firewall or proxy timeout settings  
**Fix**: Increase `proxy_read_timeout` in nginx or similar in other proxies

### 4. SSL/TLS errors with wss://
**Cause**: HTTPS proxy but HTTP backend  
**Fix**: Either use HTTPS between proxy and Flask, or ensure proxy terminates SSL

## Alternative: Use RDP Instead

If WebSocket configuration is problematic, students can use the RDP button instead:
- Works through any proxy without special configuration
- Downloads `.rdp` file that opens in Remote Desktop client
- More reliable for remote access scenarios

## Verifying Configuration

After configuring your reverse proxy, test with:

```bash
# Check if WebSocket endpoint responds
curl -i -N -H "Connection: Upgrade" \
     -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Version: 13" \
     -H "Sec-WebSocket-Key: test" \
     https://your-domain.com/ws/ssh/12345?ip=1.2.3.4
```

Expected response: `101 Switching Protocols` or `426 Upgrade Required`  
Bad response: `400 Bad Request` means proxy isn't forwarding headers

## Direct Access (No Proxy)

If accessing Flask directly (no reverse proxy), WebSockets work out of the box:
- `http://server:8080` uses `ws://`
- `https://server:8443` uses `wss://`

No special configuration needed!
