# Build VM from Scratch Feature

## Overview
The "Build VM from Scratch" feature allows teachers and administrators to create VMs from ISO images with comprehensive hardware configuration. VMs are automatically created on the node where the ISO is located.

## Architecture

### Components

1. **Service Layer** (`app/services/vm_deployment_service.py`)
   - Core VM deployment logic
   - Automatic node detection from ISO location
   - Handles both Linux and Windows VMs
   - VirtIO driver support for Windows VMs
   - ISO-based VM creation

2. **API Layer** (`app/routes/api/vm_builder.py`)
   - `/api/vm-builder/build` - Build VM with full configuration
   - `/api/vm-builder/isos` - List available ISO images from all nodes
   - `/api/vm-builder/storages` - List storage pools

3. **UI Layer** (`app/templates/template_builder/index.html`)
   - Modal form with hardware configuration options
   - ISO selection from all available nodes
   - Real-time ISO loading

## Features

### VM Creation Options

**Basic Configuration:**
- VM name
- OS type (Ubuntu, Debian, CentOS, Rocky, Alma, Windows)
- ISO image selection (required)
- Storage pool selection

**Hardware Configuration:**
- CPU cores (1-64)
- Memory (512MB - 64GB)
- Disk size (8GB - 2TB)
- Network bridge selection

**Windows-Specific:**
- Automatic VirtIO drivers ISO attachment
- UEFI configuration
- Manual installation via console

**Template Options:**
- Checkbox to convert VM to template after creation
- Useful for creating reusable templates

## Workflow

### VM Creation with ISO

1. Select OS type (Ubuntu, Debian, Windows, etc.)
2. Select ISO image from dropdown (shows node and storage location)
3. Configure hardware (CPU, RAM, disk)
4. Choose storage pool for VM disk
5. Optionally enable "Convert to template after creation"
6. Click "Build VM"

**Result:** VM created with:
- ISO attached for OS installation
- Windows VMs get VirtIO drivers ISO automatically
- VM created on the node where ISO is located
- VM left stopped for manual OS installation via console
- Optional conversion to template after creation

### Example: Linux VM

1. Select "Ubuntu" OS type
2. Select `ubuntu-22.04-live-server-amd64.iso` from dropdown
3. Configure: 4 cores, 4096MB RAM, 50GB disk
4. Select storage: `local-lvm`
5. Click "Build VM"

**Result:** VM created on node where ISO exists, ready to boot from console and install Ubuntu manually.

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


### Example: Windows VM

1. Select "Windows" OS type
2. Select `Win10_22H2_English_x64.iso` from dropdown
3. Configure: 4 cores, 8192MB RAM, 100GB disk
4. Select storage: `local-lvm`
5. Click "Build VM"

**Result:** VM created with Windows ISO and VirtIO drivers ISO attached, UEFI configured, ready to boot and install Windows.

## Technical Details

### Node Auto-detection

When you select an ISO, the system:
1. Scans all nodes in the cluster
2. Checks all storage pools for ISO content
3. Finds the node and storage where the ISO exists
4. Creates the VM on that node automatically

### Windows VirtIO Drivers

For Windows VMs, the service:
1. Checks for `virtio-win.iso` in storage
2. If not found, downloads from Fedora repository
3. Attaches VirtIO ISO to `ide0`
4. Attaches Windows ISO to `ide2`
5. Configures UEFI boot for modern Windows

### Advanced Hardware Options

The service supports advanced hardware options:

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

1. **No ISO selected:**
   - Alert: "Please select an ISO image"
   - Solution: Select an ISO from the dropdown

2. **ISO not found:**
   - Error: "ISO file not found on any node"
   - Solution: Verify ISO uploaded to Proxmox storage

3. **Storage full:**
   - API returns error with storage details
   - Solution: Select different storage pool or free up space

4. **VirtIO ISO download failure:**
   - VM created without VirtIO drivers
   - Warning logged, VM still usable
   - Solution: Manually download and upload virtio-win.iso

## Security Considerations

