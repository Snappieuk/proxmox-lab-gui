"""
VM Deployment Service - Build VMs from scratch with full configuration options

Based on the deployment.py architecture with support for:
- Linux VMs (Ubuntu, Debian, CentOS, Rocky, Alma) with cloud-init
- Windows VMs with VirtIO drivers
- Custom ISO uploads
- Cloud image templates
- Advanced hardware configuration
"""

import logging
import time
import subprocess
import os
import tempfile
from typing import Optional, Dict, Any, Tuple

from app.services.proxmox_service import get_proxmox_admin_for_cluster
from app.services.ssh_executor import SSHExecutor, get_ssh_executor_from_config
from app.config import CLUSTERS

logger = logging.getLogger(__name__)


def build_vm_from_scratch(
    cluster_id: str,
    node: str,
    vm_name: str,
    os_type: str,
    # Hardware configuration
    cpu_cores: int = 2,
    cpu_sockets: int = 1,
    memory_mb: int = 2048,
    disk_size_gb: int = 32,
    # Network configuration
    network_bridge: str = "vmbr0",
    use_dhcp: bool = True,
    ip_address: Optional[str] = None,
    netmask: Optional[str] = None,
    gateway: Optional[str] = None,
    dns_servers: Optional[str] = None,
    # Cloud-init configuration (for Linux)
    username: Optional[str] = None,
    password: Optional[str] = None,
    ssh_key: Optional[str] = None,
    # Storage configuration
    storage: str = "local-lvm",
    iso_storage: str = "local",
    # ISO configuration
    iso_file: Optional[str] = None,
    # Advanced hardware options
    cpu_type: str = "host",
    cpu_flags: Optional[str] = None,
    cpu_limit: Optional[int] = None,
    numa_enabled: bool = False,
    bios_type: str = "seabios",
    machine_type: str = "pc",
    vga_type: str = "std",
    boot_order: str = "cdn",
    scsihw: str = "virtio-scsi-pci",
    tablet: bool = True,
    agent_enabled: bool = True,
    onboot: bool = False,
    # Template options
    convert_to_template: bool = False
) -> Tuple[bool, Optional[int], str]:
    """
    Build a VM from scratch with comprehensive configuration options.
    
    Args:
        cluster_id: Proxmox cluster ID
        node: Node name to deploy on
        vm_name: Name for the VM
        os_type: OS type (ubuntu, debian, centos, rocky, alma, windows)
        ... (see parameters above)
        
    Returns:
        Tuple of (success: bool, vmid: Optional[int], message: str)
    """
    try:
        logger.info(f"Building VM from scratch: {vm_name} on cluster {cluster_id}, node {node}")
        
        # Get Proxmox connection
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return False, None, f"Failed to connect to cluster {cluster_id}"
        
        # Get cluster config for SSH operations
        cluster_config = None
        for cluster in CLUSTERS:
            if cluster['id'] == cluster_id:
                cluster_config = cluster
                break
        
        if not cluster_config:
            return False, None, f"Cluster {cluster_id} not found in configuration"
        
        # Get next available VMID
        logger.info("Allocating VMID...")
        vmid = proxmox.cluster.nextid.get()
        logger.info(f"Allocated VMID {vmid}")
        
        # Determine VM type (Windows vs Linux)
        is_windows = os_type.lower() in ['windows', 'win10', 'win11', 'windows10', 'windows11']
        
        if is_windows:
            # Deploy Windows VM
            success, message = _deploy_windows_vm(
                proxmox=proxmox,
                cluster_config=cluster_config,
                node=node,
                vmid=vmid,
                vm_name=vm_name,
                cpu_cores=cpu_cores,
                cpu_sockets=cpu_sockets,
                memory_mb=memory_mb,
                disk_size_gb=disk_size_gb,
                storage=storage,
                iso_storage=iso_storage,
                iso_file=iso_file,
                network_bridge=network_bridge,
                cpu_type=cpu_type,
                bios_type=bios_type,
                machine_type=machine_type,
                vga_type=vga_type,
                boot_order=boot_order,
                scsihw=scsihw,
                agent_enabled=agent_enabled,
                onboot=onboot,
                convert_to_template=convert_to_template
            )
        else:
            # Deploy Linux VM
            success, message = _deploy_linux_vm(
                proxmox=proxmox,
                cluster_config=cluster_config,
                node=node,
                vmid=vmid,
                vm_name=vm_name,
                os_type=os_type,
                cpu_cores=cpu_cores,
                cpu_sockets=cpu_sockets,
                memory_mb=memory_mb,
                disk_size_gb=disk_size_gb,
                storage=storage,
                iso_file=iso_file,
                network_bridge=network_bridge,
                use_dhcp=use_dhcp,
                ip_address=ip_address,
                netmask=netmask,
                gateway=gateway,
                dns_servers=dns_servers,
                username=username,
                password=password,
                ssh_key=ssh_key,
                cpu_type=cpu_type,
                cpu_flags=cpu_flags,
                cpu_limit=cpu_limit,
                numa_enabled=numa_enabled,
                bios_type=bios_type,
                machine_type=machine_type,
                vga_type=vga_type,
                boot_order=boot_order,
                scsihw=scsihw,
                tablet=tablet,
                agent_enabled=agent_enabled,
                onboot=onboot,
                convert_to_template=convert_to_template
            )
        
        if success:
            return True, vmid, message
        else:
            return False, vmid, message
            
    except Exception as e:
        logger.error(f"Failed to build VM from scratch: {e}", exc_info=True)
        return False, None, str(e)


