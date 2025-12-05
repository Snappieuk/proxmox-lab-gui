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
import os
import threading
import time
import ipaddress
from typing import Dict, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

# Module-level cache for ARP results
_arp_cache: Dict[str, str] = {}  # mac -> ip
_arp_cache_time: float = 0
_arp_cache_ttl: int = 300  # 5 minutes - refresh more frequently for faster IP updates

# Scan timeout configuration
NMAP_SCAN_TIMEOUT_BUFFER: int = 30  # Extra seconds to wait for nmap scan completion

# Background scan state
_scan_thread: Optional[threading.Thread] = None
_scan_status: Dict[str, str] = {}  # key (vmid or composite) -> status message
_scan_in_progress: bool = False
_scan_lock = threading.Lock()

# RDP hosts cache (IPs with port 3389 open)
_rdp_hosts_cache: set = set()
_rdp_hosts_cache_time: float = 0


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
                        raw_mac = parts[lladdr_idx + 1]
                        mac_normalized = raw_mac.lower().replace(':', '').replace('-', '')
                        if len(mac_normalized) == 12:
                            arp_map[mac_normalized] = ip
                        else:
                            pass
                    except (ValueError, IndexError):
                        continue
            
            return arp_map
    
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    
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
            
            # Parse output like:
            # ? (192.168.1.100) at 52:54:00:12:34:56 [ether] on eth0
            for line in result.stdout.splitlines():
                ip_match = re.search(r'\((\d+\.\d+\.\d+\.\d+)\)', line)
                mac_match = re.search(r'([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})', line)
                
                if ip_match and mac_match:
                    ip = ip_match.group(1)
                    mac = mac_match.group(0).lower().replace(':', '').replace('-', '')
                    arp_map[mac] = ip
            
            return arp_map
        
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
    
    # Method 3: /proc/net/arp (Linux fallback)
    try:
        with open('/proc/net/arp', 'r') as f:
            lines = f.readlines()
            
            # Skip header line
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    ip = parts[0]
                    mac = parts[3].lower().replace(':', '').replace('-', '')
                    if len(mac) == 12 and mac != '000000000000':
                        arp_map[mac] = ip
        
        return arp_map
    
    except FileNotFoundError:
        pass
    except Exception:
        pass
    
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
            
            # Return true even if ping fails - the attempt might still populate ARP
            # Some systems don't allow broadcast ping but it still triggers ARP
            return True
            
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            continue
    
    return False


def _is_root() -> bool:
    """Check if we're running with root privileges."""
    return os.geteuid() == 0


