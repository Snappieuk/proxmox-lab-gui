#!/usr/bin/env python3
"""
VM Template Management - Template and overlay generation logic.

This module provides template-related operations:
- Export template disks to QCOW2 base images
- Create student VMs as overlays from base images
- Full clone operations via SSH
- Template conversion and management

These are template-focused operations used by class_vm_service.py
and other high-level workflows.

Note: This module does NOT touch VMInventory/background sync - those remain
in inventory_service.py and background_sync.py.
"""

import logging
from typing import List, Optional, Tuple

from app.services.ssh_executor import SSHExecutor
from app.services.vm_core import (
    DEFAULT_VM_IMAGES_PATH,
    PROXMOX_STORAGE_NAME,
    attach_disk_to_vm,
    convert_disk_to_qcow2,
    create_overlay_disk,
    create_vm_shell,
    destroy_vm,
    get_vm_config_ssh,
    stop_vm,
    wait_for_vm_stopped,
)
from app.services.vm_utils import (
    build_vm_name,
    get_next_available_vmid_ssh,
    get_vm_mac_address_ssh,
    parse_disk_config,
    sanitize_vm_name,
)

logger = logging.getLogger(__name__)

# Module-level cache for template EFI/TPM status
# Prevents re-checking template config for every VM (reduces SSH commands)
# Key: template_vmid, Value: (has_efi, has_tpm)
_template_efi_tpm_cache = {}


# ---------------------------------------------------------------------------
# Template Export Operations
# ---------------------------------------------------------------------------