def _deploy_linux_vm(
    proxmox,
    cluster_config: Dict,
    node: str,
    vmid: int,
    vm_name: str,
    os_type: str,
    cpu_cores: int,
    cpu_sockets: int,
    memory_mb: int,
    disk_size_gb: int,
    storage: str,
    iso_file: Optional[str],
    network_bridge: str,
    use_dhcp: bool,
    ip_address: Optional[str],
    netmask: Optional[str],
    gateway: Optional[str],
    dns_servers: Optional[str],
    username: Optional[str],
    password: Optional[str],
    ssh_key: Optional[str],
    cpu_type: str,
    cpu_flags: Optional[str],
    cpu_limit: Optional[int],
    numa_enabled: bool,
    bios_type: str,
    machine_type: str,
    vga_type: str,
    boot_order: str,
    scsihw: str,
    tablet: bool,
    agent_enabled: bool,
    onboot: bool,
    convert_to_template: bool
) -> Tuple[bool, str]:
    """Deploy a Linux VM with optional cloud-init configuration."""
    try:
        logger.info(f"Creating Linux VM {vmid} ({os_type}) on node {node}")
        
        # Build VM configuration
        vm_config = {
            "vmid": vmid,
            "name": vm_name,
            "cores": cpu_cores,
            "sockets": cpu_sockets,
            "memory": memory_mb,
            "scsihw": scsihw,
            f"scsi0": f"{storage}:{disk_size_gb}",
            "net0": f"virtio,bridge={network_bridge}",
            "ostype": "l26",  # Linux 2.6+ kernel
            "cpu": cpu_type,
            "bios": bios_type,
            "machine": machine_type,
            "vga": vga_type,
            "boot": f"order={boot_order}",
            "onboot": 1 if onboot else 0,
            "agent": 1 if agent_enabled else 0,
            "tablet": 1 if tablet else 0
        }
        
        # Add CPU flags if specified
        if cpu_flags:
            vm_config["cpu"] = f"{cpu_type},{cpu_flags}"
        
        # Add CPU limit if specified
        if cpu_limit:
            vm_config["cpulimit"] = cpu_limit
        
        # Add NUMA if enabled
        if numa_enabled:
            vm_config["numa"] = 1
        
        # Add ISO if specified
        if iso_file:
            vm_config["ide2"] = f"{iso_file},media=cdrom"
        
        # Create VM
        logger.info(f"Creating VM with config: {vm_config}")
        proxmox.nodes(node).qemu.post(**vm_config)
        
        logger.info(f"VM {vmid} created successfully")
        
        # Wait for VM creation to finalize
        time.sleep(2)
        
        # If cloud-init credentials provided, configure cloud-init
        if username and password and not iso_file:
            logger.info(f"Configuring cloud-init for VM {vmid}")
            success = _configure_cloud_init(
                proxmox=proxmox,
                cluster_config=cluster_config,
                node=node,
                vmid=vmid,
                storage=storage,
                use_dhcp=use_dhcp,
                ip_address=ip_address,
                netmask=netmask,
                gateway=gateway,
                dns_servers=dns_servers,
                username=username,
                password=password,
                ssh_key=ssh_key
            )
            
            if not success:
                logger.warning(f"Cloud-init configuration failed for VM {vmid}")
        
        # Convert to template if requested
        if convert_to_template:
            logger.info(f"Converting VM {vmid} to template")
            proxmox.nodes(node).qemu(vmid).template.post()
            return True, f"VM {vmid} created and converted to template successfully"
        
        # Start VM if not converting to template
        logger.info(f"Starting VM {vmid}")
        proxmox.nodes(node).qemu(vmid).status.start.post()
        
        if iso_file:
            return True, f"VM {vmid} created and started. Access via console to install {os_type}."
        else:
            return True, f"VM {vmid} created and started successfully."
            
    except Exception as e:
        logger.error(f"Failed to deploy Linux VM: {e}", exc_info=True)
        return False, f"Failed to create VM: {str(e)}"


