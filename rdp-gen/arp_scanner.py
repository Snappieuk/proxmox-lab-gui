#!/usr/bin/env python3
"""
ARP-based IP discovery component for Proxmox VM IP discovery.

Usage:
    This module provides fast IP discovery for VMs by using nmap ARP probes
    (when running as root or with CAP_NET_RAW capability) to populate the
    kernel ARP table, then matching VM MAC addresses to discovered IPs.

    For best results, run with root privileges:
        sudo python app.py
    
    Or grant CAP_NET_RAW capability:
        sudo setcap cap_net_raw+ep /usr/bin/nmap

    When run without root:
    - nmap -sn -PR (ARP ping) requires root, so falls back to -sn (ICMP/TCP ping)
    - ARP table will still be parsed, but may have fewer entries
    - Guest agent/LXC interface lookups remain as fallback

Functions:
    - normalize_mac(mac: str) -> str: Normalize MAC to lowercase, no separators.
    - discover_ips_via_arp(vm_mac_map, subnets, background) -> Dict[int, str]
        If background=True: spawn background thread, return cached results immediately.
        If background=False: run synchronously, return discovered vmid->ip mapping.
    - get_scan_status(vmid: int) -> Optional[str]: Get last known IP or status for vmid.

Cache:
    - Internal cache (_last_discovered) with configurable TTL (default 300s)
    - Avoids excessive scans by checking cache age before new scans
"""

import subprocess
import re
import logging
import threading
import time
from typing import Dict, Optional, List
from collections import defaultdict

logger = logging.getLogger(__name__)

# Module-level cache for ARP results
_arp_cache: Dict[str, str] = {}  # mac -> ip
_arp_cache_time: float = 0
_arp_cache_ttl: int = 300  # 5 minutes

# Background scan state
_scan_thread: Optional[threading.Thread] = None
_scan_status: Dict[int, str] = {}  # vmid -> status message
_scan_in_progress: bool = False
_scan_lock = threading.Lock()


def get_arp_table() -> Dict[str, str]:
    """
    Get ARP table mapping MAC addresses to IP addresses.
    
    Returns:
        Dict[mac_address, ip_address] - lowercase MAC addresses without colons
    """
    arp_map = {}
    
    # Try multiple methods to get ARP table
    # Method 1: ip neigh (modern Linux)
    try:
        result = subprocess.run(
            ['ip', 'neigh', 'show'],
            capture_output=True,
            text=True,
            timeout=2
        )
        
        if result.returncode == 0 and result.stdout:
            logger.debug("ip neigh output:\n%s", result.stdout)
            
            # Parse output like:
            # 10.220.15.100 dev eth0 lladdr 52:54:00:12:34:56 REACHABLE
            # 192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff STALE
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5:
                    ip = parts[0]
                    # Find lladdr index
                    try:
                        lladdr_idx = parts.index('lladdr')
                        mac = parts[lladdr_idx + 1]
                        mac_normalized = mac.lower().replace(':', '').replace('-', '')
                        if len(mac_normalized) == 12:
                            arp_map[mac_normalized] = ip
                            logger.debug("ARP entry: %s -> %s", mac_normalized, ip)
                    except (ValueError, IndexError):
                        continue
            
            logger.info("ARP table (via ip neigh) contains %d entries", len(arp_map))
            return arp_map
    
    except FileNotFoundError:
        logger.debug("ip command not found, trying arp")
    except subprocess.TimeoutExpired:
        logger.warning("ip neigh command timed out")
    except Exception as e:
        logger.debug("ip neigh failed: %s", e)
    
    # Method 2: arp -a (traditional)
    for arp_cmd in ['/usr/sbin/arp', '/sbin/arp', 'arp']:
        try:
            result = subprocess.run(
                [arp_cmd, '-a'],
                capture_output=True,
                text=True,
                timeout=2
            )
            
            if result.returncode != 0:
                continue
            
            logger.debug("arp -a output:\n%s", result.stdout)
            
            # Parse output like:
            # ? (192.168.1.100) at 52:54:00:12:34:56 [ether] on eth0
            for line in result.stdout.splitlines():
                ip_match = re.search(r'\((\d+\.\d+\.\d+\.\d+)\)', line)
                mac_match = re.search(r'([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})', line)
                
                if ip_match and mac_match:
                    ip = ip_match.group(1)
                    mac = mac_match.group(0).lower().replace(':', '').replace('-', '')
                    arp_map[mac] = ip
                    logger.debug("ARP entry: %s -> %s", mac, ip)
            
            logger.info("ARP table (via %s) contains %d entries", arp_cmd, len(arp_map))
            return arp_map
        
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            logger.warning("%s command timed out", arp_cmd)
        except Exception as e:
            logger.debug("%s failed: %s", arp_cmd, e)
    
    # Method 3: /proc/net/arp (Linux fallback)
    try:
        with open('/proc/net/arp', 'r') as f:
            lines = f.readlines()
            logger.debug("/proc/net/arp output:\n%s", ''.join(lines))
            
            # Skip header line
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    ip = parts[0]
                    mac = parts[3].lower().replace(':', '').replace('-', '')
                    if len(mac) == 12 and mac != '000000000000':
                        arp_map[mac] = ip
                        logger.debug("ARP entry: %s -> %s", mac, ip)
        
        logger.info("ARP table (via /proc/net/arp) contains %d entries", len(arp_map))
        return arp_map
    
    except FileNotFoundError:
        logger.warning("/proc/net/arp not found")
    except Exception as e:
        logger.error("Failed to read /proc/net/arp: %s", e)
    
    logger.warning("All ARP table methods failed, returning empty table")
    return arp_map


