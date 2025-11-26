#!/usr/bin/env python3
"""
WebSocket SSH handler using subprocess to run native SSH client.
Provides SSH terminal access through WebSocket connections.
"""

import logging
import subprocess
import threading
import time
import json
import os
import pty
import select
from typing import Optional

logger = logging.getLogger(__name__)


class SSHWebSocketHandler:
    """
    Handles SSH connection and bidirectional communication between WebSocket and SSH.
    Uses system SSH client for interactive authentication (user types username/password).
    """
    
    def __init__(self, ws, ip: str, port: int = 22):
        self.ws = ws
        self.ip = ip
        self.port = port
        
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.read_thread: Optional[threading.Thread] = None
        
    def connect(self) -> bool:
        """
        Establish SSH connection using system SSH client.
        Opens interactive terminal where user types credentials.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            logger.info("Starting SSH connection to %s:%d", self.ip, self.port)
            
            # Create a pseudo-terminal
            self.master_fd, self.slave_fd = pty.openpty()
            
            # Start SSH process with PTY
            # Flags:
            # -o StrictHostKeyChecking=no: Auto-accept host keys
            # -o UserKnownHostsFile=/dev/null: Don't save host keys
            # -o PreferredAuthentications=keyboard-interactive,password: Try interactive/password auth
            # -o PubkeyAuthentication=no: Disable SSH key auth attempts
            # -o IdentitiesOnly=yes: Don't use SSH agent
            # -o ServerAliveInterval=30: Keep connection alive
            # -o ServerAliveCountMax=3: Disconnect after 3 failed keepalives
            # Note: If target requires publickey only, connection will fail with "Permission denied"
            self.process = subprocess.Popen(
                [
                    'ssh',
                    '-p', str(self.port),
                    '-o', 'StrictHostKeyChecking=no',
                    '-o', 'UserKnownHostsFile=/dev/null',
                    '-o', 'PreferredAuthentications=keyboard-interactive,password',
                    '-o', 'PubkeyAuthentication=no',
                    '-o', 'IdentitiesOnly=yes',
                    '-o', 'ServerAliveInterval=30',
                    '-o', 'ServerAliveCountMax=3',
                    self.ip
                ],
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                preexec_fn=os.setsid
            )
            
            # Close slave fd in parent (only child needs it)
            os.close(self.slave_fd)
            self.slave_fd = None
            
            # Set non-blocking mode on master fd
            import fcntl
            flag = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)
            
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
        Background thread to read from SSH PTY and send to WebSocket.
        """
        while self.running and self.master_fd:
            try:
                # Use select to wait for data with timeout
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                
                if ready:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        # Send to WebSocket client
                        self.send_to_client(data.decode('utf-8', errors='replace'))
                    else:
                        # EOF - SSH process closed
                        self.running = False
                        break
                        
            except OSError:
                # PTY closed
                break
            except Exception as e:
                logger.debug("Error reading from SSH: %s", e)
                break
        
        logger.debug("SSH read thread exiting")
    
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
            if self.master_fd:
                os.write(self.master_fd, data.encode('utf-8'))
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
            if self.master_fd:
                import fcntl
                import termios
                import struct
                # Set window size using ioctl
                winsize = struct.pack('HHHH', height, width, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
                logger.debug("Resized terminal to %dx%d", width, height)
        except Exception as e:
            logger.debug("Error resizing terminal: %s", e)
    
    def close(self):
        """
        Close SSH connection and cleanup.
        """
        logger.debug("Closing SSH connection")
        self.running = False
        
        try:
            if self.process:
                self.process.terminate()
                time.sleep(0.5)
                if self.process.poll() is None:
                    self.process.kill()
        except Exception as e:
            logger.debug("Error terminating SSH process: %s", e)
        
        try:
            if self.master_fd:
                os.close(self.master_fd)
                self.master_fd = None
        except Exception as e:
            logger.debug("Error closing PTY: %s", e)
        
        if self.read_thread and self.read_thread.is_alive():
            self.read_thread.join(timeout=1.0)
        
        logger.info("SSH session closed")
