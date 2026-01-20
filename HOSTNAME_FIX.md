# Fix: Unique Hostnames for Cloned VMs

## Problem
When VMs are cloned from a template, they all keep the same hostname from the template, causing network conflicts and confusion.

## Solution: Simple Template Preparation (No Cloud-Init Required!)

The easiest way to fix this is to prepare your templates correctly BEFORE converting them. The disk overlay system actually works perfectly with both sysprep (Windows) and simple startup scripts (Linux).

---

## Windows VMs - Sysprep (RECOMMENDED)

**Sysprep works perfectly with disk overlays!** Each overlay completes OOBE independently on first boot.

### Before Converting to Template:

**Option 1: Simple Sysprep (Students go through OOBE)**

1. Open: `C:\Windows\System32\Sysprep\sysprep.exe`
2. Choose:
   - System Cleanup Action: "Enter System Out-of-Box Experience (OOBE)"
   - Check "Generalize"
   - Shutdown Option: "Shutdown"
3. Click OK
4. After VM shuts down, convert it to template in Proxmox

**Option 2: Sysprep with Unattend.xml (NO OOBE - RECOMMENDED!)**

This completely skips OOBE - VMs boot directly to desktop ready to use.

1. **Create** `C:\Windows\System32\Sysprep\unattend.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
    <!-- Phase 1: Specialize (runs before first boot) -->
    <settings pass="specialize">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <ComputerName>*</ComputerName>  <!-- * = auto-generate random unique name -->
            <TimeZone>Eastern Standard Time</TimeZone>  <!-- Change to your timezone -->
        </component>
        <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <InputLocale>en-US</InputLocale>
            <SystemLocale>en-US</SystemLocale>
            <UILanguage>en-US</UILanguage>
            <UserLocale>en-US</UserLocale>
        </component>
    </settings>
    
    <!-- Phase 2: OOBE System (runs during first boot) -->
    <settings pass="oobeSystem">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <!-- Skip ALL OOBE screens -->
            <OOBE>
                <HideEULAPage>true</HideEULAPage>
                <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
                <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
                <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
                <NetworkLocation>Work</NetworkLocation>
                <ProtectYourPC>3</ProtectYourPC>  <!-- 3 = Disable telemetry prompts -->
                <SkipUserOOBE>true</SkipUserOOBE>
                <SkipMachineOOBE>true</SkipMachineOOBE>
            </OOBE>
            
            <!-- Auto-create local user account -->
            <UserAccounts>
                <LocalAccounts>
                    <LocalAccount wcm:action="add">
                        <Name>student</Name>
                        <DisplayName>Student</DisplayName>
                        <Password>
                            <Value>abc123</Value>
                            <PlainText>true</PlainText>
                        </Password>
                        <Group>Administrators</Group>
                    </LocalAccount>
                </LocalAccounts>
            </UserAccounts>
            
            <!-- Auto-logon on first boot (optional - remove if you don't want this) -->
            <AutoLogon>
                <Username>student</Username>
                <Password>
                    <Value>abc123</Value>
                    <PlainText>true</PlainText>
                </Password>
                <Enabled>true</Enabled>
                <LogonCount>1</LogonCount>  <!-- Only auto-login once -->
            </AutoLogon>
        </component>
    </settings>
</unattend>
```

2. **Run sysprep** (use unattend.xml):
   ```powershell
   C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\System32\Sysprep\unattend.xml
   ```
   Or just use the sysprep GUI - it will auto-detect the unattend.xml

3. **Convert to template** after VM shuts down

**What unattend.xml does:**
- ✅ Skips ALL OOBE screens (EULA, privacy, network, etc.)
- ✅ Auto-generates random unique computer name (`<ComputerName>*</ComputerName>`)
- ✅ Creates "student" user with password "Password123" (change as needed)
- ✅ Auto-login on first boot (optional - remove `<AutoLogon>` section if not wanted)
- ✅ Sets timezone, language, network location
- ✅ VMs boot directly to desktop, ready to use

### What Happens When Students Boot Their VMs:

**With unattend.xml (Option 2 - RECOMMENDED):**
- ✅ Boots directly to desktop
- ✅ Already logged in as "student"
- ✅ Unique computer name automatically generated
- ✅ Unique SID automatically generated
- ✅ Ready to use immediately - NO SETUP!