def export_template_to_qcow2(
    ssh_executor: SSHExecutor,
    template_vmid: int,
    node: str,
    output_path: str,
    disk: str = "scsi0",
    timeout: int = 600,
) -> Tuple[bool, str]:
    """
    Export a Proxmox template's disk to a standalone QCOW2 file.
    
    This creates a base QCOW2 file that can be used as a backing file
    for overlay disks. The output file is a complete, independent copy.
    
    Args:
        ssh_executor: SSH executor for running commands
        template_vmid: VMID of the source template
        node: Proxmox node name where template resides
        output_path: Full path for the output QCOW2 file
        disk: Disk to export (default: scsi0)
        timeout: Conversion timeout in seconds (default: 600)
        
    Returns:
        Tuple of (success, error_message, disk_controller_type, ostype, bios, machine, cpu, scsihw, memory, cores, sockets, efi_disk, tpm_state)
    """
    logger.info(f"Exporting template {template_vmid} to {output_path}")
    
    try:
        # Get the disk path from the template config (pass node for faster lookup)
        config = get_vm_config_ssh(ssh_executor, template_vmid, node=node)
        if not config:
            error_msg = f"Failed to get template {template_vmid} config from node {node}"
            logger.error(error_msg)
            return False, error_msg, None, None, None, None, None, None, None, None, None, None, None
        
        logger.info(f"Retrieved config for template {template_vmid}: {len(config)} keys")
        logger.debug(f"Config keys: {list(config.keys())}")
        
        # Log the full config to debug boot order extraction
        logger.info("Full template config:")
        for key, value in config.items():
            logger.info(f"  {key}: {value}")
        
        # Auto-detect which disk slot has the actual disk
        # Check common disk slots in order of preference
        disk_config = None
        disk_slot = None
        for slot in ['scsi0', 'virtio0', 'sata0', 'ide0', 'scsi1', 'virtio1']:
            if slot in config:
                disk_config = config[slot]
                disk_slot = slot
                logger.info(f"Found disk on slot {slot}: {disk_config}")
                break
        
        # Also check for TPM and EFI disks (Windows VMs)
        tpm_config = config.get('tpmstate0')
        efi_config = config.get('efidisk0')
        
        if tpm_config:
            logger.info(f"Template has TPM state: {tpm_config}")
        if efi_config:
            logger.info(f"Template has EFI disk: {efi_config}")
        
        if not disk_config:
            # Log all available config keys to help debug
            config_dump = '\n'.join([f"  {k}: {v}" for k, v in config.items()])
            error_msg = f"No disk found in template {template_vmid}. Config dump:\n{config_dump}"
            logger.error(error_msg)
            return False, error_msg, None, None, None, None, None, None, None, None, None, None, None
        
        # Parse disk configuration
        disk_info = parse_disk_config(disk_config)
        storage = disk_info.get('storage')
        volume = disk_info.get('volume')
        
        logger.info(f"Parsed disk config - storage: {storage}, volume: {volume}, disk_info: {disk_info}")
        
        if not storage or not volume:
            error_msg = f"Could not parse disk config for {disk_slot}: '{disk_config}'. Parsed as: storage={storage}, volume={volume}"
            logger.error(error_msg)
            return False, error_msg, None, None, None, None, None, None, None, None, None, None, None
        
        logger.info(f"Found disk on {disk_slot}, storage {storage}, volume {volume}")
        
        # Extract ALL disk options (cache, discard, iothread, ssd, backup, etc.)
        disk_options_str = ''
        if disk_info.get('options'):
            disk_options_str = ','.join([f"{k}={v}" for k, v in disk_info['options'].items()])
            logger.info(f"Template disk options: {disk_options_str}")
        
        # Extract disk controller type from slot name (scsi0 -> scsi, virtio0 -> virtio, etc.)
        disk_controller_type = 'scsi'  # Default
        if disk_slot:
            # Extract prefix (letters before digits)
            import re
            match = re.match(r'^([a-z]+)\d+$', disk_slot)
            if match:
                disk_controller_type = match.group(1)
        
        logger.info(f"Template uses {disk_controller_type} disk controller (from slot {disk_slot})")
        
        # Get hardware specs from template config (important for Windows VMs)
        ostype = config.get('ostype', 'l26')  # Default to Linux 2.6+ if not specified
        logger.info(f"Template ostype: {ostype} (from config: {config.get('ostype', 'NOT SET')})")
        bios = config.get('bios', 'seabios')  # BIOS type (seabios or ovmf/UEFI)
        machine = config.get('machine')  # Machine type (e.g., pc-q35-8.1)
        cpu = config.get('cpu')  # CPU type (e.g., host, kvm64)
        scsihw = config.get('scsihw')  # SCSI hardware controller (e.g., virtio-scsi-pci, lsi)
        
        # Extract network interface model and ALL options from net0
        net_config = config.get('net0', '')
        net_model = 'virtio'  # Default
        net_options_str = ''  # Store complete options (firewall, rate, etc.)
        if net_config:
            # Parse format: 'virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0,firewall=1,rate=100'
            parts = net_config.split(',')
            # First part is model=MAC
            if parts and '=' in parts[0]:
                net_model = parts[0].split('=')[0]  # Extract model (virtio, e1000, rtl8139, etc.)
            # Remaining parts are options (skip bridge and MAC)
            net_opts = []
            for part in parts[1:]:
                part = part.strip()
                # Preserve all options except MAC address and bridge (bridge is added separately in vm_core)
                if '=' in part and not part.startswith(net_model) and not part.startswith('bridge='):
                    net_opts.append(part)
            if net_opts:
                net_options_str = ',' + ','.join(net_opts)
        logger.info(f"Template network: model={net_model}, options={net_options_str}")
        
        # Get resource specs from template
        memory = config.get('memory', 2048)  # Memory in MB
        cores = config.get('cores', 2)  # CPU cores
        sockets = config.get('sockets', 1)  # CPU sockets
        
        # Store EFI and TPM config to pass to cloned VMs
        efi_disk = efi_config  # Will be used to create EFI disk on clones
        tpm_state = tpm_config  # Will be used to create TPM on clones
        
        # CRITICAL: Get boot order configuration (fixes Windows boot issues)
        boot_order_raw = config.get('boot')  # Boot order from template (e.g., 'scsi0, net0' or 'order=scsi0;net0')
        
        # Normalize boot order format for Proxmox API
        if boot_order_raw:
            # Check if it already has 'order=' prefix
            if boot_order_raw.startswith('order='):
                boot_order = boot_order_raw
            else:
                # Format: "scsi0, net0" -> "order=scsi0;net0"
                # Remove spaces and replace commas with semicolons
                devices = [d.strip() for d in boot_order_raw.replace(',', ';').split(';') if d.strip()]
                boot_order = f"order={';'.join(devices)}"
            logger.info(f"Template boot order (raw): {boot_order_raw} -> (normalized): {boot_order}")
        elif disk_slot:
            # Default boot order if not specified: boot from main disk only
            boot_order = f'order={disk_slot}'
            logger.info(f"No boot order in template, using default: {boot_order}")
        else:
            boot_order = None
            logger.warning("No boot order found in template and no disk slot detected")
        
        # Extract ALL other VM settings that should be preserved
        # These settings affect VM behavior and should match the template exactly
        other_settings = {
            'agent': config.get('agent'),  # QEMU guest agent config
            'args': config.get('args'),  # Custom QEMU arguments
            'audio0': config.get('audio0'),  # Audio device
            'balloon': config.get('balloon'),  # Balloon device (memory)
            'bios': bios,  # Already extracted but include in dict
            'boot': boot_order,  # Already extracted but include in dict
            'cdrom': config.get('cdrom'),  # CD-ROM image
            'cicustom': config.get('cicustom'),  # Cloud-init custom files
            'cipassword': config.get('cipassword'),  # Cloud-init password
            'citype': config.get('citype'),  # Cloud-init type
            'ciuser': config.get('ciuser'),  # Cloud-init user
            'cores': cores,  # Already extracted
            'cpu': cpu,  # Already extracted
            'cpulimit': config.get('cpulimit'),  # CPU limit
            'cpuunits': config.get('cpuunits'),  # CPU weight
            'description': config.get('description'),  # VM description
            'hotplug': config.get('hotplug'),  # Hotplug features
            'hugepages': config.get('hugepages'),  # Hugepages
            'ide2': config.get('ide2'),  # IDE2 (often cloud-init drive)
            'ipconfig0': config.get('ipconfig0'),  # Cloud-init IP config
            'keyboard': config.get('keyboard'),  # Keyboard layout
            'kvm': config.get('kvm'),  # KVM virtualization
            'localtime': config.get('localtime'),  # Use local time
            'lock': config.get('lock'),  # Lock status
            'machine': machine,  # Already extracted
            'memory': memory,  # Already extracted
            'migrate_downtime': config.get('migrate_downtime'),  # Migration downtime
            'migrate_speed': config.get('migrate_speed'),  # Migration speed
            'nameserver': config.get('nameserver'),  # DNS nameserver
            'numa': config.get('numa'),  # NUMA enabled
            'onboot': config.get('onboot'),  # Start at boot
            'ostype': ostype,  # Already extracted
            'protection': config.get('protection'),  # Deletion protection
            'reboot': config.get('reboot'),  # Reboot on shutdown
            'rng0': config.get('rng0'),  # Random number generator
            'scsihw': scsihw,  # Already extracted
            'searchdomain': config.get('searchdomain'),  # DNS search domain
            'serial0': config.get('serial0'),  # Serial port 0
            'serial1': config.get('serial1'),  # Serial port 1
            'serial2': config.get('serial2'),  # Serial port 2
            'serial3': config.get('serial3'),  # Serial port 3
            'shares': config.get('shares'),  # CPU shares
            'smbios1': config.get('smbios1'),  # SMBIOS type 1 values
            'sockets': sockets,  # Already extracted
            'sshkeys': config.get('sshkeys'),  # SSH public keys (cloud-init)
            'startup': config.get('startup'),  # Startup/shutdown order
            'tablet': config.get('tablet'),  # Tablet pointer device
            'tags': config.get('tags'),  # VM tags
            'usb0': config.get('usb0'),  # USB device 0
            'usb1': config.get('usb1'),  # USB device 1
            'usb2': config.get('usb2'),  # USB device 2
            'usb3': config.get('usb3'),  # USB device 3
            'vcpus': config.get('vcpus'),  # vCPU count
            'vga': config.get('vga'),  # VGA adapter
            'vmgenid': config.get('vmgenid'),  # VM generation ID
            'watchdog': config.get('watchdog'),  # Watchdog device
        }
        
        # Remove None values to avoid cluttering logs
        other_settings = {k: v for k, v in other_settings.items() if v is not None}
        
        logger.info(f"Template additional settings extracted: {len(other_settings)} options")
        logger.debug(f"Additional settings: {other_settings}")
        
        logger.info("Template hardware config extracted:")
        logger.info(f"  ostype: {ostype}, bios: {bios}, machine: {machine}")
        logger.info(f"  cpu: {cpu}, scsihw: {scsihw}")
        logger.info(f"  memory: {memory}MB, cores: {cores}, sockets: {sockets}")
        logger.info(f"  EFI disk: {bool(efi_disk)}, TPM: {bool(tpm_state)}")
        logger.info(f"Template resources - memory: {memory}MB, cores: {cores}, sockets: {sockets}")
        logger.info(f"Template boot config - EFI: {efi_disk}, TPM: {tpm_state}")
        
        # Resolve source path
        # For NFS storage: /mnt/pve/{storage}/images/{vmid}/{volume}
        if '/' in volume:
            # Volume includes subdirectory (e.g., "100/vm-100-disk-0.qcow2")
            source_path = f"/mnt/pve/{storage}/images/{volume}"
        else:
            # Infer vmid directory
            source_path = f"/mnt/pve/{storage}/images/{template_vmid}/{volume}"
        
        logger.info(f"Converting {source_path} to {output_path}")
        
        # Use vm_core's convert function
        success, error = convert_disk_to_qcow2(
            ssh_executor=ssh_executor,
            source_path=source_path,
            output_path=output_path,
            timeout=timeout,
        )
        
        if success:
            logger.info(f"Successfully exported template to {output_path} (controller: {disk_controller_type}, ostype: {ostype}, bios: {bios}, scsihw: {scsihw}, net: {net_model}, memory: {memory}MB, cores: {cores}, EFI: {bool(efi_disk)}, TPM: {bool(tpm_state)}, boot: {boot_order}, +{len(other_settings)} settings)")
            return True, "", disk_controller_type, ostype, bios, machine, cpu, scsihw, memory, cores, sockets, efi_disk, tpm_state, boot_order, net_model, disk_options_str, net_options_str, other_settings
        else:
            return False, error, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None
        
    except Exception as e:
        logger.exception(f"Error exporting template: {e}")
        return False, str(e), None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None


