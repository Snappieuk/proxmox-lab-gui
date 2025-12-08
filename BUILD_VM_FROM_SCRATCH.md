# Build VM from Scratch Feature

## Overview
The "Build VM from Scratch" feature allows teachers and administrators to create fully configured VMs with comprehensive hardware and network settings, based on the deployment architecture from deployment.py.

## Architecture

### Components

1. **Service Layer** (`app/services/vm_deployment_service.py`)
   - Core VM deployment logic
   - Handles both Linux and Windows VMs
   - Cloud-init integration for Linux VMs
   - VirtIO driver support for Windows VMs
   - ISO management and configuration

2. **API Layer** (`app/routes/api/vm_builder.py`)
   - `/api/vm-builder/build` - Build VM with full configuration
   - `/api/vm-builder/nodes` - List available nodes
   - `/api/vm-builder/storages` - List storage pools
   - `/api/vm-builder/isos` - List available ISO images

3. **UI Layer** (`app/templates/template_builder/index.html`)
   - Modal form with comprehensive configuration options
   - Dynamic OS-specific options (Linux cloud-init vs Windows)
   - Real-time storage and ISO loading
   - Network configuration (DHCP or static)

## Features

### VM Creation Options

**Basic Configuration:**
- VM name
- OS type (Ubuntu, Debian, CentOS, Rocky, Alma, Windows)
- Target node selection
- Storage pool selection

**Hardware Configuration:**
- CPU cores (1-64)
- Memory (512MB - 64GB)
- Disk size (8GB - 2TB)
- Network bridge selection

**Linux-Specific (Cloud-init):**
- Username and password
- DHCP or static IP configuration
- Gateway and DNS configuration
- SSH configuration
- Automatic package installation (qemu-guest-agent, openssh-server)

**Windows-Specific:**
- Automatic VirtIO drivers ISO attachment
- UEFI/BIOS configuration
- Manual installation via console

**ISO Options:**
- Optional ISO selection for manual OS installation
- Auto-disables cloud-init when ISO selected
- Downloads from available ISO storage

**Template Options:**
- Checkbox to convert VM to template after creation
- Useful for creating reusable templates

## Workflow

### Linux VM with Cloud-init (Recommended)

1. Select Linux OS type (Ubuntu, Debian, etc.)
2. Configure hardware (CPU, RAM, disk)
3. Provide username and password
4. Choose DHCP or configure static IP
5. Leave ISO blank
6. Click "Build VM"

**Result:** Fully configured VM with:
- Operating system deployed from cloud image
- User account configured
- Network configured (DHCP or static)
- QEMU guest agent installed and running
- SSH enabled with password authentication
- VM automatically started

### Linux VM with ISO (Manual Install)

1. Select Linux OS type
2. Configure hardware
3. Select ISO from dropdown
4. Click "Build VM"

**Result:** VM created with ISO attached, ready for manual installation via console.

### Windows VM

1. Select "Windows" OS type
2. Configure hardware
3. Select Windows ISO
4. Click "Build VM"

**Result:** VM created with:
- Windows ISO attached
- VirtIO drivers ISO attached (if available)
- UEFI configured
- Ready for manual Windows installation

### Template Creation

1. Configure VM as desired
2. Check "Convert to template after creation"
3. Click "Build VM"

**Result:** VM created and immediately converted to template for use in classes.

## Technical Details

### Cloud-init Configuration

For Linux VMs without ISO, the service:

1. Creates VM with empty disk
2. Adds cloud-init drive (`ide2:cloudinit`)
3. Configures cloud-init settings:
   - User account (`ciuser`, `cipassword`)
   - Network configuration (`ipconfig0`)
   - DNS servers (`searchdomain`)
   - SSH keys (optional)
