#!/usr/bin/env python3
"""
Hostname Auto-Rename Service

Automatically renames VMs using QEMU Guest Agent after they boot.
Prevents loops by tracking which VMs have already been renamed.
"""

import logging
import time
import threading
from datetime import datetime, timedelta

from app.models import db, VMAssignment
from app.services.ssh_executor import get_ssh_executor_from_config

logger = logging.getLogger(__name__)

# Track rename attempts to prevent excessive retries
_rename_attempts = {}  # {vmid: {'attempts': int, 'last_attempt': datetime}}
MAX_RENAME_ATTEMPTS = 3
RETRY_COOLDOWN_MINUTES = 5


def auto_rename_vm_after_boot(vmid: int, hostname: str, node: str, cluster_id: str = None, app=None):
    """
    Automatically rename VM after it boots using QEMU Guest Agent.
    Runs in background thread with loop prevention.
    
    Args:
        vmid: Proxmox VM ID
        hostname: Target hostname to set
        node: Proxmox node name
        cluster_id: Cluster identifier (optional)
        app: Flask app instance (required for app context in background thread)
    """
    if not app:
        logger.error(f"Cannot auto-rename VM {vmid} - Flask app instance not provided")
        return
    
    def rename_job():
        # Background threads need Flask app context for database operations
        with app.app_context():
            try:
                # Check if this VM has already been renamed
                assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
                if assignment and assignment.hostname_configured:
                    logger.debug(f"VM {vmid} already renamed, skipping")
                    return
                
                # Check retry limits
                if vmid in _rename_attempts:
                    attempt_data = _rename_attempts[vmid]
                    if attempt_data['attempts'] >= MAX_RENAME_ATTEMPTS:
                        logger.warning(f"VM {vmid} exceeded max rename attempts ({MAX_RENAME_ATTEMPTS}), giving up")
                        return
                    
                    # Check cooldown
                    if datetime.now() - attempt_data['last_attempt'] < timedelta(minutes=RETRY_COOLDOWN_MINUTES):
                        logger.debug(f"VM {vmid} still in cooldown, skipping")
                        return
                
                # Wait for VM to boot and guest agent to be ready
                logger.info(f"Waiting 45s for VM {vmid} to boot before attempting hostname rename...")
                time.sleep(45)
                
                # Check again after sleep (VM might have been renamed already)
                assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
                if assignment and assignment.hostname_configured:
                    logger.debug(f"VM {vmid} was renamed during boot wait, skipping")
                    return
                
                # Track this attempt
                if vmid not in _rename_attempts:
                    _rename_attempts[vmid] = {'attempts': 0, 'last_attempt': datetime.now()}
                _rename_attempts[vmid]['attempts'] += 1
                _rename_attempts[vmid]['last_attempt'] = datetime.now()
                
                ssh_executor = get_ssh_executor_from_config()
                
                # Check if guest agent is running
                logger.info(f"Checking guest agent status for VM {vmid}...")
                exit_code, stdout, stderr = ssh_executor.execute(
                    f"qm guest exec {vmid} -- echo test",
                    check=False,
                    timeout=10
                )
                
                if exit_code != 0:
                    logger.warning(f"Guest agent not available for VM {vmid}: {stderr}")
                    # Don't mark as failed - guest agent might not be installed
                    return
                
                # Detect OS type by trying uname
                logger.info(f"Detecting OS type for VM {vmid}...")
                exit_code, stdout, stderr = ssh_executor.execute(
                    f"qm guest exec {vmid} -- uname",
                    check=False,
                    timeout=10
                )
                
                if exit_code == 0:  # Linux
                    logger.info(f"Auto-renaming Linux VM {vmid} to hostname: {hostname}")
                    
                    # Set hostname
                    exit_code, stdout, stderr = ssh_executor.execute(
                        f"qm guest exec {vmid} -- hostnamectl set-hostname {hostname}",
                        check=False,
                        timeout=15
                    )
                    
                    if exit_code != 0:
                        logger.error(f"Failed to set hostname for VM {vmid}: {stderr}")
                        return
                    
                    # Update /etc/hostname
                    ssh_executor.execute(
                        f"qm guest exec {vmid} -- bash -c 'echo {hostname} > /etc/hostname'",
                        check=False,
                        timeout=15
                    )
                    
                    # Update /etc/hosts
                    ssh_executor.execute(
                        f"qm guest exec {vmid} -- sed -i 's/127.0.1.1.*/127.0.1.1\\t{hostname}/' /etc/hosts",
                        check=False,
                        timeout=15
                    )
                    
                    logger.info(f"Successfully renamed Linux VM {vmid} to {hostname}")
                    
                else:  # Assume Windows
                    logger.info(f"Auto-renaming Windows VM {vmid} to hostname: {hostname}")
                    
                    # Rename computer (this will schedule a reboot)
                    exit_code, stdout, stderr = ssh_executor.execute(
                        f'qm guest exec {vmid} -- powershell -Command "Rename-Computer -NewName {hostname} -Force -Restart"',
                        check=False,
                        timeout=30
                    )
                    
                    if exit_code != 0:
                        logger.error(f"Failed to rename Windows VM {vmid}: {stderr}")
                        return
                    
                    logger.info(f"Successfully renamed Windows VM {vmid} to {hostname} (rebooting)")
                
                # Mark as configured in database
                if assignment:
                    assignment.hostname_configured = True
                    assignment.target_hostname = hostname
                    db.session.commit()
                    logger.info(f"Marked VM {vmid} as hostname_configured in database")
                    
                    # Immediately close the session to release any locks
                    db.session.close()
                
                # Clear retry tracking
                if vmid in _rename_attempts:
                    del _rename_attempts[vmid]
                    
            except Exception as e:
                logger.error(f"Failed to auto-rename VM {vmid}: {e}", exc_info=True)
                db.session.rollback()
                db.session.close()  # Release lock even on error
    
    # Run in background thread so it doesn't block VM creation
    thread = threading.Thread(target=rename_job, daemon=True, name=f"HostnameRename-{vmid}")
    thread.start()
    logger.debug(f"Started background thread to rename VM {vmid} to {hostname}")