# ---------------------------------------------------------------------------
# Overlay VM Creation
# ---------------------------------------------------------------------------

def create_overlay_vm(
    ssh_executor: SSHExecutor,
    vmid: int,
    name: str,
    base_qcow2_path: str,
    node: str,
    memory: int = 2048,
    cores: int = 2,
    storage: str = None,
    disk_controller_type: str = 'scsi',
    ostype: str = 'l26',
    bios: str = None,
    machine: str = None,
    cpu: str = None,
    scsihw: str = None,
    efi_disk: str = None,
    tpm_state: str = None,
    boot_order: str = None,
    net_model: str = 'virtio',
    disk_options: str = '',
    net_options: str = '',
    other_settings: dict = None,
    template_vmid: int = None,
) -> Tuple[bool, str, Optional[str]]:
    """
    Create a VM using a QCOW2 overlay (copy-on-write).
    
    NEW APPROACH: Copy template's .conf file and modify it directly.
    This guarantees 100% identical configuration without parsing 65+ settings.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to create
        name: VM name
        base_qcow2_path: Path to backing QCOW2 file
        node: Proxmox node name
        memory: Memory in MB (default: 2048) - IGNORED if template_vmid provided
        cores: CPU cores (default: 2) - IGNORED if template_vmid provided
        storage: Storage name (default: PROXMOX_STORAGE_NAME)
        disk_controller_type: Disk controller type - scsi, virtio, sata, or ide (default: scsi)
        ostype: OS type - IGNORED if template_vmid provided
        bios: BIOS type - IGNORED if template_vmid provided
        machine: Machine type - IGNORED if template_vmid provided
        cpu: CPU type - IGNORED if template_vmid provided
        scsihw: SCSI hardware controller - IGNORED if template_vmid provided
        efi_disk: EFI disk config - IGNORED if template_vmid provided
        tpm_state: TPM state config - IGNORED if template_vmid provided
        boot_order: Boot order configuration - IGNORED if template_vmid provided
        net_model: Network interface model - IGNORED if template_vmid provided
        disk_options: Disk options string - IGNORED if template_vmid provided
        net_options: Network options string - IGNORED if template_vmid provided
        other_settings: Dict of all other VM settings - IGNORED if template_vmid provided
        template_vmid: If provided, copy config from this template VM (NEW PARAMETER)
        
    Returns:
        Tuple of (success, error_message, mac_address)
    """
    storage = storage or PROXMOX_STORAGE_NAME
    images_path = DEFAULT_VM_IMAGES_PATH
    
    try:
        # Step 1: Create overlay disk first
        overlay_dir = f"{images_path}/{vmid}"
        overlay_path = f"{overlay_dir}/vm-{vmid}-disk-0.qcow2"
        
        success, error = create_overlay_disk(
            ssh_executor=ssh_executor,
            output_path=overlay_path,
            backing_file=base_qcow2_path,
        )
        
        if not success:
            return False, f"Failed to create overlay disk: {error}", None
        
        # Step 2: Clone VM config from template (if template_vmid provided)
        if template_vmid:
            logger.info(f"Cloning VM config from template {template_vmid} to VM {vmid}")
            from app.services.vm_config_clone import clone_vm_config, check_template_has_efi_tpm
            
            # Get template node - prefer passed 'node' parameter, fallback to database lookup
            template_node = node  # Use the node parameter passed to this function
            if not template_node:
                # Fallback: try to get from database
                from app.models import Template
                template_obj = Template.query.filter_by(proxmox_vmid=template_vmid).first()
                template_node = template_obj.node if template_obj else None
            
            logger.info(f"Template {template_vmid} is on node: {template_node}")
            
            # Check if template has EFI/TPM disks that need to be created
            # Use cache to avoid re-checking for every VM
            if template_vmid in _template_efi_tpm_cache:
                has_efi, has_tpm = _template_efi_tpm_cache[template_vmid]
                logger.debug(f"Template {template_vmid} EFI/TPM status from cache: has_efi={has_efi}, has_tpm={has_tpm}")
            else:
                has_efi, has_tpm = check_template_has_efi_tpm(ssh_executor, template_vmid, template_node)
                _template_efi_tpm_cache[template_vmid] = (has_efi, has_tpm)
                logger.info(f"Template {template_vmid} EFI/TPM check (cached): has_efi={has_efi}, has_tpm={has_tpm}")
            
            # Ensure overlay directory exists (should already exist from overlay disk creation)
            ssh_executor.execute(f"mkdir -p {overlay_dir}", check=False)
            
            # Create EFI disk if template has one (required for UEFI boot)
            if has_efi:
                logger.info(f"Template has EFI disk, creating for VM {vmid}")
                efi_path = f"{overlay_dir}/vm-{vmid}-disk-1.raw"
                # Create 528K EFI disk (standard size) - use qemu-img for proper format
                efi_cmd = f"qemu-img create -f raw {efi_path} 528K"
                exit_code, stdout, stderr = ssh_executor.execute(efi_cmd, timeout=30, check=False)
                if exit_code != 0:
                    logger.error(f"Failed to create EFI disk: {stderr}")
                    ssh_executor.execute(f"rm -rf {overlay_dir}", check=False)
                    return False, f"Failed to create EFI disk: {stderr}", None
                
                # Set proper permissions on EFI disk
                ssh_executor.execute(f"chmod 600 {efi_path}", timeout=10, check=False)
                ssh_executor.execute(f"chown root:root {efi_path}", timeout=10, check=False)
                
                # Verify EFI disk was created
                verify_cmd = f"ls -lh {efi_path}"
                exit_code, ls_output, _ = ssh_executor.execute(verify_cmd, timeout=10, check=False)
                if exit_code == 0:
                    logger.info(f"Created EFI disk at {efi_path}: {ls_output.strip()}")
                else:
                    logger.error(f"EFI disk verification failed - file not found: {efi_path}")
            
            # Create TPM state disk if template has one (required for Windows 11)
            if has_tpm:
                logger.info(f"Template has TPM, creating for VM {vmid}")
                tpm_path = f"{overlay_dir}/vm-{vmid}-disk-2.raw"
                # Create 4M TPM state disk (standard size) - use qemu-img for proper format
                tpm_cmd = f"qemu-img create -f raw {tpm_path} 4M"
                exit_code, stdout, stderr = ssh_executor.execute(tpm_cmd, timeout=30, check=False)
                if exit_code != 0:
                    logger.warning(f"Failed to create TPM disk: {stderr}")
                    # Don't fail the whole operation for TPM
                else:
                    # Set proper permissions on TPM disk
                    ssh_executor.execute(f"chmod 600 {tpm_path}", timeout=10, check=False)
                    ssh_executor.execute(f"chown root:root {tpm_path}", timeout=10, check=False)
                    
                    # Verify TPM disk was created
                    verify_cmd = f"ls -lh {tpm_path}"
                    exit_code, ls_output, _ = ssh_executor.execute(verify_cmd, timeout=10, check=False)
                    if exit_code == 0:
                        logger.info(f"Created TPM disk at {tpm_path}: {ls_output.strip()}")
                    else:
                        logger.warning(f"TPM disk verification failed - file not found: {tpm_path}")
            
            # Disk path relative to storage mount: "58000/vm-58000-disk-0.qcow2"
            overlay_disk_rel = f"{vmid}/vm-{vmid}-disk-0.qcow2"
            
            success, error, mac = clone_vm_config(
                ssh_executor=ssh_executor,
                source_vmid=template_vmid,
                dest_vmid=vmid,
                dest_name=name,
                overlay_disk_path=overlay_disk_rel,
                storage=storage,
                source_node=template_node,  # Pass node for SSH hop
            )
            
            if not success:
                # Cleanup overlay disk on failure
                ssh_executor.execute(f"rm -rf {overlay_dir}", check=False)
                return False, f"Failed to clone VM config: {error}", None
            
            logger.info(f"VM {vmid} created with config from template {template_vmid}, MAC: {mac}")
            
            # Verify the config was written correctly (especially EFI/TPM paths)
            verify_config_cmd = f"cat /etc/pve/qemu-server/{vmid}.conf"
            exit_code, config_content, _ = ssh_executor.execute(verify_config_cmd, timeout=10, check=False)
            if exit_code == 0:
                if has_efi and 'efidisk0:' in config_content:
                    for line in config_content.split('\n'):
                        if line.startswith('efidisk0:'):
                            logger.info(f"VM {vmid} EFI config: {line}")
                elif has_efi:
                    logger.error(f"VM {vmid} config missing efidisk0 line!")
                    
                if has_tpm and 'tpmstate0:' in config_content:
                    for line in config_content.split('\n'):
                        if line.startswith('tpmstate0:'):
                            logger.info(f"VM {vmid} TPM config: {line}")
                elif has_tpm:
                    logger.warning(f"VM {vmid} config missing tpmstate0 line!")
            else:
                logger.warning(f"Could not verify VM {vmid} config")
            return True, "", mac
        
        # Step 3: Fallback to old approach if no template_vmid (shouldn't happen in normal usage)
        logger.warning("No template_vmid provided, using legacy VM creation approach")
        
        # OLD APPROACH: Create VM shell with explicit settings
        logger.info(f"Creating VM shell with scsihw={scsihw}, disk_controller_type={disk_controller_type}")
        success, error = create_vm_shell(
            ssh_executor=ssh_executor,
            vmid=vmid,
            name=name,
            memory=memory,
            cores=cores,
            ostype=ostype,
            bios=bios,
            machine=machine,
            cpu=cpu,
            scsihw=scsihw,
            storage=storage,
            net_model=net_model,
            net_options=net_options,
            other_settings=other_settings,
        )
        
        if not success:
            return False, f"Failed to create VM shell: {error}", None
        
        # Step 1.5: Add EFI disk and TPM if template had them (critical for Windows UEFI boot)
        if efi_disk:
            logger.info(f"Template has EFI disk, adding to VM {vmid}: {efi_disk}")
            # Parse EFI disk config (format: "storage:size,efitype=4m,pre-enrolled-keys=1")
            # For clones, create new EFI disk with same parameters
            efi_cmd = f"qm set {vmid} --efidisk0 {storage}:1,efitype=4m,pre-enrolled-keys=1"
            exit_code, stdout, stderr = ssh_executor.execute(efi_cmd, timeout=30, check=False)
            if exit_code != 0:
                logger.error(f"Failed to add EFI disk to VM {vmid}: {stderr}")
                destroy_vm(ssh_executor, vmid, purge=True)
                return False, f"Failed to add EFI disk: {stderr}", None
            logger.info(f"Added EFI disk to VM {vmid}")
        
        if tpm_state:
            logger.info(f"Template has TPM, adding to VM {vmid}: {tpm_state}")
            # Parse TPM config (format: "storage:size,version=v2.0")
            # For clones, create new TPM with same parameters
            tpm_cmd = f"qm set {vmid} --tpmstate0 {storage}:1,version=v2.0"
            exit_code, stdout, stderr = ssh_executor.execute(tpm_cmd, timeout=30, check=False)
            if exit_code != 0:
                logger.warning(f"Failed to add TPM to VM {vmid}: {stderr}")
                # Don't fail the whole operation for TPM
            else:
                logger.info(f"Added TPM to VM {vmid}")
        
        # Step 2: Create overlay disk
        overlay_dir = f"{images_path}/{vmid}"
        overlay_path = f"{overlay_dir}/vm-{vmid}-disk-0.qcow2"
        
        success, error = create_overlay_disk(
            ssh_executor=ssh_executor,
            output_path=overlay_path,
            backing_file=base_qcow2_path,
        )
        
        if not success:
            # Cleanup VM shell on failure
            destroy_vm(ssh_executor, vmid, purge=True)
            return False, f"Failed to create overlay disk: {error}", None
        
        # Step 3: Attach disk to VM using correct controller type with all options
        disk_spec = f"{vmid}/vm-{vmid}-disk-0.qcow2"
        success, error = attach_disk_to_vm(
            ssh_executor=ssh_executor,
            vmid=vmid,
            disk_path=disk_spec,
            storage=storage,
            set_boot=False,  # Don't set basic boot here, we'll set proper boot order below
            disk_slot=f"{disk_controller_type}0",  # Use detected controller type
            disk_options=disk_options,  # Apply all disk options from template
        )
        
        if not success:
            # Cleanup on failure
            destroy_vm(ssh_executor, vmid, purge=True)
            return False, f"Failed to attach disk: {error}", None
        
        # Step 3.5: Set proper boot order (CRITICAL for Windows VMs)
        if boot_order:
            logger.info(f"Applying boot order to VM {vmid}: {boot_order}")
            # Replace disk slot reference in boot order with our new disk
            # e.g., 'order=scsi0;ide2;net0' stays the same since we use scsi0
            boot_cmd = f"qm set {vmid} --boot {boot_order}"
            exit_code, stdout, stderr = ssh_executor.execute(boot_cmd, timeout=30, check=False)
            if exit_code != 0:
                logger.error(f"Failed to set boot order on VM {vmid}: {stderr}")
                destroy_vm(ssh_executor, vmid, purge=True)
                return False, f"Failed to set boot order: {stderr}", None
            logger.info(f"Boot order applied to VM {vmid}: {boot_order}")
        else:
            # Fallback to simple boot configuration
            logger.info(f"No boot order from template, using simple boot for VM {vmid}")
            boot_cmd = f"qm set {vmid} --boot order={disk_controller_type}0"
            ssh_executor.execute(boot_cmd, timeout=30, check=False)
        
        # Get MAC address
        mac = get_vm_mac_address_ssh(ssh_executor, vmid)
        
        logger.info(f"Created overlay VM {vmid} ({name}) backed by {base_qcow2_path}")
        return True, "", mac
        
    except Exception as e:
        logger.exception(f"Error creating overlay VM: {e}")
        # Attempt cleanup
        try:
            destroy_vm(ssh_executor, vmid, purge=True)
        except Exception:
            pass
        return False, str(e), None


