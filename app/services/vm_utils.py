#!/usr/bin/env python3
"""
VM Utilities - Common helper functions for VM operations.

This module provides utility routines for:
- VM naming sanitization (DNS-style naming)
- Next available VMID lookup
- MAC address extraction from VM config
- Optimal node selection based on resource availability

These functions are used by vm_core.py, vm_template.py, class_vm_service.py,
and other modules that work with Proxmox VMs.

Note: This module does NOT touch VMInventory/background sync - those remain
in inventory_service.py and background_sync.py.
"""

import logging
import re
from typing import Optional, Set

logger = logging.getLogger(__name__)

# Import VMInventory model for VMID lookup
from app.models import VMInventory  # noqa: E402 - import after logger setup


def get_optimal_node(ssh_executor, proxmox=None, vm_memory_mb=2048, simulated_vms_per_node=None) -> str:
    """
    Find the optimal Proxmox node for VM creation based on resource availability.
    
    Selects node with most available RAM and CPU capacity, accounting for
    VMs that will be created in the same batch (simulated weighting).
    
    Args:
        ssh_executor: SSH executor for running commands
        proxmox: Optional ProxmoxAPI connection (if None, uses SSH to query)
        vm_memory_mb: Memory per VM in MB (for simulated weighting)
        simulated_vms_per_node: Dict of {node_name: count} for VMs being created
        
    Returns:
        Node name with most available resources
    """
    try:
        # Track simulated VM allocations if not provided
        if simulated_vms_per_node is None:
            simulated_vms_per_node = {}
            
        # Try to use Proxmox API if available
        if proxmox:
            nodes = proxmox.nodes.get()
            best_node = None
            best_score = -1
            
            for node in nodes:
                if node.get('status') != 'online':
                    continue
                
                node_name = node['node']
                    
                # Calculate availability score (higher is better)
                # Weight RAM more heavily than CPU (RAM is usually the bottleneck)
                try:
                    mem_total = float(node.get('maxmem', 1))
                    mem_used = float(node.get('mem', 0))
                except (ValueError, TypeError):
                    logger.debug(f"Node {node_name}: Invalid memory values, skipping")
                    continue
                
                # Add simulated memory usage from VMs being created
                simulated_count = simulated_vms_per_node.get(node_name, 0)
                simulated_mem = simulated_count * vm_memory_mb * 1024 * 1024  # Convert MB to bytes
                mem_used += simulated_mem
                
                mem_free_pct = (mem_total - mem_used) / mem_total if mem_total > 0 else 0
                
                try:
                    cpu_total = float(node.get('maxcpu', 1))
                    cpu_used = float(node.get('cpu', 0))
                except (ValueError, TypeError):
                    logger.debug(f"Node {node_name}: Invalid CPU values, using defaults")
                    cpu_total = 1
                    cpu_used = 0
                    
                cpu_free_pct = (1 - cpu_used) if cpu_used < 1 else 0
                
                # Score: 70% RAM + 30% CPU
                score = (mem_free_pct * 0.7) + (cpu_free_pct * 0.3)
                
                simulated_note = f" (+{simulated_count} simulated VMs)" if simulated_count > 0 else ""
                logger.debug(f"Node {node_name}: {mem_free_pct*100:.1f}% RAM free, {cpu_free_pct*100:.1f}% CPU free, score={score:.3f}{simulated_note}")
                
                if score > best_score:
                    best_score = score
                    best_node = node_name
            
            if best_node:
                logger.info(f"Selected optimal node: {best_node} (score={best_score:.3f})")
                # Increment simulated count for next VM
                simulated_vms_per_node[best_node] = simulated_vms_per_node.get(best_node, 0) + 1
                return best_node
            
            # If no online nodes found via API, try to get first available node
            if nodes:
                first_node = nodes[0].get('node')
                if first_node:
                    logger.warning(f"No optimal node found, using first available: {first_node}")
                    return first_node
        
        # Fallback: use hostname command
        exit_code, stdout, stderr = ssh_executor.execute("hostname", timeout=10)
        if exit_code == 0 and stdout.strip():
            node = stdout.strip()
            logger.info(f"Using connected node: {node}")
            return node
            
    except Exception as e:
        logger.warning(f"Failed to determine optimal node: {e}")
    
    # Ultimate fallback - try hostname again
    try:
        exit_code, stdout, stderr = ssh_executor.execute("hostname", timeout=10)
        if exit_code == 0 and stdout.strip():
            node = stdout.strip()
            logger.warning(f"Could not determine optimal node via API, using hostname: {node}")
            return node
    except:
        pass
    
    logger.error("Could not determine any valid node - this should not happen!")
    raise RuntimeError("No valid Proxmox node could be determined for VM creation")


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
    Find the next available VMID on the Proxmox cluster.
    
    Queries VMInventory database first (fastest), then falls back to cluster query.
    
    Args:
        ssh_executor: SSH executor for running commands (from ssh_executor.py)
        start: Starting VMID to search from (default: 200)
        
    Returns:
        Next available VMID
        
    Raises:
        RuntimeError: If unable to find available VMID after max attempts
    """
    # Query VMInventory database for all used VMIDs (fastest method)
    try:
        used_vmids = {vm.vmid for vm in VMInventory.query.with_entities(VMInventory.vmid).all()}
        logger.debug(f"Found {len(used_vmids)} VMIDs in use from VMInventory database")
    except Exception as e:
        logger.warning(f"Failed to query VMInventory: {e}, falling back to cluster query")
        
        # Fallback: Query cluster directly via pvesh
        exit_code, stdout, stderr = ssh_executor.execute(
            "pvesh get /cluster/resources --type vm --output-format json",
            check=False,
            timeout=30
        )
        
        used_vmids = set()
        if exit_code == 0 and stdout.strip():
            try:
                import json
                resources = json.loads(stdout)
                used_vmids = {int(r['vmid']) for r in resources if 'vmid' in r}
                logger.debug(f"Found {len(used_vmids)} VMIDs in use from cluster query")
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.warning(f"Failed to parse cluster resources: {e}")
                used_vmids = set()
    
    # Find next available VMID starting from 'start'
    vmid = start
    max_attempts = 1000
    
    for _ in range(max_attempts):
        if vmid not in used_vmids:
            # Found available VMID
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


def get_vm_mac_address_ssh(ssh_executor, vmid: int, node: Optional[str] = None) -> Optional[str]:
    """Get the MAC address of a VM's first network interface via SSH.
    
    Parses the output of VM config to extract MAC address from net0 line.
    Uses cluster-aware pvesh command if node is not specified.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID
        node: Optional node name - if provided, uses node-specific qm config,
              otherwise uses cluster-aware pvesh
        
    Returns:
        MAC address string (uppercase, colon-separated) or None if not found
    """
    try:
        # First, find which node the VM is on if not specified
        if not node:
            # Use cluster-wide query to find the VM's node
            exit_code, stdout, stderr = ssh_executor.execute(
                "pvesh get /cluster/resources --type vm --output-format json",
                check=False,
                timeout=10
            )
            if exit_code == 0 and stdout:
                import json
                try:
                    resources = json.loads(stdout)
                    for resource in resources:
                        if resource.get('vmid') == vmid:
                            node = resource.get('node')
                            break
                except Exception:
                    pass
        
        # Use cluster-aware pvesh command to get VM config
        if node:
            command = f"pvesh get /nodes/{node}/qemu/{vmid}/config"
        else:
            # Fallback to qm config (may fail if VM is on different node)
            command = f"qm config {vmid}"
        
        exit_code, stdout, stderr = ssh_executor.execute(
            command,
            check=False,
            timeout=10
        )
        
        if exit_code == 0 and stdout:
            # Look for net0 line and extract MAC
            # Format can be: "net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0"
            # Or with generated MAC: "net0: virtio=12:34:56:78:9A:BC,bridge=vmbr0,firewall=1"
            for line in stdout.split('\n'):
                if line.startswith('net0:') or line.strip().startswith('"net0"'):
                    # Extract everything after 'net0:'
                    if ':' in line:
                        net_config = line.split(':', 1)[1].strip()
                    else:
                        net_config = line
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
    - "TRUENAS-NFS:100/vm-100-disk-0.qcow2,size=32G,cache=writeback,discard=on"
    - "local-lvm:vm-100-disk-0,size=32G,iothread=1,ssd=1"
    
    Args:
        disk_line: Disk configuration string
        
    Returns:
        Dict with keys: storage, volume, size_str, size_gb, format, and ALL other options
    """
    result = {
        'storage': None,
        'volume': None,
        'size_str': None,
        'size_gb': None,
        'format': None,
        'options': {},  # Store all extra options
    }
    
    if not disk_line:
        return result
    
    # Split on comma to separate volume from options
    parts = disk_line.split(',')
    volume_part = parts[0].strip()
    
    # Parse storage:volume (handle both "storage:volume" and "storage: volume")
    if ':' in volume_part:
        storage, volume = volume_part.split(':', 1)
        result['storage'] = storage.strip()
        result['volume'] = volume.strip()
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
    
    # Parse ALL options from remaining parts
    for part in parts[1:]:
        part = part.strip()
        if '=' in part:
            key, value = part.split('=', 1)
            key = key.strip()
            value = value.strip()
            
            if key == 'size':
                result['size_str'] = value.upper()
                # Convert to GB
                try:
                    if 'G' in value.upper():
                        result['size_gb'] = float(value.upper().replace('G', ''))
                    elif 'M' in value.upper():
                        result['size_gb'] = float(value.upper().replace('M', '')) / 1024
                    elif 'T' in value.upper():
                        result['size_gb'] = float(value.upper().replace('T', '')) * 1024
                except ValueError:
                    pass
            else:
                # Store ALL other options (cache, discard, iothread, ssd, backup, etc.)
                result['options'][key] = value
    
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
