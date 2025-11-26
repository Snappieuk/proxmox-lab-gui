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
    try:
        # Run arp -a to get the ARP table
        result = subprocess.run(
            ['arp', '-a'],
            capture_output=True,
            text=True,
            timeout=2
        )
        
        if result.returncode != 0:
            logger.warning("arp command failed: %s", result.stderr)
            return arp_map
        
        logger.debug("ARP table output:\n%s", result.stdout)
        
        # Parse output like:
        # ? (192.168.1.100) at 52:54:00:12:34:56 [ether] on eth0
        # or on some systems:
        # hostname (192.168.1.100) at 52:54:00:12:34:56 on en0 ifscope [ethernet]
        for line in result.stdout.splitlines():
            # Look for IP address and MAC address patterns
            ip_match = re.search(r'\((\d+\.\d+\.\d+\.\d+)\)', line)
            mac_match = re.search(r'([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})', line)
            
            if ip_match and mac_match:
                ip = ip_match.group(1)
                mac = mac_match.group(0).lower().replace(':', '').replace('-', '')
                arp_map[mac] = ip
                logger.debug("ARP entry: %s -> %s", mac, ip)
        
        logger.info("ARP table contains %d entries", len(arp_map))
        
    except subprocess.TimeoutExpired:
        logger.warning("arp command timed out")
    except Exception as e:
        logger.error("Failed to get ARP table: %s", e)
    
    return arp_map


def broadcast_ping(subnet: str = "192.168.1.255", count: int = 2) -> bool:
    """
    Send broadcast ping to populate ARP table.
    
    Args:
        subnet: Broadcast address (e.g., "192.168.1.255" or "10.220.15.255")
        count: Number of ping packets to send
    
    Returns:
        True if ping succeeded, False otherwise
    """
    try:
        # Use ping -b for broadcast (Linux)
        # -b: allow pinging broadcast address
        # -c: count of pings
        # -W: timeout in seconds
        result = subprocess.run(
            ['ping', '-b', '-c', str(count), '-W', '1', subnet],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        logger.debug("Broadcast ping to %s completed with code %d", subnet, result.returncode)
        return True
        
    except subprocess.TimeoutExpired:
        logger.warning("Broadcast ping timed out")
        return False
    except FileNotFoundError:
        logger.warning("ping command not found")
        return False
    except Exception as e:
        logger.error("Broadcast ping failed: %s", e)
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
        subnets = ["192.168.1.255", "10.220.15.255"]  # Common defaults
    
    # Broadcast ping to populate ARP tables
    for subnet in subnets:
        logger.info("Broadcasting ping to %s", subnet)
        broadcast_ping(subnet)
    
    # Get ARP table
    arp_table = get_arp_table()
    logger.info("ARP table has %d entries, looking for %d VM MACs", len(arp_table), len(vm_mac_map))
    
    # Map VM MACs to IPs
    vm_ips = {}
    for vmid, mac in vm_mac_map.items():
        if mac in arp_table:
            vm_ips[vmid] = arp_table[mac]
            logger.info("VM %d (MAC %s) -> IP %s", vmid, mac, arp_table[mac])
        else:
            logger.debug("VM %d (MAC %s) not found in ARP table", vmid, mac)
    
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