def create_student_overlays(
    ssh_executor: SSHExecutor,
    base_qcow2_path: str,
    class_prefix: str,
    count: int,
    node: str,
    start_vmid: int = None,
    memory: int = 2048,
    cores: int = 2,
) -> Tuple[List[dict], int, int]:
    """
    Create multiple student VMs as overlays from a base QCOW2.
    
    Args:
        ssh_executor: SSH executor for running commands
        base_qcow2_path: Path to the base QCOW2 file
        class_prefix: Prefix for VM names
        count: Number of student VMs to create
        node: Proxmox node name
        start_vmid: Starting VMID (default: auto-detect)
        memory: Memory per VM in MB (default: 2048)
        cores: CPU cores per VM (default: 2)
        
    Returns:
        Tuple of (created_vms, successful_count, failed_count)
        where created_vms is a list of dicts with vmid, name, mac
    """
    created_vms = []
    successful = 0
    failed = 0
    
    # Get starting VMID if not specified
    if start_vmid is None:
        start_vmid = get_next_available_vmid_ssh(ssh_executor)
    
    current_vmid = start_vmid
    
    for i in range(count):
        student_name = build_vm_name(class_prefix, "student", i + 1)
        
        # Find next available VMID with safeguard against infinite loops
        max_search_iterations = 1000  # Prevent infinite loop if VMID space exhausted
        search_iterations = 0
        while search_iterations < max_search_iterations:
            search_iterations += 1
            exit_code, _, _ = ssh_executor.execute(
                f"qm status {current_vmid}",
                check=False,
                timeout=10
            )
            if exit_code != 0:
                break  # VMID is available
            current_vmid += 1
        
        if search_iterations >= max_search_iterations:
            logger.error(f"Failed to find available VMID after {max_search_iterations} iterations (starting from {current_vmid - max_search_iterations})")
            failed += 1
            continue  # Skip this VM and try next one
        
        vmid = current_vmid
        current_vmid += 1
        
        success, error, mac = create_overlay_vm(
            ssh_executor=ssh_executor,
            vmid=vmid,
            name=student_name,
            base_qcow2_path=base_qcow2_path,
            node=node,
            memory=memory,
            cores=cores,
        )
        
        if success:
            created_vms.append({
                'vmid': vmid,
                'name': student_name,
                'mac': mac,
                'node': node,
            })
            successful += 1
            logger.info(f"Created student VM {vmid} ({student_name})")
        else:
            failed += 1
            logger.error(f"Failed to create student VM: {error}")
    
    return created_vms, successful, failed