def _deploy_windows_vm(
    proxmox,
    cluster_config: Dict,
    node: str,
    vmid: int,
    vm_name: str,
    cpu_cores: int,
    cpu_sockets: int,
    memory_mb: int,
    disk_size_gb: int,
    storage: str,
    iso_storage: str,
    iso_file: Optional[str],
    network_bridge: str,
    cpu_type: str,
    bios_type: str,
    machine_type: str,
    vga_type: str,
    boot_order: str,
    scsihw: str,
    agent_enabled: bool,
    onboot: bool,
    convert_to_template: bool
) -> Tuple[bool, str]:
    """Deploy a Windows VM with VirtIO drivers."""
    try:
        logger.info(f"Creating Windows VM {vmid} on node {node}")
        
        # Build Windows VM configuration
        vm_config = {
            "vmid": vmid,
            "name": vm_name,
            "cores": cpu_cores,
            "sockets": cpu_sockets,
            "memory": memory_mb,
            "scsihw": scsihw,
            f"scsi0": f"{storage}:{disk_size_gb}",
            "net0": f"virtio,bridge={network_bridge}",
            "ostype": "win10",  # Windows OS type
            "cpu": cpu_type,
            "bios": "ovmf" if bios_type == "ovmf" else "seabios",  # UEFI for modern Windows
            "machine": machine_type,
            "vga": vga_type,
            "boot": f"order={boot_order}",
            "onboot": 1 if onboot else 0,
            "agent": 1 if agent_enabled else 0
        }
        
        # Add Windows ISO if specified
        if iso_file:
            vm_config["ide2"] = f"{iso_file},media=cdrom"
        
        # Create VM
        logger.info(f"Creating Windows VM with config: {vm_config}")
        proxmox.nodes(node).qemu.post(**vm_config)
        
        logger.info(f"Windows VM {vmid} created successfully")
        
        # Wait for VM creation to finalize
        time.sleep(2)
        
        # Try to add VirtIO drivers ISO
        virtio_iso = _ensure_virtio_iso(proxmox, node, iso_storage)
        if virtio_iso:
            logger.info(f"Adding VirtIO ISO: {virtio_iso}")
            try:
                proxmox.nodes(node).qemu(vmid).config.put(ide0=f"{virtio_iso},media=cdrom")
            except Exception as e:
                logger.warning(f"Failed to add VirtIO ISO: {e}")
        else:
            logger.warning("VirtIO ISO not available")
        
        # Convert to template if requested
        if convert_to_template:
            logger.info(f"Converting VM {vmid} to template")
            proxmox.nodes(node).qemu(vmid).template.post()
            return True, f"Windows VM {vmid} created and converted to template successfully"
        
        return True, f"Windows VM {vmid} created. Ready for manual OS installation via console."
        
    except Exception as e:
        logger.error(f"Failed to deploy Windows VM: {e}", exc_info=True)
        return False, f"Failed to create Windows VM: {str(e)}"