1. **Access control:** Only teachers and administrators can build VMs
2. **Network isolation:** VMs created on specified bridge (vmbr0 default)
3. **Manual OS installation:** No automatic credentials or network config (user controls security)

## Future Enhancements

Potential improvements:

1. **Storage selection UI:** Show available space per storage pool
2. **Advanced hardware options UI:** Expose CPU flags, NUMA, etc. in modal
3. **Bulk VM creation:** Build multiple VMs with same configuration
4. **Progress tracking:** Show real-time deployment progress
5. **Post-deployment scripts:** Run custom scripts after VM creation
6. **Template marketplace:** Share templates with other teachers

## Comparison with deployment.py

### Similarities
- Uses same ProxmoxAPI operations (`qm create`, `config.put`)
- VirtIO driver handling
- SSH executor for complex operations
- Error handling patterns

### Differences
- **Simplified approach:** ISO-only, no cloud-init complexity
- **No database:** VMs not tracked in deploy database
- **Automatic node detection:** No manual node selection
- **Immediate execution:** No background job queue
- **Manual OS installation:** User installs OS via console (no automation)

### Integration Points
- Both use `ProxmoxAPI` library
- SSH operations use similar patterns
- VirtIO ISO download logic ported directly

## Deployment

### Prerequisites
- Proxmox cluster configured in app/config.py
- ISO images uploaded to Proxmox storage (must have 'iso' content type)
- Storage with 'images' content enabled for VM disks
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
   - ISO: `ubuntu-22.04-live-server-amd64.iso`
   - Storage: `local-lvm`
   - CPU: 2 cores
   - Memory: 2048 MB
   - Disk: 32 GB
4. Click "Build VM"
5. Verify VM created in Proxmox on node where ISO exists
6. Open VM console and install OS manually

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

### Permission denied
- Verify user is teacher or admin
- Check session cluster_id is set
- Verify Proxmox API credentials valid

## API Examples

### Build Linux VM with ISO
```bash
curl -X POST http://localhost:8080/api/vm-builder/build \
  -H "Content-Type: application/json" \
  -d '{
    "vm_name": "ubuntu-test",
    "os_type": "ubuntu",
    "iso_file": "local:iso/ubuntu-22.04-live-server-amd64.iso",
    "cpu_cores": 2,
    "memory_mb": 2048,
    "disk_size_gb": 32,
    "storage": "local-lvm",
    "network_bridge": "vmbr0"
  }'
```

### Build Linux VM as Template
```bash
curl -X POST http://localhost:8080/api/vm-builder/build \
  -H "Content-Type: application/json" \
  -d '{
    "vm_name": "debian-template",
    "os_type": "debian",
    "iso_file": "local:iso/debian-12.0.0-amd64-netinst.iso",
    "cpu_cores": 4,
    "memory_mb": 4096,
    "disk_size_gb": 64,
    "storage": "local-lvm",
    "network_bridge": "vmbr0",
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
    "iso_file": "local:iso/Win10_22H2_English_x64.iso",
    "cpu_cores": 4,
    "memory_mb": 8192,
    "disk_size_gb": 128,
    "storage": "local-lvm",
    "network_bridge": "vmbr0",
    "bios_type": "ovmf"
  }'
```

### List Available ISOs
```bash
curl http://localhost:8080/api/vm-builder/isos
```

Response:
```json
{
  "ok": true,
  "isos": [
    {
      "volid": "local:iso/ubuntu-22.04-live-server-amd64.iso",
      "name": "ubuntu-22.04-live-server-amd64.iso",
      "size": 1474873344,
      "node": "pve1",
      "storage": "local"
    },
    {
      "volid": "local:iso/Win10_22H2_English_x64.iso",
      "name": "Win10_22H2_English_x64.iso",
      "size": 5200000000,
      "node": "pve1",
      "storage": "local"
    }
  ]
}
```

## Conclusion

The "Build VM from Scratch" feature provides a simple, ISO-based interface for creating VMs with hardware configuration control. VMs are automatically created on the node where the ISO is located, eliminating manual node selection. The feature supports manual OS installation for both Linux and Windows, making it flexible for creating custom templates or lab VMs.