# ---------------------------------------------------------------------------
# Full Clone Operations (via SSH qm clone)
# ---------------------------------------------------------------------------

def full_clone_vm(
    ssh_executor: SSHExecutor,
    source_vmid: int,
    target_vmid: int,
    name: str,
    timeout: int = 300,
) -> Tuple[bool, str, Optional[str]]:
    """
    Create a full clone of a VM via SSH.
    
    Uses `qm clone --full` which creates an independent copy of the source VM.
    
    Args:
        ssh_executor: SSH executor for running commands
        source_vmid: Source VM ID to clone
        target_vmid: Target VM ID for the clone
        name: Name for the cloned VM
        timeout: Clone timeout in seconds (default: 300)
        
    Returns:
        Tuple of (success, error_message, mac_address)
    """
    safe_name = sanitize_vm_name(name)
    
    cmd = f"qm clone {source_vmid} {target_vmid} --name {safe_name} --full"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            cmd,
            timeout=timeout,
            check=False
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to clone VM {source_vmid} -> {target_vmid}: {error_msg}")
            return False, error_msg, None
        
        # Get MAC address of cloned VM
        mac = get_vm_mac_address_ssh(ssh_executor, target_vmid)
        
        logger.info(f"Cloned VM {source_vmid} -> {target_vmid} ({safe_name})")
        return True, "", mac
        
    except Exception as e:
        logger.exception(f"Error cloning VM: {e}")
        return False, str(e), None


