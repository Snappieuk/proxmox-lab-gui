#!/usr/bin/env python3
"""
SSH Executor for Proxmox Operations

Provides SSH connection wrapper for executing commands on Proxmox nodes.
Used by class VM service for direct qm and qemu-img operations.
"""

import logging
import threading
import time
from typing import Dict, Optional, Tuple

# Import paramiko for SSH execution
try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False

logger = logging.getLogger(__name__)


# Global SSH connection pool
class SSHConnectionPool:
    """
    Thread-safe SSH connection pool for reusing connections.
    
    Maintains one connection per (host, username) pair.
    Connections are lazily created and automatically reconnected if dropped.
    """
    
    MAX_CONNECTIONS = 50  # Prevent unbounded connection growth
    
    def __init__(self, max_connections: int = MAX_CONNECTIONS):
        self._connections: Dict[Tuple[str, str], 'SSHExecutor'] = {}
        self._lock = threading.Lock()
        self._last_used: Dict[Tuple[str, str], float] = {}
        self._connection_timeout = 600  # Close connections idle for 10 minutes
        self._max_connections = max_connections
        self._stats = {
            'created': 0,
            'reused': 0,
            'closed': 0,
            'dropped': 0,  # Closed due to max exceeded
        }
    
    def get_executor(self, host: str, username: str, password: str, port: int = 22) -> 'SSHExecutor':
        """
        Get or create an SSH executor from the pool.
        
        Args:
            host: Proxmox node hostname/IP
            username: SSH username
            password: SSH password
            port: SSH port
            
        Returns:
            SSHExecutor instance (connected)
            
        Raises:
            RuntimeError: If pool is at max capacity and no idle connections to recycle
        """
        key = (host, username)
        
        with self._lock:
            # Clean up stale connections
            self._cleanup_stale_connections()
            
            # Check if we have a connection
            if key in self._connections:
                executor = self._connections[key]
                
                # Verify connection is still alive
                if executor.is_connected():
                    self._last_used[key] = time.time()
                    self._stats['reused'] += 1
                    logger.debug(f"Reusing SSH connection to {username}@{host}")
                    return executor
                else:
                    logger.info(f"SSH connection to {username}@{host} dropped, reconnecting...")
                    try:
                        executor.connect()
                        self._last_used[key] = time.time()
                        return executor
                    except Exception as e:
                        logger.warning(f"Failed to reconnect SSH to {username}@{host}: {e}")
                        # Remove dead connection
                        del self._connections[key]
                        if key in self._last_used:
                            del self._last_used[key]
            
            # Check if pool is at capacity
            if len(self._connections) >= self._max_connections:
                # Try to recycle oldest idle connection
                if self._last_used:
                    oldest_key = min(self._last_used, key=self._last_used.get)
                    logger.warning(f"SSH pool at max capacity ({self._max_connections}), "
                                 f"closing oldest idle connection: {oldest_key[1]}@{oldest_key[0]}")
                    try:
                        self._connections[oldest_key].close()
                    except Exception as e:
                        logger.warning(f"Error closing idle SSH connection: {e}")
                    del self._connections[oldest_key]
                    del self._last_used[oldest_key]
                    self._stats['dropped'] += 1
                else:
                    raise RuntimeError(f"SSH connection pool at max capacity ({self._max_connections}) "
                                     f"and no idle connections to recycle")
            
            # Create new connection
            logger.info(f"Creating new pooled SSH connection to {username}@{host} "
                       f"({len(self._connections) + 1}/{self._max_connections})")
            executor = SSHExecutor(host, username, password, port)
            executor.connect()
            self._connections[key] = executor
            self._last_used[key] = time.time()
            self._stats['created'] += 1
            return executor
    
    def _cleanup_stale_connections(self):
        """Close connections that haven't been used recently."""
        current_time = time.time()
        stale_keys = []
        
        for key, last_used in self._last_used.items():
            if current_time - last_used > self._connection_timeout:
                stale_keys.append(key)
        
        for key in stale_keys:
            if key in self._connections:
                try:
                    self._connections[key].disconnect()
                    logger.info(f"Closed stale SSH connection to {key[1]}@{key[0]}")
                    self._stats['closed'] += 1
                except Exception as e:
                    logger.warning(f"Error closing stale connection: {e}")
                finally:
                    del self._connections[key]
                    if key in self._last_used:
                        del self._last_used[key]
    
    def close_all(self):
        """Close all pooled connections."""
        with self._lock:
            for executor in self._connections.values():
                try:
                    executor.disconnect()
                    self._stats['closed'] += 1
                except Exception as e:
                    logger.warning(f"Error closing connection: {e}")
            self._connections.clear()
            self._last_used.clear()
            logger.info("Closed all pooled SSH connections")
    
    def get_stats(self) -> Dict:
        """Get connection pool statistics."""
        with self._lock:
            return {
                'created': self._stats['created'],
                'reused': self._stats['reused'],
                'closed': self._stats['closed'],
                'dropped': self._stats['dropped'],
                'active': len(self._connections),
                'max_size': self._max_connections,
                'utilization_percent': int((len(self._connections) / self._max_connections) * 100),
            }


# Global connection pool instance
_connection_pool = SSHConnectionPool()


def get_pooled_ssh_executor(host: str, username: str, password: str, port: int = 22) -> 'SSHExecutor':
    """
    Get an SSH executor from the global connection pool.
    
    This is more efficient than creating new connections, especially for
    repeated operations like class VM creation.
    
    Args:
        host: Proxmox node hostname/IP
        username: SSH username
        password: SSH password
        port: SSH port
        
    Returns:
        SSHExecutor instance (already connected)
    """
    return _connection_pool.get_executor(host, username, password, port)


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
    
    def is_connected(self) -> bool:
        """Check if SSH connection is active."""
        with self._lock:
            if self._client is None:
                return False
            
            try:
                # Try to get transport and check if it's active
                transport = self._client.get_transport()
                return transport is not None and transport.is_active()
            except Exception:
                return False
    
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
    
    DEPRECATED: Use get_pooled_ssh_executor_from_config() for better performance.
    
    Returns:
        SSHExecutor configured with current cluster credentials (NOT connected)
    """
    from app.services.proxmox_service import get_current_cluster
    
    cluster = get_current_cluster()
    
    # Extract username without realm (root@pam -> root)
    username = cluster["user"].split("@")[0] if "@" in cluster["user"] else cluster["user"]
    
    return SSHExecutor(
        host=cluster["host"],
        username=username,
        password=cluster["password"],
    )


def get_pooled_ssh_executor_from_config() -> SSHExecutor:
    """
    Get an SSH executor from the connection pool using current cluster config.
    
    This is the RECOMMENDED way to get SSH connections - it reuses existing
    connections and is much faster than creating new ones.
    
    Returns:
        SSHExecutor from pool (already connected and ready to use)
    """
    try:
        from app.services.proxmox_service import get_current_cluster
        
        cluster = get_current_cluster()
        
        # Extract username without realm (root@pam -> root)
        username = cluster["user"].split("@")[0] if "@" in cluster["user"] else cluster["user"]
        
        logger.info(f"Getting pooled SSH executor for {username}@{cluster['host']}")
        
        return get_pooled_ssh_executor(
            host=cluster["host"],
            username=username,
            password=cluster["password"],
        )
    except Exception as e:
        logger.error(f"Failed to create Proxmox connection with pooling: {e}")
        raise
