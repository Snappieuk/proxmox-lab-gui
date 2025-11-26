#!/usr/bin/env python3
"""
Fast IP discovery using broadcast ping + ARP table lookup.
Maps MAC addresses from Proxmox VMs to IPs discovered on the network.
"""

import subprocess
import re
import logging
from typing import Dict, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


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
            
            logger.debug("Broadcast ping to %s completed with code %d", subnet, result.returncode)
            return True
            
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            logger.warning("Broadcast ping timed out")
            return False
        except Exception as e:
            logger.debug("Broadcast ping with %s failed: %s", ping_cmd, e)
            continue
    
    logger.warning("All ping commands failed or not found")
    return False


def discover_ips_via_arp(vm_mac_map: Dict[int, str], subnets: Optional[list] = None) -> Dict[int, str]:
    """
    Discover VM IPs using broadcast ping + ARP lookup.
    
    Args:
        vm_mac_map: Dict mapping vmid to MAC address (lowercase, no separators)
        subnets: List of broadcast addresses to ping (e.g., ["192.168.1.255", "10.0.0.255"])
    
    Returns:
        Dict mapping vmid to discovered IP address
    """
    if not vm_mac_map:
        return {}
    
    logger.info("VM MAC map: %s", vm_mac_map)
    
    # Default subnets if none provided
    if subnets is None:
        subnets = ["10.220.15.255"]  # Default to 10.220.8.0/21 network
    
    # Get existing ARP table first (might already have entries)
    arp_table = get_arp_table()
    logger.info("Pre-ping ARP table has %d entries", len(arp_table))
    
    # Check if we already have matches before pinging
    vm_ips = {}
    for vmid, mac in vm_mac_map.items():
        if mac in arp_table:
            vm_ips[vmid] = arp_table[mac]
            logger.info("VM %d (MAC %s) -> IP %s (cached)", vmid, mac, arp_table[mac])
    
    # If we found all IPs, no need to ping
    if len(vm_ips) == len(vm_mac_map):
        logger.info("All IPs found in existing ARP cache, skipping broadcast ping")
        return vm_ips
    
    # Broadcast ping to populate ARP tables (only if needed)
    logger.info("Broadcasting ping to %d subnets", len(subnets))
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
