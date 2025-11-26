#!/usr/bin/env python3
"""
WebSocket SSH handler using paramiko.
Provides SSH terminal access through WebSocket connections.
"""

import logging
import threading
import time
import json
import paramiko
from typing import Optional

logger = logging.getLogger(__name__)


class SSHWebSocketHandler:
    """
    Handles SSH connection and bidirectional communication between WebSocket and SSH.
    """
    
    def __init__(self, ws, ip: str, username: str = "root", port: int = 22):
        self.ws = ws
        self.ip = ip
        self.username = username
        self.port = port
        
        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.channel: Optional[paramiko.Channel] = None
        self.running = False
        
    def connect(self) -> bool:
        """
        Establish SSH connection to the target host.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Try to use SSH keys first, then fall back to keyboard-interactive
            logger.info("Connecting to %s@%s:%d", self.username, self.ip, self.port)
            
            # Look for common SSH key locations
            key_paths = [
                '/root/.ssh/id_rsa',
                '/root/.ssh/id_ed25519',
                '/root/.ssh/id_ecdsa',
            ]
            
            connected = False
            for key_path in key_paths:
                try:
                    self.ssh_client.connect(
                        self.ip,
                        port=self.port,
                        username=self.username,
                        key_filename=key_path,
                        timeout=10,
                        allow_agent=True,
                        look_for_keys=True,
                        banner_timeout=10
                    )
                    logger.info("Connected using key: %s", key_path)
                    connected = True
                    break
                except (paramiko.ssh_exception.SSHException, FileNotFoundError):
                    continue
            
            if not connected:
                # Try without specific key (will use agent or prompt for password)
                try:
                    self.ssh_client.connect(
                        self.ip,
                        port=self.port,
                        username=self.username,
                        timeout=10,
                        allow_agent=True,
                        look_for_keys=True,
                        banner_timeout=10
                    )
                    connected = True
                    logger.info("Connected using SSH agent or default keys")
                except paramiko.AuthenticationException:
                    # Password required - send prompt to client
                    self.send_to_client("\r\n\x1b[1;33mPassword authentication required.\x1b[0m\r\n")
                    self.send_to_client("Please configure SSH key authentication for passwordless access.\r\n")
                    self.send_to_client("From the Proxmox host, run:\r\n")
                    self.send_to_client(f"  ssh-copy-id {self.username}@{self.ip}\r\n\r\n")
                    return False
            
            # Open interactive shell
            self.channel = self.ssh_client.invoke_shell(
                term='xterm-256color',
                width=80,
                height=24
            )
            self.channel.settimeout(0.1)
            
            self.running = True
            
            # Start background thread to read from SSH and send to WebSocket
            self.read_thread = threading.Thread(target=self._read_from_ssh, daemon=True)
            self.read_thread.start()
            
            logger.info("SSH session established")
            return True
            
        except Exception as e:
            logger.exception("Failed to connect to SSH: %s", e)
            self.send_to_client(f"\r\n\x1b[1;31mConnection failed: {str(e)}\x1b[0m\r\n")
            return False
    
    def _read_from_ssh(self):
        """
        Background thread to read from SSH and send to WebSocket.
        """
        while self.running and self.channel:
            try:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    if data:
                        self.send_to_client(data.decode('utf-8', errors='ignore'))
                else:
                    time.sleep(0.01)
            except Exception as e:
                if self.running:
                    logger.debug("Error reading from SSH: %s", e)
                break
    
    def send_to_client(self, data: str):
        """
        Send data to WebSocket client.
        
        Args:
            data: String data to send
        """
        try:
            self.ws.send(data)
        except Exception as e:
            logger.debug("Error sending to WebSocket: %s", e)
            self.running = False
    
    def handle_client_input(self, data: str):
        """
        Handle input from WebSocket client and send to SSH.
        
        Args:
            data: Input data from client
        """
        try:
            if self.channel and self.channel.send_ready():
                self.channel.send(data)
        except Exception as e:
            logger.debug("Error sending to SSH: %s", e)
    
    def resize_terminal(self, width: int, height: int):
        """
        Resize the SSH terminal.
        
        Args:
            width: Terminal width in characters
            height: Terminal height in characters
        """
        try:
            if self.channel:
                self.channel.resize_pty(width=width, height=height)
        except Exception as e:
            logger.debug("Error resizing terminal: %s", e)
    
    def close(self):
        """
        Close SSH connection and cleanup.
        """
        self.running = False
        
        try:
            if self.channel:
                self.channel.close()
        except:
            pass
        
        try:
            if self.ssh_client:
                self.ssh_client.close()
        except:
            pass
        
        logger.info("SSH session closed")
