#!/usr/bin/env python3
"""
SSH Service - SSH terminal handling.

This module provides WebSocket-based SSH terminal functionality using paramiko.
"""

import logging
import threading
from typing import Optional

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False

logger = logging.getLogger(__name__)


class SSHWebSocketHandler:
    """
    Handles SSH connection using paramiko with password authentication.
    """
    
    def __init__(self, ws, ip: str, username: str, password: str, port: int = 22):
        self.ws = ws
        self.ip = ip
        self.username = username
        self.password = password
        self.port = port
        
        self.client: Optional[paramiko.SSHClient] = None
        self.channel: Optional[paramiko.Channel] = None
        self.running = False
        self.read_thread: Optional[threading.Thread] = None
        
    def connect(self) -> bool:
        """Establish SSH connection with password authentication."""
        if not PARAMIKO_AVAILABLE:
            logger.error("paramiko not available")
            self.send_to_client("\r\n\x1b[1;31mError: SSH module not installed.\x1b[0m\r\n")
            return False
            
        try:
            logger.info("Connecting to %s@%s:%d", self.username, self.ip, self.port)
            
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Connect with password
            self.client.connect(
                hostname=self.ip,
                port=self.port,
                username=self.username,
                password=self.password,
                look_for_keys=False,
                allow_agent=False,
                timeout=10
            )
            
            # Open interactive shell
            self.channel = self.client.invoke_shell(term='xterm-256color', width=80, height=24)
            self.running = True
            
            # Start read thread
            self.read_thread = threading.Thread(target=self._read_from_ssh, daemon=True)
            self.read_thread.start()
            
            logger.info("SSH session established")
            return True
            
        except paramiko.AuthenticationException:
            logger.error("Authentication failed for user %s", self.username)
            self.send_to_client("\r\n\x1b[1;31mAuthentication failed: Invalid username or password\x1b[0m\r\n")
            return False
        except Exception as e:
            logger.error("Connection failed: %s", e)
            self.send_to_client(f"\r\n\x1b[1;31mConnection failed: {e}\x1b[0m\r\n")
            return False
    
    def _read_from_ssh(self):
        """Read from SSH and send to WebSocket."""
        while self.running and self.channel:
            try:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096).decode('utf-8', errors='replace')
                    self.send_to_client(data)
                if self.channel.closed:
                    break
            except:
                break
        self.running = False
    
    def send_to_client(self, data: str):
        """Send data to WebSocket."""
        try:
            self.ws.send(data)
        except:
            self.running = False
    
    def handle_client_input(self, data: str):
        """Send client input to SSH."""
        try:
            if self.channel and not self.channel.closed:
                self.channel.send(data.encode('utf-8'))
        except:
            self.running = False
    
    def resize_terminal(self, width: int, height: int):
        """Resize terminal."""
        try:
            if self.channel:
                self.channel.resize_pty(width=width, height=height)
        except:
            pass
    
    def close(self):
        """Close connection."""
        self.running = False
        if self.channel:
            try:
                self.channel.close()
            except:
                pass
        if self.client:
            try:
                self.client.close()
            except:
                pass