def linked_clone_vm(
    ssh_executor: SSHExecutor,
    source_vmid: int,
    target_vmid: int,
    name: str,
    timeout: int = 120,
) -> Tuple[bool, str, Optional[str]]:
    """
    Create a linked clone of a VM via SSH.
    
    Uses `qm clone` without --full, creating a copy-on-write clone.
    Note: Linked clones must be on the same storage as the source.
    
    Args:
        ssh_executor: SSH executor for running commands
        source_vmid: Source VM ID to clone (must be a template)
        target_vmid: Target VM ID for the clone
        name: Name for the cloned VM
        timeout: Clone timeout in seconds (default: 120)
        
    Returns:
        Tuple of (success, error_message, mac_address)
    """
    safe_name = sanitize_vm_name(name)
    
    cmd = f"qm clone {source_vmid} {target_vmid} --name {safe_name}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            cmd,
            timeout=timeout,
            check=False
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to linked clone VM {source_vmid} -> {target_vmid}: {error_msg}")
            return False, error_msg, None
        
        # Get MAC address of cloned VM
        mac = get_vm_mac_address_ssh(ssh_executor, target_vmid)
        
        logger.info(f"Linked clone VM {source_vmid} -> {target_vmid} ({safe_name})")
        return True, "", mac
        
    except Exception as e:
        logger.exception(f"Error creating linked clone: {e}")
        return False, str(e), None


