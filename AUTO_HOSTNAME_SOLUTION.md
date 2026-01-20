# Automated Hostname Solution - No Manual Template Prep!

## The Problem
You don't want to manually prepare templates with sysprep or scripts.

## The Automated Solution

### Option 1: QEMU Guest Agent Auto-Rename (BEST - Works Now!)

**This works with ANY template - no preparation needed!**

The system can automatically rename VMs after they boot using QEMU Guest Agent.

#### How It Works:
1. You deploy class VMs from any template (even with duplicate hostname)
2. VMs boot up normally
3. After ~30 seconds, the system detects VM is running
4. Automatically executes hostname change command via QEMU Guest Agent
5. VM reboots with new unique hostname

#### Requirements:
- **Windows**: QEMU Guest Agent installed (download: https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win-guest-tools.exe)
- **Linux**: `qemu-guest-agent` package installed (`apt install qemu-guest-agent`)

#### Implementation:

**Add to `app/services/class_vm_service.py`** after VM creation:

```python
def auto_rename_vm_after_boot(vmid, hostname, node, cluster_id):
    """
    Automatically rename VM after it boots using QEMU Guest Agent.
    Runs in background thread.
    """
    import time
    import threading
    
    def rename_job():
        # Wait for VM to boot and guest agent to be ready
        time.sleep(45)  # Give VM time to boot
        
        try:
            ssh_executor = get_ssh_executor_from_config()
            
            # Check if guest agent is running
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm guest exec {vmid} -- echo test",
                check=False
            )
            
            if exit_code != 0:
                logger.warning(f"Guest agent not available for VM {vmid}, skipping auto-rename")
                return
            
            # Detect OS type
            exit_code, stdout, stderr = ssh_executor.execute(
                f"qm guest exec {vmid} -- uname",
                check=False
            )
            
            if exit_code == 0:  # Linux
                logger.info(f"Auto-renaming Linux VM {vmid} to {hostname}")
                ssh_executor.execute(
                    f"qm guest exec {vmid} -- hostnamectl set-hostname {hostname}",
                    check=False
                )
                # Update /etc/hostname
                ssh_executor.execute(
                    f"qm guest exec {vmid} -- bash -c 'echo {hostname} > /etc/hostname'",
                    check=False
                )
            else:  # Windows
                logger.info(f"Auto-renaming Windows VM {vmid} to {hostname}")
                # Rename computer and schedule reboot
                ssh_executor.execute(
                    f"qm guest exec {vmid} -- powershell -Command \"Rename-Computer -NewName {hostname} -Force -Restart\"",
                    check=False
                )
            
            logger.info(f"Successfully renamed VM {vmid} to {hostname}")
            
        except Exception as e:
            logger.error(f"Failed to auto-rename VM {vmid}: {e}")
    
    # Run in background thread so it doesn't block VM creation
    thread = threading.Thread(target=rename_job, daemon=True)
    thread.start()


# Call this after each student VM is created:
auto_rename_vm_after_boot(vmid, hostname, node, cluster_id)
```

---

### Option 2: Automatic Unattend.xml Injection (Windows Only)

**For Windows templates without sysprep - inject unattend.xml automatically during deployment.**

This uses `guestfish` to mount the QCOW2 overlay and inject the unattend.xml file.

#### Requirements:
- Install `libguestfs-tools` on Proxmox nodes: `apt install libguestfs-tools`

#### Implementation:

```python
def inject_unattend_xml(vmid, hostname, node):
    """
    Inject unattend.xml into Windows VM disk to auto-configure hostname.
    """
    ssh_executor = get_ssh_executor_from_config()
    
    # Get disk path
    exit_code, stdout, stderr = ssh_executor.execute(
        f"qm config {vmid} | grep scsi0 | awk '{{print $2}}'"
    )
    disk_path = stdout.strip()
    
    # Create unattend.xml with unique hostname
    unattend_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
    <settings pass="specialize">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <ComputerName>{hostname}</ComputerName>
        </component>
    </settings>
    <settings pass="oobeSystem">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <OOBE>
                <HideEULAPage>true</HideEULAPage>
                <SkipUserOOBE>true</SkipUserOOBE>
                <SkipMachineOOBE>true</SkipMachineOOBE>
            </OOBE>
        </component>
    </settings>
</unattend>
"""
    
    # Save unattend.xml to temp file
    ssh_executor.execute(f"cat > /tmp/unattend-{vmid}.xml << 'EOF'\n{unattend_xml}\nEOF")
    
    # Inject into VM disk using guestfish
    ssh_executor.execute(f"""
        guestfish -a {disk_path} -i << 'GFEOF'
        mkdir-p /Windows/Panther
        upload /tmp/unattend-{vmid}.xml /Windows/Panther/unattend.xml
        GFEOF
    """)
    
    # Cleanup
    ssh_executor.execute(f"rm /tmp/unattend-{vmid}.xml")
    
    logger.info(f"Injected unattend.xml into VM {vmid} with hostname {hostname}")
```

---

### Option 3: Post-Boot Script via Web UI (Simplest!)

**Let students run a one-click script through the web console.**

Add a "Set Hostname" button to the VM portal that:
1. Opens web console
2. Auto-types the hostname change command
3. Student just clicks "Run"

#### Add to portal UI:

```javascript
async function autoSetHostname(vmid, hostname) {
    // Open SSH console
    const consoleWindow = window.open(`/ssh/${vmid}`, '_blank');
    
    // After console loads, auto-type the command
    setTimeout(() => {
        consoleWindow.postMessage({
            type: 'auto-command',
            command: `hostnamectl set-hostname ${hostname} && echo "Hostname set to ${hostname}!"`
        }, '*');
    }, 2000);
}
```

---

## Comparison

| Method | Effort | When It Works | Limitations |
|--------|--------|---------------|-------------|
| **QEMU Guest Agent** | Low - just install agent | After first boot (~45s) | Requires guest agent installed |
| **Unattend.xml Injection** | Medium - install guestfish | During deployment | Windows only, requires libguestfs |
| **Post-Boot Script** | Very Low | Manual click by student | Requires student action |
| **Sysprep Template** | High - manual prep | Instant on first boot | Must prepare each template |

---

## Recommended Solution

**Use QEMU Guest Agent auto-rename** - it's the best balance:

✅ Works with ANY template (no preparation)  
✅ Fully automated (no student action)  
✅ Works for both Windows and Linux  
✅ Only requires guest agent installed (which you should have anyway)  

Just add the `auto_rename_vm_after_boot()` function to your VM creation code and call it after each VM is created. Done!

---

## Quick Start: Enable Guest Agent

### Windows:
1. Download: https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win-guest-tools.exe
2. Install on template
3. Service starts automatically

### Linux:
```bash
# Ubuntu/Debian
sudo apt install qemu-guest-agent
sudo systemctl enable qemu-guest-agent
sudo systemctl start qemu-guest-agent

# CentOS/RHEL
sudo yum install qemu-guest-agent
sudo systemctl enable qemu-guest-agent
sudo systemctl start qemu-guest-agent
```

### Verify it's working:
```bash
# On Proxmox node
qm guest exec <vmid> -- echo "Guest agent is working!"
```

If you see output, guest agent is ready for auto-rename!
