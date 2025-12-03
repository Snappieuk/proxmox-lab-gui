#!/usr/bin/env python3
"""
SSH Executor for Proxmox Operations

Provides SSH connection wrapper for executing commands on Proxmox nodes.
Used by class VM service for direct qm and qemu-img operations.
"""

import logging
import threading
from typing import Optional, Tuple

# Import paramiko for SSH execution
try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False

logger = logging.getLogger(__name__)


class SSHExecutor:
    """
    Execute commands on a remote Proxmox node via SSH.
    
    Uses the same credentials as the Proxmox API connection.
    Maintains a persistent connection for efficiency.
    """
    
    def __init__(self, host: str, username: str, password: str, port: int = 22):
        """
        Initialize SSH executor.
        
        Args:
            host: Proxmox node hostname/IP
            username: SSH username (typically 'root')
            password: SSH password
            port: SSH port (default 22)
        """
        if not PARAMIKO_AVAILABLE:
            raise RuntimeError("paramiko is required for SSH execution. Install with: pip install paramiko")
        
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self._client: Optional[paramiko.SSHClient] = None
        self._lock = threading.Lock()
    
    def connect(self) -> None:
        """Establish SSH connection."""
        with self._lock:
            if self._client is not None:
                return
            
            self._client = paramiko.SSHClient()
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            logger.info(f"Connecting to Proxmox node via SSH: {self.username}@{self.host}:{self.port}")
            self._client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=30,
                banner_timeout=30,
            )
            logger.info("SSH connection established")
    
    def disconnect(self) -> None:
        """Close SSH connection."""
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                    logger.info("SSH connection closed")
                except Exception as e:
                    logger.warning(f"Error closing SSH connection: {e}")
                finally:
                    self._client = None
    
    def execute(
        self,
        command: str,
        timeout: int = 300,
        check: bool = True,
    ) -> Tuple[int, str, str]:
        """
        Execute a command on the remote host.
        
        Args:
            command: Shell command to execute
            timeout: Command timeout in seconds
            check: If True, raise RuntimeError on non-zero exit code
            
        Returns:
            Tuple of (exit_code, stdout, stderr)
            
        Raises:
            RuntimeError: If check=True and command returns non-zero exit code
            RuntimeError: If not connected or connection fails
        """
        if self._client is None:
            raise RuntimeError("Not connected. Call connect() first.")
        
        try:
            logger.debug(f"Executing SSH command: {command}")
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            
            # Wait for command to complete
            exit_code = stdout.channel.recv_exit_status()
            
            # Read output
            stdout_text = stdout.read().decode('utf-8', errors='replace')
            stderr_text = stderr.read().decode('utf-8', errors='replace')
            
            if stdout_text:
                logger.debug(f"Command stdout: {stdout_text[:500]}")  # Log first 500 chars
            
            if exit_code != 0:
                logger.warning(f"Command exited with code {exit_code}")
                if stderr_text:
                    logger.warning(f"Command stderr: {stderr_text}")
                
                if check:
                    raise RuntimeError(f"Command failed with exit code {exit_code}: {stderr_text}")
            
            return exit_code, stdout_text, stderr_text
            
        except paramiko.SSHException as e:
            logger.error(f"SSH execution failed: {e}")
            raise RuntimeError(f"SSH execution failed: {e}")
        except Exception as e:
            logger.error(f"Command execution error: {e}")
            raise RuntimeError(f"Command execution error: {e}")
    
    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):  # noqa: U100 - required by context manager protocol
        """Context manager exit."""
        self.disconnect()
        return False


def get_ssh_executor_from_config() -> SSHExecutor:
    """
    Create an SSH executor using the current Proxmox cluster configuration.
    
    Returns:
        SSHExecutor configured with current cluster credentials
    """
    from app.services.proxmox_client import get_current_cluster
    
    cluster = get_current_cluster()
    
    # Extract username without realm (root@pam -> root)
    username = cluster["user"].split("@")[0] if "@" in cluster["user"] else cluster["user"]
    
    return SSHExecutor(
        host=cluster["host"],
        username=username,
        password=cluster["password"],
    )