# ---------------------------------------------------------------------------
# Template Conversion
# ---------------------------------------------------------------------------

def convert_to_template(
    ssh_executor: SSHExecutor,
    vmid: int,
) -> Tuple[bool, str]:
    """
    Convert a VM to a template via SSH.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to convert
        
    Returns:
        Tuple of (success, error_message)
    """
    # First ensure VM is stopped
    status_code, stdout, _ = ssh_executor.execute(
        f"qm status {vmid}",
        check=False,
        timeout=10
    )
    
    if status_code == 0 and 'running' in stdout.lower():
        logger.info(f"Stopping VM {vmid} before template conversion")
        stop_vm(ssh_executor, vmid)
        if not wait_for_vm_stopped(ssh_executor, vmid, timeout=60):
            return False, f"VM {vmid} did not stop in time"
    
    # Convert to template
    cmd = f"qm template {vmid}"
    
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            cmd,
            timeout=60,
            check=False
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            logger.error(f"Failed to convert VM {vmid} to template: {error_msg}")
            return False, error_msg
        
        logger.info(f"Converted VM {vmid} to template")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error converting to template: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# Template Push Operations
# ---------------------------------------------------------------------------

def update_overlay_backing_file(
    ssh_executor: SSHExecutor,
    vmid: int,
    new_base_path: str,
    storage: str = None,
) -> Tuple[bool, str]:
    """
    Update a VM's overlay disk to use a new backing file.
    
    This deletes the old overlay and creates a fresh one pointing to
    the new base. All VM changes are lost.
    
    Args:
        ssh_executor: SSH executor for running commands
        vmid: VM ID to update
        new_base_path: Path to the new backing QCOW2 file
        storage: Storage name (default: PROXMOX_STORAGE_NAME)
        
    Returns:
        Tuple of (success, error_message, disk_controller_type)
    """
    storage = storage or PROXMOX_STORAGE_NAME
    images_path = DEFAULT_VM_IMAGES_PATH
    
    try:
        # Ensure VM is stopped
        stop_vm(ssh_executor, vmid)
        if not wait_for_vm_stopped(ssh_executor, vmid, timeout=30):
            return False, f"VM {vmid} did not stop in time"
        
        # Delete old overlay
        overlay_path = f"{images_path}/{vmid}/vm-{vmid}-disk-0.qcow2"
        ssh_executor.execute(f"rm -f {overlay_path}", check=False)
        
        # Create new overlay with updated backing file
        success, error = create_overlay_disk(
            ssh_executor=ssh_executor,
            output_path=overlay_path,
            backing_file=new_base_path,
        )
        
        if not success:
            return False, f"Failed to recreate overlay: {error}"
        
        logger.info(f"Updated VM {vmid} overlay to use {new_base_path}")
        return True, ""
        
    except Exception as e:
        logger.exception(f"Error updating overlay: {e}")
        return False, str(e)


