#!/usr/bin/env python3
"""
VM Utilities - Common helper functions for VM operations.

This module provides utility routines for:
- VM naming sanitization (DNS-style naming)
- Next available VMID lookup
- MAC address extraction from VM config

These functions are used by vm_core.py, vm_template.py, class_vm_service.py,
and other modules that work with Proxmox VMs.

Note: This module does NOT touch VMInventory/background sync - those remain
in inventory_service.py and background_sync.py.
"""

import logging
import re
from typing import Optional, Set

logger = logging.getLogger(__name__)


def sanitize_vm_name(name: str, fallback: str = "vm") -> str:
    """Sanitize a proposed VM name to satisfy Proxmox (DNS-style) constraints.

    Rules applied (approx RFC 1123 hostname subset):
    - Lowercase all characters
    - Allow only a-z, 0-9, hyphen; replace others with '-'
    - Collapse consecutive hyphens
    - Trim leading/trailing hyphens
    - Ensure starts/ends with alphanumeric (prepend/append fallback tokens if needed)
    - Limit length to 63 chars
    - If result empty, use fallback
    
    Args:
        name: Proposed VM name
        fallback: Fallback name if sanitization produces empty result
        
    Returns:
        Sanitized VM name safe for Proxmox
    """
    if not isinstance(name, str):
        name = str(name) if name is not None else ""
    original = name
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name)
    name = name.strip('-')
    
    # Ensure we have at least the fallback
    if not name:
        name = fallback
    
    # Safety check: ensure name is non-empty before character checks
    if not name:
        name = "vm"  # Ultimate fallback
    
    # Ensure starts with alphanumeric
    if not name[0].isalnum():
        name = f"{fallback}-{name}" if name else fallback
    
    # Ensure ends with alphanumeric (re-check in case above changed things)
    if name and not name[-1].isalnum():
        name = f"{name}0"
    
    # Truncate to 63 characters
    if len(name) > 63:
        name = name[:63].rstrip('-') or fallback
    if name != original:
        logger.debug(f"sanitize_vm_name: '{original}' -> '{name}'")
    return name


def get_next_available_vmid_ssh(ssh_executor, start: int = 200) -> int:
    """
    Find the next available VMID on the Proxmox cluster via SSH.
    
    Queries the cluster using pvesh or falls back to checking qm status.
    
    Args:
        ssh_executor: SSH executor for running commands (from ssh_executor.py)
        start: Starting VMID to search from (default: 200)
        
    Returns:
        Next available VMID
        
    Raises:
        RuntimeError: If unable to find available VMID after max attempts
    """
    # First try pvesh to get next available VMID
    exit_code, stdout, stderr = ssh_executor.execute(
        f"pvesh get /cluster/nextid --vmid {start}",
        check=False
    )
    
    if exit_code == 0 and stdout.strip().isdigit():
        return int(stdout.strip())
    
    # Fallback: increment and check qm status
    vmid = start
    max_attempts = 1000
    
    for _ in range(max_attempts):
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm status {vmid}",
            check=False,
            timeout=10
        )
        if exit_code != 0:
            # VMID not in use (qm returns non-zero for non-existent VM)
            return vmid
        vmid += 1
    
    raise RuntimeError(f"Could not find available VMID after {max_attempts} attempts starting from {start}")


def get_next_available_vmid_api(proxmox, start: int = 200, used_vmids: Set[int] = None) -> int:
    """
    Find the next available VMID via Proxmox API.
    
    Optionally accepts a set of already-known used VMIDs to avoid
    redundant API queries when batch-creating VMs.
    
    Args:
        proxmox: ProxmoxAPI client instance
        start: Starting VMID to search from
        used_vmids: Optional set of VMIDs already known to be in use
        
    Returns:
        Next available VMID
    """
    if used_vmids is None:
        # Query cluster resources to get all used VMIDs
        used_vmids = set()
        try:
            resources = proxmox.cluster.resources.get(type="vm")
            for r in resources:
                vmid = r.get("vmid")
                if vmid is not None:
                    used_vmids.add(int(vmid))
        except Exception as e:
            logger.warning(f"Failed to query cluster resources, falling back to per-node: {e}")
            # Fallback to per-node query
            for node_info in proxmox.nodes.get():
                node_name = node_info.get("node")
                if not node_name:
                    continue
                try:
                    for vm in proxmox.nodes(node_name).qemu.get():
                        vmid = vm.get("vmid")
                        if vmid is not None:
                            used_vmids.add(int(vmid))
                    for ct in proxmox.nodes(node_name).lxc.get():
                        vmid = ct.get("vmid")
                        if vmid is not None:
                            used_vmids.add(int(vmid))
                except Exception:
                    pass
    
    # Find next available
    vmid = start
    while vmid in used_vmids:
        vmid += 1
    
    return vmid


def get_vm_mac_address_ssh(ssh_executor, vmid: int) -> Optional[str]:
    """Get the MAC address of a VM's first network interface via SSH.
    
    Parses the output of `qm config` to extract MAC address from net0 line.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID
        
    Returns:
        MAC address string (uppercase, colon-separated) or None if not found
    """
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            f"qm config {vmid}",
            check=False,
            timeout=10
        )
        
        if exit_code == 0 and stdout:
            # Look for net0 line and extract MAC
            # Format can be: "net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0"
            # Or with generated MAC: "net0: virtio=12:34:56:78:9A:BC,bridge=vmbr0,firewall=1"
            for line in stdout.split('\n'):
                if line.startswith('net0:'):
                    # Extract everything after 'net0:'
                    net_config = line.split(':', 1)[1].strip()
                    # Look for MAC address pattern (XX:XX:XX:XX:XX:XX)
                    mac_match = re.search(
                        r'([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})',
                        net_config
                    )
                    if mac_match:
                        return mac_match.group(1).upper()
        
        return None
    except Exception as e:
        logger.warning(f"Failed to get MAC address for VM {vmid}: {e}")
        return None