def parallel_ping_sweep(subnet_cidr: str, timeout_ms: int = 300, max_workers: int = 300, check_rdp: bool = False, needed_macs: Optional[set] = None) -> tuple:
    """
    Ping every IP in a subnet in parallel to populate ARP table.
    Optionally check RDP port (3389) on responding hosts in parallel.
    Stops early if all needed MACs are found in ARP table.
    
    Args:
        subnet_cidr: CIDR notation (e.g., "10.220.8.0/21")
        timeout_ms: Timeout per ping in milliseconds
        max_workers: Number of concurrent pings
        check_rdp: If True, check port 3389 on alive hosts (in parallel after ping)
        needed_macs: Optional set of MAC addresses we're looking for (stops early when all found)
    
    Returns:
        Tuple of (alive_count, rdp_hosts_set) where rdp_hosts_set contains IPs with RDP open
    """
    import socket
    
    try:
        network = ipaddress.ip_network(subnet_cidr, strict=False)
        total_hosts = network.num_addresses - 2  # Exclude network and broadcast
        
        start_time = time.time()
        
        def ping_host(ip: str) -> bool:
            """Ping a host. Returns True if alive."""
            try:
                # Use -c 1 (one packet), -W timeout (in seconds), -n (numeric, no DNS)
                timeout_sec = max(1, timeout_ms // 1000)
                result = subprocess.run(
                    ['ping', '-c', '1', '-W', str(timeout_sec), '-n', str(ip)],
                    capture_output=True,
                    timeout=timeout_sec + 1
                )
                return result.returncode == 0
            except:
                return False
        
        def check_rdp_port(ip: str) -> bool:
            """Check if RDP port 3389 is open. Returns True if open."""
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                rdp_result = sock.connect_ex((ip, 3389))
                sock.close()
                return rdp_result == 0
            except:
                return False
        
        # Ping all IPs in parallel
        alive_hosts = []
        checked_count = 0
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all ping jobs
            future_to_ip = {
                executor.submit(ping_host, str(ip)): str(ip) 
                for ip in network.hosts()
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_ip):
                ip = future_to_ip[future]
                is_alive = future.result()
                checked_count += 1
                
                if is_alive:
                    alive_hosts.append(ip)
                
                # Early exit if we've found all needed MACs
                if needed_macs and checked_count % 50 == 0:  # Check every 50 hosts to avoid overhead
                    current_arp = get_arp_table()
                    found_macs = needed_macs.intersection(current_arp.keys())
                    if len(found_macs) == len(needed_macs):
                        # Cancel remaining futures
                        for f in future_to_ip.keys():
                            f.cancel()
                        break
        
        alive_count = len(alive_hosts)
        rdp_hosts = set()
        
        # If RDP check requested, scan all alive hosts in parallel
        if check_rdp and alive_hosts:
            rdp_start = time.time()
            
            with ThreadPoolExecutor(max_workers=min(100, alive_count)) as rdp_executor:
                # Submit all RDP checks in parallel
                rdp_futures = {
                    rdp_executor.submit(check_rdp_port, ip): ip 
                    for ip in alive_hosts
                }
                
                # Collect RDP check results
                for future in as_completed(rdp_futures):
                    ip = rdp_futures[future]
                    has_rdp = future.result()
                    if has_rdp:
                        rdp_hosts.add(ip)
            
            rdp_duration = time.time() - rdp_start
        
        elapsed = time.time() - start_time
        
        return (alive_count, rdp_hosts)
        
    except Exception:
        return (0, set())


def scan_network_range(subnet_cidr: str = "10.220.8.0/21", timeout: int = 3) -> bool:
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
        timeout: Scan timeout in seconds (default: 3)
    
    Returns:
        True if scan succeeded (nmap ran without error)
    """
    is_root = _is_root()
    
    # Determine nmap arguments based on privileges
    # Use -T4 (aggressive but not insane) for better reliability
    # Add --min-rate and --max-rate to control scan speed
    if is_root:
        # ARP ping (-PR) is most reliable but requires root
        # Increased host timeout to 2s to ensure hosts have time to respond
        # Use --min-rate 50 to ensure scan doesn't complete instantly
        nmap_args = ['-sn', '-PR', '-T4', '--min-rate', '50', '--max-rate', '300', 
                    '--max-retries', '2', '--host-timeout', '3s', subnet_cidr]
        scan_type = "ARP ping (root)"
    else:
        # Unprivileged: use standard ping scan without -PR
        # -sn does ICMP echo, TCP SYN to 443, TCP ACK to 80, ICMP timestamp
        nmap_args = ['-sn', '-T4', '--min-rate', '50', '--max-rate', '300',
                    '--max-retries', '2', '--host-timeout', '3s', subnet_cidr]
        scan_type = "ICMP/TCP ping (unprivileged)"
    
    # Try nmap with different paths
    for nmap_cmd in ['/usr/bin/nmap', '/usr/local/bin/nmap', 'nmap']:
        try:
            scan_timeout = timeout + NMAP_SCAN_TIMEOUT_BUFFER
            
            start_time = time.time()
            
            result = subprocess.run(
                [nmap_cmd] + nmap_args,
                capture_output=True,
                text=True,
                timeout=scan_timeout
            )
            
            elapsed = time.time() - start_time
            
            if result.returncode != 0:
                # Check if it's a privilege error for -PR (nmap exits with code 1)
                # We check stderr for common privilege-related messages
                stderr_lower = (result.stderr or "").lower()
                is_privilege_error = (
                    result.returncode == 1 and 
                    any(msg in stderr_lower for msg in [
                        "requires root",
                        "operation not permitted",
                        "permission denied",
                        "raw socket",
                    ])
                )
                if is_privilege_error:
                    # Retry without -PR
                    result = subprocess.run(
                        [nmap_cmd, '-sn', '-T4', '--max-retries', '1', '--host-timeout', '2s', subnet_cidr],
                        capture_output=True,
                        text=True,
                        timeout=scan_timeout
                    )
            
            return True
            
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            continue
    
    
    # Fallback: use fping if available
    for fping_cmd in ['/usr/bin/fping', '/usr/sbin/fping', 'fping']:
        try:
            result = subprocess.run(
                [fping_cmd, '-a', '-g', '-r', '1', subnet_cidr],
                capture_output=True,
                text=True,
                timeout=timeout + 5
            )
            return True
        except FileNotFoundError:
            continue
        except Exception:
            continue
    
    return False


def has_rdp_port_open(ip: str) -> bool:
    """
    Check if an IP has RDP port (3389) open based on scan cache.
    
    Args:
        ip: IP address to check
    
    Returns:
        True if IP is in the RDP hosts cache, False otherwise
    """
    global _rdp_hosts_cache, _rdp_hosts_cache_time
    result = ip in _rdp_hosts_cache
    
    return result


def get_rdp_cache_time() -> float:
    """
    Get the timestamp of the last RDP port scan.
    
    Returns:
        Timestamp of last scan, or 0 if no scan has completed
    """
    global _rdp_hosts_cache_time
    return _rdp_hosts_cache_time


def invalidate_arp_cache():
    """
    Invalidate the ARP cache to force a fresh scan on next request.
    Call this after VM operations (start/stop/restart) to ensure IPs are updated.
    """
    global _arp_cache_time, _rdp_hosts_cache_time
    _arp_cache_time = 0
    _rdp_hosts_cache_time = 0


def get_scan_status(vmid: int) -> Optional[str]:
    """
    Get the current scan status for a VM.
    
    Args:
        vmid: VM ID
    
    Returns:
        Status message or None
    """
    return _scan_status.get(vmid)


def _background_scan_worker(vm_mac_map: Dict[str, str], subnets: Optional[List[str]] = None):
    """
    Background worker that performs network scan and updates IP cache.
    
    Args:
        vm_mac_map: Dict mapping vmid to MAC address
        subnets: Optional list of broadcast addresses for fallback
    """
    global _scan_in_progress, _scan_status, _arp_cache, _arp_cache_time
    
    try:
        
        # Update status for all VMs
        with _scan_lock:
            for key in vm_mac_map.keys():
                _scan_status[key] = "Scanning network..."
        
        # Try parallel ping sweep first (most reliable for ARP population)
        needed_macs = set(vm_mac_map.values())  # Set of MACs we're looking for
        alive_count, rdp_hosts = parallel_ping_sweep("10.220.8.0/21", timeout_ms=300, max_workers=300, check_rdp=True, needed_macs=needed_macs)
        
        if alive_count == 0:
            # Fallback to nmap if parallel ping found nothing
            scan_success = scan_network_range("10.220.8.0/21", timeout=1)
        
        # Get updated ARP table
        arp_table = get_arp_table()
        
        # Cache RDP hosts globally
        global _rdp_hosts_cache, _rdp_hosts_cache_time
        with _scan_lock:
            _rdp_hosts_cache = rdp_hosts
            _rdp_hosts_cache_time = time.time()
        
        # Match VMs to IPs and update cache + status
        found_count = 0
        with _scan_lock:
            for key, mac in vm_mac_map.items():
                if mac in arp_table:
                    ip = arp_table[mac]
                    _arp_cache[mac] = ip
                    _scan_status[key] = ip
                    found_count += 1
                else:
                    _scan_status[key] = "Not found in ARP"
            
            _arp_cache_time = time.time()
        
    except Exception as e:
        with _scan_lock:
            for key in vm_mac_map.keys():
                _scan_status[key] = f"Scan error: {e}"
    
    finally:
        with _scan_lock:
            global _scan_in_progress
            _scan_in_progress = False


def discover_ips_via_arp(vm_mac_map: Dict[str, str], subnets: Optional[List[str]] = None, background: bool = True, force_refresh: bool = False) -> Dict[str, str]:
    """
    Discover VM IPs using network scan + ARP lookup.
    
    Args:
        vm_mac_map: Dict mapping vmid to MAC address (lowercase, no separators)
        subnets: List of broadcast addresses to ping (e.g., ["192.168.1.255", "10.0.0.255"])
                 Note: Now used as fallback; primary method is network range scan
        background: If True, run scan in background thread and return immediately with cached results
        force_refresh: If True, ignore cache and force a new scan (used after VM operations)
    
    Returns:
        Dict mapping vmid to discovered IP address (from cache if background=True)
    """
    global _scan_thread, _scan_in_progress, _arp_cache, _arp_cache_time
    
    if not vm_mac_map:
        return {}
    
    # Check if cache is still valid (5 minute TTL)
    cache_age = time.time() - _arp_cache_time
    cache_valid = cache_age < _arp_cache_ttl and not force_refresh
    
    
    # Check if we already have matches in cache
    vm_ips: Dict[str, str] = {}
    if cache_valid:
        for key, mac in vm_mac_map.items():
            if mac in _arp_cache:
                vm_ips[key] = _arp_cache[mac]
    
    # If we found all IPs and cache is valid, no need to scan at all
    if len(vm_ips) == len(vm_mac_map) and cache_valid:
        return vm_ips
    
    # If background mode, start scan thread ONLY if cache is expired or force refresh
    # DO NOT start scan just because some IPs are missing - respect the cache TTL!
    if background:
        # Only start a new scan if:
        # 1. Cache is expired (older than TTL) OR force_refresh is True
        # 2. AND we're not already scanning
        should_scan = (not cache_valid) and not _scan_in_progress
        
        if should_scan:
            with _scan_lock:
                if not _scan_in_progress:
                    _scan_in_progress = True
                    _scan_thread = threading.Thread(
                        target=_background_scan_worker,
                        args=(vm_mac_map, subnets),
                        daemon=True
                    )
                    _scan_thread.start()
                    
                    # Set initial status for VMs without IPs
                    for key in vm_mac_map.keys():
                        if key not in vm_ips:
                            _scan_status[key] = "Scan starting..."
        
        return vm_ips  # Return whatever we have cached (even if incomplete)
    
    # Synchronous mode (old behavior)
    
    # Try parallel ping sweep first (most reliable for ARP population)
    needed_macs = set(vm_mac_map.values())  # Set of MACs we're looking for
    alive_count, rdp_hosts = parallel_ping_sweep("10.220.8.0/21", timeout_ms=300, max_workers=300, check_rdp=True, needed_macs=needed_macs)
    
    # Update RDP hosts cache
    global _rdp_hosts_cache, _rdp_hosts_cache_time
    _rdp_hosts_cache = rdp_hosts
    _rdp_hosts_cache_time = time.time()
    
    if alive_count == 0:
        # Fallback to nmap if parallel ping found nothing
        scan_success = scan_network_range("10.220.8.0/21", timeout=5)
    
    # Get updated ARP table
    arp_table = get_arp_table()
    
    # Map remaining VM MACs to IPs
    for key, mac in vm_mac_map.items():
        if key in vm_ips:
            continue  # Already found
        if mac in arp_table:
            vm_ips[key] = arp_table[mac]
        else:
            pass
    
    return vm_ips


def verify_single_ip(ip: str, expected_mac: str, timeout: int = 2) -> bool:
    """
    Fast verification: ping a single IP and check if ARP table MAC matches expected MAC.
    
    Args:
        ip: IP address to verify
        expected_mac: Expected MAC address (will be normalized)
        timeout: Ping timeout in seconds
    
    Returns:
        True if IP is reachable and MAC matches, False otherwise
    """
    if not ip or not expected_mac:
        return False
    
    # Normalize expected MAC
    expected_mac_normalized = normalize_mac(expected_mac)
    if not expected_mac_normalized:
        return False
    
    # Try to ping the IP to populate ARP table
    for ping_cmd in ['/usr/bin/ping', '/bin/ping', 'ping']:
        try:
            subprocess.run(
                [ping_cmd, '-c', '1', '-W', str(timeout), ip],
                capture_output=True,
                text=True,
                timeout=timeout + 1
            )
            # Don't care if ping succeeds - just need ARP entry
            break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        except Exception:
            break
    
    # Check ARP table for this IP
    arp_table = get_arp_table()
    actual_mac = None
    
    # Find MAC for this IP (reverse lookup)
    for mac, arp_ip in arp_table.items():
        if arp_ip == ip:
            actual_mac = mac
            break
    
    # Compare MACs
    if actual_mac and actual_mac == expected_mac_normalized:
        return True
    
    return False


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
    
    print("Testing ARP scanner...")
    print("\n1. Getting current ARP table:")
    arp = get_arp_table()
    for mac, ip in list(arp.items())[:5]:  # Show first 5 entries
        print(f"  {mac} -> {ip}")
    
    print(f"\nTotal ARP entries: {len(arp)}")