def check_and_retry_pending_renames(app=None):
    """
    Check for VMs that need hostname configuration and retry them.
    This can be called periodically by a background daemon.
    
    Args:
        app: Flask app instance (required for app context)
    """
    if not app:
        logger.error("Cannot check pending renames - Flask app instance not provided")
        return
    
    with app.app_context():
        try:
            # Find VMs that need renaming
            pending = VMAssignment.query.filter_by(hostname_configured=False).filter(
                VMAssignment.target_hostname.isnot(None)
            ).all()
            
            if not pending:
                return
            
            logger.info(f"Found {len(pending)} VMs pending hostname configuration")
            
            for assignment in pending:
                # Skip if recently attempted
                if assignment.proxmox_vmid in _rename_attempts:
                    attempt_data = _rename_attempts[assignment.proxmox_vmid]
                    if attempt_data['attempts'] >= MAX_RENAME_ATTEMPTS:
                        continue
                    if datetime.now() - attempt_data['last_attempt'] < timedelta(minutes=RETRY_COOLDOWN_MINUTES):
                        continue
                
                # Retry rename
                logger.info(f"Retrying hostname rename for VM {assignment.proxmox_vmid}")
                auto_rename_vm_after_boot(
                    assignment.proxmox_vmid,
                    assignment.target_hostname,
                    assignment.node or "pve",
                    app=app
                )
                
        except Exception as e:
            logger.error(f"Error checking pending renames: {e}", exc_info=True)


def mark_vm_as_renamed(vmid: int, hostname: str = None):
    """
    Manually mark a VM as already renamed (useful for templates that are already configured).
    
    Args:
        vmid: Proxmox VM ID
        hostname: Optional hostname to record (if known)
    """
    try:
        assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
        if assignment:
            assignment.hostname_configured = True
            if hostname:
                assignment.target_hostname = hostname
            db.session.commit()
            logger.info(f"Manually marked VM {vmid} as hostname_configured")
            
            # Clear retry tracking
            if vmid in _rename_attempts:
                del _rename_attempts[vmid]
                
    except Exception as e:
        logger.error(f"Failed to mark VM {vmid} as renamed: {e}")


def reset_hostname_flag(vmid: int):
    """
    Reset the hostname_configured flag to allow re-renaming.
    Useful for troubleshooting or re-deploying VMs.
    
    Args:
        vmid: Proxmox VM ID
    """
    try:
        assignment = VMAssignment.query.filter_by(proxmox_vmid=vmid).first()
        if assignment:
            assignment.hostname_configured = False
            db.session.commit()
            logger.info(f"Reset hostname_configured flag for VM {vmid}")
            
            # Clear retry tracking
            if vmid in _rename_attempts:
                del _rename_attempts[vmid]
                
    except Exception as e:
        logger.error(f"Failed to reset hostname flag for VM {vmid}: {e}")
