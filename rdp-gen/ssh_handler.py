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
    
    def __init__(self, ws, ip: str, username: str = 'root', port: int = 22):
        self.ws = ws
        self.ip = ip
        self.username = username
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
            ssh_cmd = [
                'ssh',
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null',
                '-o', 'NumberOfPasswordPrompts=3',
                '-o', 'PubkeyAuthentication=no',  # Disable key auth to force password
                '-o', 'PreferredAuthentications=password',  # Only use password auth
                '-o', 'PasswordAuthentication=yes',  # Enable password auth
                '-o', 'BatchMode=no',  # Interactive mode
                '-v',  # Verbose for debugging
                f'{self.username}@{self.ip}'
            ]
            logger.info("Starting SSH with command: %s", ' '.join(ssh_cmd))
            
            # Set environment to force SSH to use our PTY
            env = os.environ.copy()
            env['SSH_ASKPASS_REQUIRE'] = 'never'
            
            self.process = subprocess.Popen(
                ssh_cmd,
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                env=env,
                preexec_fn=os.setsid
            )
            logger.info("SSH process started with PID: %d", self.process.pid)
            
            # Close slave fd in parent (only child needs it)
            os.close(self.slave_fd)
            self.slave_fd = None
            
            # Set non-blocking mode on master fd
            import fcntl
            flag = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)
            
            self.running = True
            
            # Start background thread immediately to capture any output
            self.read_thread = threading.Thread(target=self._read_from_ssh, daemon=True)
            self.read_thread.start()
            
            # Give SSH time to initialize and display prompts
            time.sleep(0.3)
            
            # Check if process is still running
            poll_result = self.process.poll()
            if poll_result is not None:
                logger.error("SSH process exited with code %d", poll_result)
                self.running = False
                self.send_to_client(f"\r\n\x1b[1;31mSSH process terminated with exit code {poll_result}\x1b[0m\r\n")
                return False
            
            logger.info("SSH session established, process PID %d still running", self.process.pid)
            return True
            
        except Exception as e:
            logger.exception("Failed to connect to SSH: %s", e)
            self.send_to_client(f"\r\n\x1b[1;31mConnection failed: {str(e)}\x1b[0m\r\n")
            return False
    
    def _read_from_ssh(self):
        """
        Background thread to read from SSH PTY and send to WebSocket.
        """
        logger.info("SSH read thread started")
        while self.running and self.master_fd:
            try:
                # Use select to wait for data with timeout
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                
                if ready:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        # Send to WebSocket client
                        decoded = data.decode('utf-8', errors='replace')
                        logger.info("SSH output (%d bytes): %r", len(data), decoded[:200])
                        self.send_to_client(decoded)
                    else:
                        # EOF - SSH process closed
                        logger.info("SSH process sent EOF, connection closed")
                        self.running = False
                        break
                        
            except OSError as e:
                # PTY closed
                logger.info("OSError reading from SSH: %s", e)
                break
            except Exception as e:
                logger.error("Error reading from SSH: %s", e, exc_info=True)
                break
        
        logger.info("SSH read thread exiting")
    
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
        logger.info("Closing SSH connection (running=%s, process=%s)", self.running, self.process)
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