**Without unattend.xml (Option 1):**
- Students go through Windows Setup (OOBE)
- Must set computer name and create user account
- Takes 5-10 minutes per VM

---

## Already Deployed VMs? Post-Deployment Fix

**Windows (PowerShell - Run as Administrator):**
```powershell
# Auto-generate from MAC address
$mac = (Get-NetAdapter | Select-Object -First 1).MacAddress -replace '-',''
$hostname = "student-" + $mac.Substring($mac.Length - 6)
Rename-Computer -NewName $hostname -Force -Restart
```

**Linux (Bash - Run as root):**
```bash
MAC=$(ip link show | grep -m1 'link/ether' | awk '{print $2}' | tr -d ':' | tail -c 7)
hostnamectl set-hostname "student-${MAC}"
echo "Hostname changed to: student-${MAC}"AC=$(ip link show | grep -m1 'link/ether' | awk '{print $2}' | tr -d ':' | tail -c 7)

# Generate hostname like: student-a1b2c3
NEW_HOSTNAME="student-${MAC}"

# Set hostname
hostnamectl set-hostname "$NEW_HOSTNAME"
echo "$NEW_HOSTNAME" > /etc/hostname

# Update /etc/hosts
sed -i "s/127.0.1.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts

# Mark as complete
touch "$MARKER_FILE"

echo "Hostname set to: $NEW_HOSTNAME"
```

Make it executable:
```bash
chmod +x /usr/local/bin/set-unique-hostname.sh
```

**Option A: Systemd Service (Ubuntu/Debian/CentOS 7+)**

Create `/etc/systemd/system/set-hostname.service`:

```ini
[Unit]
Description=Set unique hostname on first boot
Before=network.target
ConditionPathExists=!/etc/hostname-set

[Service]
Type=oneshot
ExecStart=/usr/local/bin/set-unique-hostname.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable it:
```bash
systemctl enable set-hostname.service
```

**Option B: rc.local (Older Systems)**

Add to `/etc/rc.local` (before `exit 0`):
```bash
/usr/local/bin/set-unique-hostname.sh
```

Make rc.local executable:
```bash
chmod +x /etc/rc.local
```

### What Happens When VMs Boot:

Each VM will:
1. Run the script on first boot
2. Generate a unique hostname from its MAC address (which is unique per VM)
3. Set the hostname permanently
4. Never run again (marked by `/etc/hostname-set` file)

---

## Already Deployed VMs? Post-Deployment Fix

### For Already Deployed VMs (Manual Script)

Create a PowerShell script that students run on first login:

**`C:\change-hostname.ps1`**:
```powershell
# Get VM ID from Proxmox guest agent or prompt user
$vmNumber = Read-Host "Enter your student number (1-50)"
$className = "system-security"  # Change this per class
$newHostname = "$className-s$vmNumber"

# Change hostname
Rename-Computer -NewName $newHostname -Force

# Prompt for restart
$restart = Read-Host "Hostname will be '$newHostname'. Restart now? (Y/N)"
if ($restart -eq 'Y') {
    Restart-Computer -Force
}
```

**Or create a scheduled task that runs once:**
```powershell
# Save this as C:\auto-rename.ps1 in your template
$vmid = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Virtual Machine\Guest\Parameters").VirtualMachineName
$newHostname = "student-vm-$vmid"

if ($env:COMPUTERNAME -eq "TEMPLATE-NAME") {
    Rename-Computer -NewName $newHostname -Force
    # Disable this scheduled task so it only runs once
    Disable-ScheduledTask -TaskName "AutoRename"
    Restart-Computer -Force
}
```

Create scheduled task in template:
```powershell
$action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument "-File C:\auto-rename.ps1"
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
Register-ScheduledTask -TaskName "AutoRename" -Action $action -Trigger $trigger -Principal $principal
```

### Option 3: Proxmox QEMU Guest Agent (Advanced)

If you have QEMU Guest Agent installed, you can use this script to set hostname based on VM name:

**PowerShell script (run as Administrator):**
```powershell
# This reads the VM name from Proxmox and sets it as hostname
$qemuGuestAgent = "C:\Program Files\Qemu-ga\qemu-ga.exe"