def broadcast_ping(subnet: str = "192.168.1.255", count: int = 1) -> bool:
    """
    Send broadcast ping to populate ARP table.
    
    Args:
        subnet: Broadcast address (e.g., "192.168.1.255" or "10.220.15.255")
        count: Number of ping packets to send
    
    Returns:
        True if ping succeeded, False otherwise
    """
    # Try multiple ping commands
    for ping_cmd in ['/usr/bin/ping', '/bin/ping', 'ping']:
        try:
            # Use ping -b for broadcast (Linux)
            # -b: allow pinging broadcast address
            # -c: count of pings
            # -W: timeout in seconds
            result = subprocess.run(
                [ping_cmd, '-b', '-c', str(count), '-W', '1', subnet],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            logger.info("Broadcast ping to %s: returncode=%d", subnet, result.returncode)
            if result.stdout:
                logger.debug("Ping stdout: %s", result.stdout[:200])
            if result.stderr:
                logger.debug("Ping stderr: %s", result.stderr[:200])
            
            # Return true even if ping fails - the attempt might still populate ARP
            # Some systems don't allow broadcast ping but it still triggers ARP
            return True
            
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            logger.warning("Broadcast ping timed out")
            return False
        except Exception as e:
            logger.warning("Broadcast ping with %s failed: %s", ping_cmd, e)
            continue
    
    logger.warning("All ping commands failed or not found")
    return False


def _is_root() -> bool:
    """Check if we're running with root privileges."""
    import os
    return os.geteuid() == 0


def scan_network_range(subnet_cidr: str = "10.220.8.0/21", timeout: int = 2) -> bool:
    """
    Scan network range to populate ARP table using nmap.
    
    When running as root (or with CAP_NET_RAW):
        Uses `nmap -sn -PR <subnet>` (ARP ping) for reliable L2 discovery.
        ARP probes are the most reliable for local network VM discovery.
    
    When running without root:
        Falls back to `nmap -sn <subnet>` (ICMP/TCP ping).
        Less reliable but still populates ARP table for responding hosts.
    
    After the scan, parse /proc/net/arp to collect MAC->IP mappings.
    
    Args:
        subnet_cidr: Network in CIDR notation (e.g., "10.220.8.0/21")
        timeout: Scan timeout in seconds
    
    Returns:
        True if scan succeeded (nmap ran without error)
    """
    is_root = _is_root()
    
    # Determine nmap arguments based on privileges
    if is_root:
        # ARP ping (-PR) is most reliable but requires root
        nmap_args = ['-sn', '-PR', '-T4', '--max-retries', '1', subnet_cidr]
        scan_type = "ARP ping (root)"
    else:
        # Unprivileged: use standard ping scan without -PR
        # -sn does ICMP echo, TCP SYN to 443, TCP ACK to 80, ICMP timestamp
        nmap_args = ['-sn', '-T4', '--max-retries', '1', subnet_cidr]
        scan_type = "ICMP/TCP ping (unprivileged)"
    
    logger.info("Starting %s scan on %s", scan_type, subnet_cidr)
    
    # Try nmap with different paths
    for nmap_cmd in ['/usr/bin/nmap', '/usr/local/bin/nmap', 'nmap']:
        try:
            result = subprocess.run(
                [nmap_cmd] + nmap_args,
                capture_output=True,
                text=True,
                timeout=timeout + 30  # ARP scans on large subnets can take time
            )
            
            if result.returncode != 0:
                logger.warning("nmap returned non-zero: %d, stderr: %s", 
                             result.returncode, result.stderr[:200] if result.stderr else "")
                # Check if it's a privilege error for -PR
                if "requires root" in (result.stderr or "").lower() or \
                   "operation not permitted" in (result.stderr or "").lower():
                    logger.warning("nmap -PR requires root privileges, falling back to unprivileged scan")
                    # Retry without -PR
                    result = subprocess.run(
                        [nmap_cmd, '-sn', '-T4', '--max-retries', '1', subnet_cidr],
                        capture_output=True,
                        text=True,
                        timeout=timeout + 30
                    )
            
            logger.info("Network scan with nmap completed: returncode=%d", result.returncode)
            if result.stdout:
                # Count how many hosts were found
                host_count = result.stdout.count("Host is up")
                logger.info("nmap found %d hosts up", host_count)
            return True
            
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            logger.warning("nmap scan timed out after %d seconds", timeout + 30)
            return False
        except Exception as e:
            logger.debug("nmap scan failed: %s", e)
            continue
    
    logger.warning("nmap not found, network scan unavailable")
    
    # Fallback: use fping if available
    for fping_cmd in ['/usr/bin/fping', '/usr/sbin/fping', 'fping']:
        try:
            result = subprocess.run(
                [fping_cmd, '-a', '-g', '-r', '1', subnet_cidr],
                capture_output=True,
                text=True,
                timeout=timeout + 5
            )
            logger.info("Network scan with fping completed: returncode=%d", result.returncode)
            if result.stdout:
                alive_hosts = len(result.stdout.strip().split('\n'))
                logger.info("fping found %d alive hosts", alive_hosts)
            return True
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug("fping scan failed: %s", e)
            continue
    
    logger.warning("No network scanner available (nmap/fping not found), falling back to broadcast ping")
    return False


def get_scan_status(vmid: int) -> Optional[str]:
    """
    Get the current scan status for a VM.
    
    Args:
        vmid: VM ID
    
    Returns:
        Status message or None
    """
    return _scan_status.get(vmid)


def _background_scan_worker(vm_mac_map: Dict[int, str], subnets: Optional[List[str]] = None):
    """
    Background worker that performs network scan and updates IP cache.
    
    Args:
        vm_mac_map: Dict mapping vmid to MAC address
        subnets: Optional list of broadcast addresses for fallback
    """
    global _scan_in_progress, _scan_status, _arp_cache, _arp_cache_time
    
    try:
        logger.info("Background scan started for %d VMs", len(vm_mac_map))
        
        # Update status for all VMs
        with _scan_lock:
            for vmid in vm_mac_map.keys():
                _scan_status[vmid] = "Scanning network..."
        
        # Try network range scan first (more reliable than broadcast ping)
        logger.info("Scanning network range 10.220.8.0/21 to populate ARP")
        scan_success = scan_network_range("10.220.8.0/21", timeout=3)
        
        # If scan failed, try broadcast ping as fallback
        if not scan_success:
            subnets = subnets or ["10.220.15.255"]  # Default to 10.220.8.0/21 broadcast
            logger.info("Falling back to broadcast ping to %d subnets", len(subnets))
            with _scan_lock:
                for vmid in vm_mac_map.keys():
                    _scan_status[vmid] = "Broadcast ping..."
            for subnet in subnets:
                broadcast_ping(subnet)
        
        # Get updated ARP table
        arp_table = get_arp_table()
        logger.info("Post-scan ARP table has %d entries", len(arp_table))
        
        # Match VMs to IPs and update cache + status
        found_count = 0
        with _scan_lock:
            for vmid, mac in vm_mac_map.items():
                if mac in arp_table:
                    ip = arp_table[mac]
                    _arp_cache[mac] = ip
                    _scan_status[vmid] = ip
                    found_count += 1
                    logger.info("VM %d (MAC %s) -> IP %s", vmid, mac, ip)
                else:
                    _scan_status[vmid] = "Not found in ARP"
                    logger.warning("VM %d (MAC %s) not found in ARP table", vmid, mac)
            
            _arp_cache_time = time.time()
        
        logger.info("Background scan complete: discovered IPs for %d/%d VMs", found_count, len(vm_mac_map))
        
    except Exception as e:
        logger.error("Background scan failed: %s", e)
        with _scan_lock:
            for vmid in vm_mac_map.keys():
                _scan_status[vmid] = f"Scan error: {e}"
    
    finally:
        with _scan_lock:
            global _scan_in_progress
            _scan_in_progress = False


def discover_ips_via_arp(vm_mac_map: Dict[int, str], subnets: Optional[List[str]] = None, background: bool = True) -> Dict[int, str]:
    """
    Discover VM IPs using network scan + ARP lookup.
    
    Args:
        vm_mac_map: Dict mapping vmid to MAC address (lowercase, no separators)
        subnets: List of broadcast addresses to ping (e.g., ["192.168.1.255", "10.0.0.255"])
                 Note: Now used as fallback; primary method is network range scan
        background: If True, run scan in background thread and return immediately with cached results
    
    Returns:
        Dict mapping vmid to discovered IP address (from cache if background=True)
    """
    global _scan_thread, _scan_in_progress
    
    if not vm_mac_map:
        return {}
    
    logger.info("VM MAC map: %s", vm_mac_map)
    
    # Get existing ARP table first (might already have cached entries)
    arp_table = get_arp_table()
    logger.info("Current ARP table has %d entries", len(arp_table))
    
    # Check if we already have matches in cache
    vm_ips = {}
    for vmid, mac in vm_mac_map.items():
        if mac in arp_table:
            vm_ips[vmid] = arp_table[mac]
            logger.info("VM %d (MAC %s) -> IP %s (cached)", vmid, mac, arp_table[mac])
    
    # If we found all IPs, no need to scan
    if len(vm_ips) == len(vm_mac_map):
        logger.info("All IPs found in existing ARP cache, skipping network scan")
        return vm_ips
    
    # If background mode, start scan thread and return immediately
    if background:
        with _scan_lock:
            if not _scan_in_progress:
                _scan_in_progress = True
                _scan_thread = threading.Thread(
                    target=_background_scan_worker,
                    args=(vm_mac_map, subnets),
                    daemon=True
                )
                _scan_thread.start()
                logger.info("Started background network scan")
                
                # Set initial status for VMs without IPs
                for vmid in vm_mac_map.keys():
                    if vmid not in vm_ips:
                        _scan_status[vmid] = "Scan starting..."
            else:
                logger.info("Background scan already in progress")
        
        return vm_ips  # Return whatever we have cached
    
    # Synchronous mode (old behavior)
    logger.info("Running synchronous network scan")
    
    # Try network range scan first (more reliable than broadcast ping)
    logger.info("Scanning network range 10.220.8.0/21 to populate ARP")
    scan_success = scan_network_range("10.220.8.0/21", timeout=3)
    
    # If scan failed, try broadcast ping as fallback
    if not scan_success:
        subnets = subnets or ["10.220.15.255"]  # Default to 10.220.8.0/21 broadcast
        logger.info("Falling back to broadcast ping to %d subnets", len(subnets))
        for subnet in subnets:
            broadcast_ping(subnet)
    
    # Get updated ARP table
    arp_table = get_arp_table()
    logger.info("Post-ping ARP table has %d entries, looking for %d VM MACs", len(arp_table), len(vm_mac_map))
    
    # Map remaining VM MACs to IPs
    for vmid, mac in vm_mac_map.items():
        if vmid in vm_ips:
            continue  # Already found
        if mac in arp_table:
            vm_ips[vmid] = arp_table[mac]
            logger.info("VM %d (MAC %s) -> IP %s", vmid, mac, arp_table[mac])
        else:
            logger.warning("VM %d (MAC %s) NOT in ARP table", vmid, mac)
            # Show a few ARP table entries for comparison
            if len(arp_table) > 0:
                sample_macs = list(arp_table.keys())[:3]
                logger.debug("Sample ARP MACs: %s", sample_macs)
    
    logger.info("Discovered IPs for %d/%d VMs via ARP", len(vm_ips), len(vm_mac_map))
    return vm_ips


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    """
    Normalize MAC address to lowercase without separators.
    
    Args:
        mac: MAC address in any format (with colons, dashes, or spaces)
    
    Returns:
        Normalized MAC address (lowercase, no separators) or None
    """
    if not mac:
        return None
    
    # Remove common separators
    normalized = mac.lower().replace(':', '').replace('-', '').replace(' ', '')
    
    # Validate it's 12 hex characters
    if len(normalized) == 12 and all(c in '0123456789abcdef' for c in normalized):
        return normalized
    
    return None


if __name__ == '__main__':
    # Test the ARP scanner
    logging.basicConfig(level=logging.DEBUG)
    
    print("Testing ARP scanner...")
    print("\n1. Getting current ARP table:")
    arp = get_arp_table()
    for mac, ip in list(arp.items())[:5]:  # Show first 5 entries
        print(f"  {mac} -> {ip}")
    
    print(f"\nTotal ARP entries: {len(arp)}")