def get_vm_mac_address_api(proxmox, node: str, vmid: int, vmtype: str = "qemu") -> Optional[str]:
    """Get the MAC address of a VM's first network interface via Proxmox API.
    
    Checks net0-net9 interfaces for the first valid MAC address.
    
    Args:
        proxmox: ProxmoxAPI client instance
        node: Proxmox node name
        vmid: VM ID
        vmtype: VM type ('qemu' or 'lxc')
        
    Returns:
        MAC address string (uppercase, colon-separated) or None if not found
    """
    try:
        if vmtype == "lxc":
            config = proxmox.nodes(node).lxc(vmid).config.get()
        else:
            config = proxmox.nodes(node).qemu(vmid).config.get()
        
        if not config:
            return None
        
        # Check net0-net9
        for i in range(10):
            net_key = f"net{i}"
            net_config = config.get(net_key, "")
            if not net_config:
                continue
            
            if vmtype == "lxc":
                # LXC format: "name=eth0,bridge=vmbr0,hwaddr=XX:XX:XX:XX:XX:XX,ip=dhcp"
                mac_match = re.search(r'hwaddr=([0-9a-fA-F:]+)', net_config)
            else:
                # QEMU format: "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
                # Match MAC address pattern (6 hex pairs separated by colons)
                mac_match = re.search(r'([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})', net_config)
            
            if mac_match:
                raw_mac = mac_match.group(0)
                # Normalize: uppercase and colon-separated
                mac = raw_mac.upper().replace('-', ':')
                return mac
        
        return None
    except Exception as e:
        logger.debug(f"Failed to get MAC for {vmtype}/{vmid}: {e}")
        return None


def normalize_mac_address(mac: str) -> Optional[str]:
    """Normalize MAC address to lowercase without separators.
    
    Args:
        mac: MAC address in any format (colons, hyphens, etc.)
        
    Returns:
        Normalized MAC (lowercase, no separators) or None if invalid
    """
    if not mac:
        return None
    # Remove common separators and lowercase
    normalized = mac.lower().replace(':', '').replace('-', '').replace('.', '')
    if len(normalized) != 12:
        return None
    # Validate hex characters
    try:
        int(normalized, 16)
        return normalized
    except ValueError:
        return None


def format_mac_address(mac: str, separator: str = ':') -> Optional[str]:
    """Format normalized MAC address with specified separator.
    
    Args:
        mac: MAC address (normalized or raw)
        separator: Separator to use (default: ':')
        
    Returns:
        Formatted MAC or None if invalid
    """
    normalized = normalize_mac_address(mac)
    if not normalized:
        return None
    # Insert separator every 2 chars
    parts = [normalized[i:i+2] for i in range(0, 12, 2)]
    return separator.join(parts).upper()


def parse_disk_config(disk_line: str) -> dict:
    """Parse a Proxmox disk configuration line.
    
    Handles formats like:
    - "TRUENAS-NFS:100/vm-100-disk-0.qcow2,size=32G"
    - "local-lvm:vm-100-disk-0,size=32G"
    
    Args:
        disk_line: Disk configuration string
        
    Returns:
        Dict with keys: storage, volume, size_str, size_gb (may be None)
    """
    result = {
        'storage': None,
        'volume': None,
        'size_str': None,
        'size_gb': None,
        'format': None,
    }
    
    if not disk_line:
        return result
    
    # Split on comma to separate volume from options
    parts = disk_line.split(',')
    volume_part = parts[0].strip()
    
    # Parse storage:volume
    if ':' in volume_part:
        result['storage'], result['volume'] = volume_part.split(':', 1)
    else:
        result['volume'] = volume_part
    
    # Detect format from volume name
    if result['volume']:
        if '.qcow2' in result['volume']:
            result['format'] = 'qcow2'
        elif '.raw' in result['volume']:
            result['format'] = 'raw'
        elif '.vmdk' in result['volume']:
            result['format'] = 'vmdk'
    
    # Parse size from options
    for part in parts[1:]:
        part = part.strip()
        if part.startswith('size='):
            result['size_str'] = part.split('=')[1].upper()
            # Convert to GB
            size_str = result['size_str']
            try:
                if 'G' in size_str:
                    result['size_gb'] = float(size_str.replace('G', ''))
                elif 'M' in size_str:
                    result['size_gb'] = float(size_str.replace('M', '')) / 1024
                elif 'T' in size_str:
                    result['size_gb'] = float(size_str.replace('T', '')) * 1024
            except ValueError:
                pass
    
    return result


def build_vm_name(prefix: str, suffix: str = None, index: int = None) -> str:
    """Build a standardized VM name from components.
    
    Args:
        prefix: Base name prefix (e.g., class name)
        suffix: Optional suffix (e.g., "teacher", "student")
        index: Optional index number
        
    Returns:
        Sanitized VM name
    """
    parts = [prefix]
    if suffix:
        parts.append(suffix)
    if index is not None:
        parts.append(str(index))
    
    name = '-'.join(parts)
    return sanitize_vm_name(name)