def _configure_cloud_init(
    proxmox,
    cluster_config: Dict,
    node: str,
    vmid: int,
    storage: str,
    use_dhcp: bool,
    ip_address: Optional[str],
    netmask: Optional[str],
    gateway: Optional[str],
    dns_servers: Optional[str],
    username: str,
    password: str,
    ssh_key: Optional[str]
) -> bool:
    """Configure cloud-init for a VM."""
    try:
        # Add cloud-init drive
        logger.info(f"Adding cloud-init drive to VM {vmid}")
        proxmox.nodes(node).qemu(vmid).config.put(ide2=f"{storage}:cloudinit")
        
        # Prepare IP configuration
        ip_config = None
        if not use_dhcp and ip_address and netmask and gateway:
            ip_config = f"ip={ip_address}/{netmask},gw={gateway}"
        
        # Prepare DNS
        nameserver = None
        if dns_servers:
            nameserver = dns_servers.replace(",", " ")
        
        # Configure cloud-init settings
        config_update = {
            "ciuser": username,
            "cipassword": password,
            "ipconfig0": ip_config or "ip=dhcp",
            "searchdomain": nameserver
        }
        
        if ssh_key:
            config_update["sshkeys"] = ssh_key
        
        logger.info(f"Applying cloud-init config: {config_update}")
        proxmox.nodes(node).qemu(vmid).config.put(**config_update)
        
        # Create custom user-data for package installation
        logger.info(f"Creating cloud-init user-data snippet for VM {vmid}")
        try:
            ssh_executor = get_ssh_executor_from_config(cluster_config, node)
            
            user_data = f"""#cloud-config
users:
  - name: {username}
    gecos: {username}
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: users, admin, sudo
    shell: /bin/bash
    lock_passwd: false
chpasswd:
  list: |
    {username}:{password}
  expire: false
disable_root: false
ssh_pwauth: true
package_update: true
package_upgrade: true
packages:
  - qemu-guest-agent
  - openssh-server
runcmd:
  - systemctl enable qemu-guest-agent
  - systemctl start qemu-guest-agent
  - systemctl enable ssh
  - systemctl start ssh
  - sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
  - sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
  - sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
  - systemctl restart ssh || systemctl restart sshd
"""
            
            # Create temporary file
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.yml') as f:
                f.write(user_data)
                temp_file = f.name
            
            # Upload user-data snippet
            snippet_path = f"/var/lib/vz/snippets/vm-{vmid}-user-data.yml"
            ssh_executor.upload_file(temp_file, snippet_path)
            
            # Update VM config to use custom user-data
            proxmox.nodes(node).qemu(vmid).config.put(cicustom=f"user=local:snippets/vm-{vmid}-user-data.yml")
            
            # Cleanup
            os.unlink(temp_file)
            
            logger.info(f"Cloud-init user-data configured for VM {vmid}")
            
        except Exception as e:
            logger.warning(f"Could not create custom cloud-init snippet: {e}")
        
        # Regenerate cloud-init drive
        proxmox.nodes(node).qemu(vmid).config.put(ide2=f"{storage}:cloudinit")
        time.sleep(1)
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to configure cloud-init: {e}")
        return False


def _ensure_virtio_iso(
    proxmox,
    node_name: str,
    storage: str = "local"
) -> Optional[str]:
    """
    Ensure VirtIO drivers ISO exists in storage.
    
    Returns:
        ISO path if available, None otherwise
    """
    try:
        virtio_filename = "virtio-win.iso"
        
        # Check if VirtIO ISO already exists
        logger.info(f"Checking for VirtIO ISO in {storage}:iso/")
        try:
            storage_content = proxmox.nodes(node_name).storage(storage).content.get()
            for item in storage_content:
                if item.get('volid', '').endswith(virtio_filename):
                    logger.info(f"Found VirtIO ISO: {item['volid']}")
                    return item['volid']
        except Exception as e:
            logger.warning(f"Could not check storage content: {e}")
        
        # VirtIO ISO not found
        logger.info(f"VirtIO ISO not found in {storage}")
        
        # Download VirtIO ISO (requires internet access from Proxmox node)
        virtio_url = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
        
        logger.info(f"Downloading VirtIO ISO from {virtio_url}")
        try:
            download_task = proxmox.nodes(node_name).storage(storage).download_url.post(
                content='iso',
                filename=virtio_filename,
                url=virtio_url
            )
            logger.info(f"VirtIO ISO download initiated: {download_task}")
            
            # Wait for download to complete (with timeout)
            max_wait = 300  # 5 minutes
            waited = 0
            while waited < max_wait:
                try:
                    storage_content = proxmox.nodes(node_name).storage(storage).content.get()
                    for item in storage_content:
                        if item.get('volid', '').endswith(virtio_filename):
                            logger.info(f"VirtIO ISO downloaded: {item['volid']}")
                            return item['volid']
                except Exception:
                    pass
                
                time.sleep(5)
                waited += 5
            
            logger.warning("VirtIO ISO download timed out")
            return None
            
        except Exception as e:
            logger.error(f"Failed to download VirtIO ISO: {e}")
            return None
        
    except Exception as e:
        logger.error(f"Failed to ensure VirtIO ISO: {e}")
        return None


def list_available_isos(cluster_id: str, node: str, storage: str = "local") -> Tuple[bool, list, str]:
    """
    List available ISO images in storage.
    
    Returns:
        Tuple of (success: bool, isos: list, message: str)
    """
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return False, [], f"Failed to connect to cluster {cluster_id}"
        
        logger.info(f"Listing ISOs in {storage} on node {node}")
        
        storage_content = proxmox.nodes(node).storage(storage).content.get()
        
        isos = []
        for item in storage_content:
            if item.get('content') == 'iso':
                isos.append({
                    'volid': item['volid'],
                    'name': item.get('volid', '').split('/')[-1],
                    'size': item.get('size', 0),
                    'format': item.get('format', 'iso')
                })
        
        logger.info(f"Found {len(isos)} ISO images")
        return True, isos, "ISOs retrieved successfully"
        
    except Exception as e:
        logger.error(f"Failed to list ISOs: {e}")
        return False, [], str(e)