4. Creates custom user-data snippet:
   ```yaml
   #cloud-config
   users:
     - name: <username>
       sudo: ALL=(ALL) NOPASSWD:ALL
       groups: users, admin, sudo
       shell: /bin/bash
   packages:
     - qemu-guest-agent
     - openssh-server
   runcmd:
     - systemctl enable qemu-guest-agent
     - systemctl start qemu-guest-agent
     - sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
     - systemctl restart ssh
   ```
5. Starts VM (cloud-init runs automatically on first boot)

### Windows VirtIO Drivers

For Windows VMs, the service:

1. Checks for `virtio-win.iso` in storage
2. If not found, downloads from Fedora repository
3. Attaches VirtIO ISO to `ide0`
4. Attaches Windows ISO to `ide2`
5. Configures UEFI boot for modern Windows

### Advanced Hardware Options

The service supports all deployment.py hardware options:

- **CPU:** type, flags, limit, NUMA
- **BIOS:** SeaBIOS or UEFI (OVMF)
- **Machine type:** pc, q35, etc.
- **VGA type:** std, qxl, virtio, vmware
- **Boot order:** cdn (CD/Disk/Network), dnc, etc.
- **SCSI controller:** virtio-scsi-pci, lsi, etc.
- **Tablet device:** enabled/disabled
- **QEMU guest agent:** enabled/disabled
- **Start at boot:** onboot flag

(These advanced options are not exposed in the UI but can be added to the API call if needed)

## Error Handling

### Common Scenarios

1. **Missing cloud-init credentials:**
   - Alert: "For Linux VMs without ISO, you must provide username and password"
   - Solution: Fill in username/password or select an ISO

2. **Storage full:**
   - API returns error with storage details
   - Solution: Select different storage pool

3. **VirtIO ISO download failure:**
   - VM created without VirtIO drivers
   - Warning logged, VM still usable
   - Solution: Manually download and upload virtio-win.iso

4. **Cloud-init failure:**
   - VM created and started, but cloud-init may not run
   - Check VM console for cloud-init logs
   - Solution: Reinstall with ISO or debug cloud-init

## Security Considerations

1. **Password transmission:** Passwords sent over HTTPS, stored temporarily in Proxmox cloud-init config
2. **SSH keys:** Supported as alternative to password authentication
3. **Network isolation:** VMs created on specified bridge (vmbr0 default)
4. **Access control:** Only teachers and administrators can build VMs

## Future Enhancements

Potential improvements:

1. **Cloud image selection:** Choose from pre-downloaded cloud images (Ubuntu 22.04, Debian 12, etc.)
2. **Advanced hardware options UI:** Expose CPU flags, NUMA, etc. in modal
3. **Bulk VM creation:** Build multiple VMs with same configuration
4. **Progress tracking:** Show real-time deployment progress
5. **Post-deployment scripts:** Run custom scripts after VM creation
6. **Template marketplace:** Share templates with other teachers

## Comparison with deployment.py

### Similarities
- Uses same ProxmoxAPI operations (`qm create`, `config.put`)
- Cloud-init configuration approach
- VirtIO driver handling
- SSH executor for complex operations
- Error handling patterns

### Differences
- **Simplified UI:** Fewer options exposed (deployment.py has 40+ parameters)
- **No database:** VMs not tracked in depl0y database
- **No cloud image templates:** Uses direct ISO or cloud-init, not template cloning
- **Immediate execution:** No background job queue
- **Manual template management:** Teachers manage templates via Proxmox GUI

### Integration Points
- Both use `ProxmoxAPI` library
- SSH operations use similar patterns
- Cloud-init user-data format identical
- VirtIO ISO download logic ported directly

## Deployment

### Prerequisites
- Proxmox cluster configured in app/config.py
- SSH access to Proxmox nodes (for advanced operations)
- Storage with ISO content enabled
- (Optional) virtio-win.iso uploaded for Windows VMs

### Installation
1. Files already created:
   - `app/services/vm_deployment_service.py`
   - `app/routes/api/vm_builder.py`
   - Updated `app/routes/__init__.py`
   - Updated `app/templates/template_builder/index.html`