def push_base_to_students(
    ssh_executor: SSHExecutor,
    new_base_path: str,
    student_vmids: List[int],
) -> Tuple[int, int]:
    """
    Push a new base image to all student VMs.
    
    Recreates overlay disks for each student VM pointing to the new base.
    Student VMs will lose their current state.
    
    Args:
        ssh_executor: SSH executor for running commands
        new_base_path: Path to the new base QCOW2 file
        student_vmids: List of student VM IDs to update
        
    Returns:
        Tuple of (successful_count, failed_count)
    """
    successful = 0
    failed = 0
    
    for vmid in student_vmids:
        success, error = update_overlay_backing_file(
            ssh_executor=ssh_executor,
            vmid=vmid,
            new_base_path=new_base_path,
        )
        
        if success:
            successful += 1
        else:
            failed += 1
            logger.error(f"Failed to update VM {vmid}: {error}")
    
    logger.info(f"Pushed base to students: {successful} successful, {failed} failed")
    return successful, failed


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def get_template_disk_info(
    ssh_executor: SSHExecutor,
    template_vmid: int,
) -> Optional[dict]:
    """
    Get information about a template's primary disk.
    
    Args:
        ssh_executor: SSH executor for running commands
        template_vmid: Template VM ID
        
    Returns:
        Dict with storage, volume, size_gb, format, or None if not found
    """
    config = get_vm_config_ssh(ssh_executor, template_vmid)
    if not config:
        return None
    
    # Check common disk slots
    for slot in ['scsi0', 'virtio0', 'sata0', 'ide0']:
        disk_config = config.get(slot)
        if disk_config:
            info = parse_disk_config(disk_config)
            info['slot'] = slot
            return info
    
    return None


def verify_base_image_exists(
    ssh_executor: SSHExecutor,
    base_path: str,
) -> bool:
    """
    Verify a base QCOW2 image exists and is readable.
    
    Args:
        ssh_executor: SSH executor for running commands
        base_path: Path to the base image
        
    Returns:
        True if file exists and is readable
    """
    exit_code, _, _ = ssh_executor.execute(
        f"test -r {base_path}",
        check=False,
        timeout=10
    )
    return exit_code == 0


def cleanup_base_image(
    ssh_executor: SSHExecutor,
    base_path: str,
) -> Tuple[bool, str]:
    """
    Remove a base QCOW2 image.
    
    Args:
        ssh_executor: SSH executor for running commands
        base_path: Path to the base image
        
    Returns:
        Tuple of (success, error_message)
    """
    try:
        exit_code, stdout, stderr = ssh_executor.execute(
            f"rm -f {base_path}",
            check=False,
            timeout=30
        )
        
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            return False, error_msg
        
        logger.info(f"Removed base image: {base_path}")
        return True, ""
        
    except Exception as e:
        return False, str(e)