if (Test-Path $qemuGuestAgent) {
    # Get VM name from Proxmox
    $vmName = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Virtual Machine\Guest\Parameters" -ErrorAction SilentlyContinue).VirtualMachineName
    
    if ($vmName -and ($env:COMPUTERNAME -ne $vmName)) {
        Write-Host "Changing hostname from $env:COMPUTERNAME to $vmName"
        Rename-Computer -NewName $vmName -Force
        
        $restart = Read-Host "Restart required. Restart now? (Y/N)"
        if ($restart -eq 'Y') {
            Restart-Computer -Force
        }
    }
}
```

## Verification

After VMs boot, check hostnames:

**Linux:**
```bash
hostname
```

**Windows:**
```powershell
hostname
```

Each VM should have a unique hostname.

## Template Preparation Checklist

### Linux Template:
- [ ] Install cloud-init
- [ ] Clear cloud-init state
- [ ] Verify DHCP is enabled
- [ ] Convert to template

### Windows Template:
- [ ] Run sysprep (Option 1), OR
- [ ] Add hostname change script (Option 2), OR
- [ ] Install QEMU Guest Agent + script (Option 3)
- [ ] Test on one clone before converting to template

## Troubleshooting

**Cloud-init not working?**
- Verify template has cloud-init installed
- Check VM has cloud-init drive: `qm cloudinit dump <vmid> meta`
- Review logs: `/var/log/cloud-init.log` on VM

**Windows hostname not changing?**
- Verify script has Administrator privileges
- Check if sysprep ran successfully
- Ensure QEMU Guest Agent is installed and running

## Additional Resources

- Cloud-init docs: https://cloudinit.readthedocs.io/
- Proxmox cloud-init: https://pve.proxmox.com/wiki/Cloud-Init_Support
- Windows sysprep: https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/sysprep--generalize--a-windows-installation
---

## Template Preparation Checklist

### Windows Template (SIMPLE - RECOMMENDED):
1. [ ] Configure VM as desired (install software, settings, etc.)
2. [ ] Run sysprep with OOBE + Generalize + Shutdown
3. [ ] After shutdown, convert VM to template in Proxmox
4. [ ] Done! Each clone will auto-generate unique SID and hostname

**Optional**: Add unattend.xml for automatic OOBE setup

### Linux Template (SIMPLE - NO CLOUD-INIT):
1. [ ] Configure VM as desired
2. [ ] Create `/usr/local/bin/set-unique-hostname.sh` script
3. [ ] Create systemd service or add to rc.local
4. [ ] Test: `systemctl enable set-hostname.service`
5. [ ] Remove `/etc/hostname-set` if it exists (so script runs on first boot)
6. [ ] Convert VM to template in Proxmox
7. [ ] Done! Each clone will auto-generate unique hostname from MAC

---

## Verification

After VMs boot, check hostnames:

**Linux:**
```bash
hostname
# Should show: student-a1b2c3 (last 6 chars of MAC)
```

**Windows:**
```powershell
hostname
# Should show: unique name set during OOBE or generated from unattend.xml
```

Each VM should have a unique hostname.

---

## Why This Works with Disk Overlays

**The key insight**: Sysprep and startup scripts work perfectly with QCOW2 overlays because:

1. **Template disk is read-only base** - Contains sysprep'd state or startup script
2. **Each student VM is a copy-on-write overlay** - Writes go to the overlay, not the base
3. **On first boot**:
   - Windows: OOBE runs independently, writes unique SID/hostname to overlay
   - Linux: Startup script runs, generates unique hostname from MAC, writes to overlay
4. **Result**: Each overlay diverges from the base, creating unique VMs

No cloud-init required! Just proper template preparation.

---

## Troubleshooting

**Windows - Sysprep didn't run?**
- Check `C:\Windows\Panther\setupact.log` for errors
- Verify you selected "Generalize" and "OOBE"
- Make sure VM actually shut down (not just stopped)

**Linux - Script not running?**
- Check if service is enabled: `systemctl status set-hostname.service`
- Check script permissions: `ls -l /usr/local/bin/set-unique-hostname.sh`
- Check logs: `journalctl -u set-hostname.service`
- Manually run: `/usr/local/bin/set-unique-hostname.sh`

**Still seeing duplicate hostnames?**
- Template might not have been prepared correctly
- Recreate template following checklist above
- Test with one clone before deploying to entire class

---

## Additional Resources

- Windows Sysprep Guide: https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/sysprep--generalize--a-windows-installation
- Unattend.xml Generator: https://www.windowsafg.com/
- Linux Hostname Guide: https://www.freedesktop.org/software/systemd/man/hostnamectl.html