2. Blueprint automatically registered in `app/__init__.py`

3. No database migrations needed (uses existing Proxmox infrastructure)

### Testing
1. Navigate to `/template-builder` as teacher or admin
2. Click "Build from Scratch"
3. Fill in form:
   - Name: `test-ubuntu-vm`
   - OS: Ubuntu
   - Node: `pve1`
   - CPU: 2 cores
   - Memory: 2048 MB
   - Disk: 32 GB
   - Username: `testuser`
   - Password: `TestPassword123`
   - DHCP: checked
4. Click "Build VM"
5. Verify VM created in Proxmox
6. Check VM console for cloud-init progress
7. SSH to VM after boot completes

## Troubleshooting

### VM created but not starting
- Check Proxmox task log
- Verify storage has space
- Check VM hardware configuration in Proxmox GUI

### Cloud-init not running
- Verify cloud-init drive attached (`ide2:cloudinit`)
- Check VM console for cloud-init logs
- SSH to VM and run `cloud-init status`

### VirtIO drivers not found
- Manually upload virtio-win.iso to `local:iso/`
- Or download from: https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/

### Network not configured
- Verify network bridge exists (`vmbr0`)
- Check Proxmox network configuration
- Verify DHCP server available (if using DHCP)
- Check static IP settings (if not using DHCP)

### Permission denied
- Verify user is teacher or admin
- Check session cluster_id is set
- Verify Proxmox API credentials valid

## API Examples

### Build Linux VM with DHCP
```bash
curl -X POST http://localhost:8080/api/vm-builder/build \
  -H "Content-Type: application/json" \
  -d '{
    "vm_name": "ubuntu-test",
    "os_type": "ubuntu",
    "node": "pve1",
    "cpu_cores": 2,
    "memory_mb": 2048,
    "disk_size_gb": 32,
    "storage": "local-lvm",
    "network_bridge": "vmbr0",
    "username": "ubuntu",
    "password": "SecurePass123",
    "use_dhcp": true
  }'
```

### Build Linux VM with Static IP
```bash
curl -X POST http://localhost:8080/api/vm-builder/build \
  -H "Content-Type: application/json" \
  -d '{
    "vm_name": "debian-web",
    "os_type": "debian",
    "node": "pve2",
    "cpu_cores": 4,
    "memory_mb": 4096,
    "disk_size_gb": 64,
    "storage": "TRUENAS-NFS",
    "network_bridge": "vmbr0",
    "username": "admin",
    "password": "SecurePass123",
    "use_dhcp": false,
    "ip_address": "192.168.1.100",
    "netmask": "24",
    "gateway": "192.168.1.1",
    "dns_servers": "8.8.8.8,8.8.4.4",
    "convert_to_template": true
  }'
```

### Build Windows VM with ISO
```bash
curl -X POST http://localhost:8080/api/vm-builder/build \
  -H "Content-Type: application/json" \
  -d '{
    "vm_name": "windows-10",
    "os_type": "windows",
    "node": "pve1",
    "cpu_cores": 4,
    "memory_mb": 8192,
    "disk_size_gb": 128,
    "storage": "local-lvm",
    "iso_storage": "local",
    "iso_file": "local:iso/Win10_22H2_English_x64.iso",
    "network_bridge": "vmbr0",
    "bios_type": "ovmf"
  }'
```

### List Available Nodes
```bash
curl http://localhost:8080/api/vm-builder/nodes
```

### List Available ISOs
```bash
curl http://localhost:8080/api/vm-builder/isos?node=pve1&storage=local
```

## Conclusion

The "Build VM from Scratch" feature provides a comprehensive, user-friendly interface for creating VMs with full configuration control. It integrates deployment.py's robust VM creation logic while maintaining the simplicity expected by teachers and administrators. The feature supports both automated cloud-init deployment (for Linux) and manual ISO installation (for any OS), making it flexible for various use cases.
