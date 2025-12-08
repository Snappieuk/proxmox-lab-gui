"""
Auto-shutdown service for monitoring and shutting down idle VMs.

Monitors VMs in classes with auto-shutdown enabled and shuts them down
if CPU usage stays below threshold for the configured duration.
"""
import logging
import time
from datetime import datetime, timedelta
from threading import Thread, Event
from typing import Dict, List, Optional

from app.models import Class, VMAssignment, db
from app.services.proxmox_service import get_proxmox_admin_for_cluster

logger = logging.getLogger(__name__)

# Track VM idle states: {vmid: {'cluster': str, 'class_id': int, 'idle_since': datetime, 'low_cpu_checks': int}}
vm_idle_tracker: Dict[int, dict] = {}

# Shutdown daemon control
shutdown_daemon_active = False
shutdown_daemon_thread: Optional[Thread] = None
shutdown_daemon_stop_event = Event()

CHECK_INTERVAL = 300  # Check every 5 minutes


def start_auto_shutdown_daemon(app):
    """Start the auto-shutdown background daemon."""
    global shutdown_daemon_active, shutdown_daemon_thread, shutdown_daemon_stop_event
    
    if shutdown_daemon_active:
        logger.info("Auto-shutdown daemon already running")
        return
    
    shutdown_daemon_stop_event.clear()
    shutdown_daemon_thread = Thread(
        target=_auto_shutdown_daemon_worker,
        args=(app,),
        daemon=True,
        name="AutoShutdownDaemon"
    )
    shutdown_daemon_thread.start()
    shutdown_daemon_active = True
    logger.info("Auto-shutdown daemon started")


def stop_auto_shutdown_daemon():
    """Stop the auto-shutdown background daemon."""
    global shutdown_daemon_active, shutdown_daemon_stop_event
    
    if not shutdown_daemon_active:
        return
    
    logger.info("Stopping auto-shutdown daemon...")
    shutdown_daemon_stop_event.set()
    shutdown_daemon_active = False


def _auto_shutdown_daemon_worker(app):
    """Background worker that checks VMs and shuts down idle ones."""
    logger.info("Auto-shutdown daemon worker started")
    
    while not shutdown_daemon_stop_event.is_set():
        try:
            with app.app_context():
                check_and_shutdown_idle_vms()
        except Exception as e:
            logger.error(f"Error in auto-shutdown daemon: {e}", exc_info=True)
        
        # Wait for next check (or until stop event)
        shutdown_daemon_stop_event.wait(CHECK_INTERVAL)
    
    logger.info("Auto-shutdown daemon worker stopped")


def check_and_shutdown_idle_vms():
    """Check all VMs and enforce auto-shutdown, hour restrictions, and session limits."""
    # Get all classes (check all settings, not just auto-shutdown)
    classes = Class.query.filter(
        (Class.auto_shutdown_enabled == True) | 
        (Class.restrict_hours == True) | 
        (Class.max_session_minutes > 0)
    ).all()
    
    if not classes:
        return
    
    logger.info(f"Checking restrictions for {len(classes)} classes")
    
    for class_ in classes:
        try:
            check_class_vms(class_)
        except Exception as e:
            logger.error(f"Error checking class {class_.id} ({class_.name}): {e}", exc_info=True)


def check_class_vms(class_: Class):
    """Check VMs in a specific class for all restrictions."""
    from datetime import datetime
    
    # Get student VMs (exclude teacher and template VMs)
    vms = VMAssignment.query.filter_by(
        class_id=class_.id,
        is_teacher_vm=False,
        is_template_vm=False
    ).all()
    
    if not vms:
        return
    
    current_hour = datetime.now().hour
    
    # Build restriction summary
    restrictions = []
    if class_.auto_shutdown_enabled:
        restrictions.append(f"CPU<{class_.auto_shutdown_cpu_threshold}%/{class_.auto_shutdown_idle_minutes}min")
    if class_.restrict_hours:
        restrictions.append(f"hours:{class_.hours_start}-{class_.hours_end}")
    if class_.max_session_minutes > 0:
        restrictions.append(f"max:{class_.max_session_minutes}min")
    
    logger.info(f"Checking {len(vms)} VMs in class {class_.name} ({', '.join(restrictions)})")
    
    cluster_id = class_.deployment_cluster
    if not cluster_id:
        logger.warning(f"Class {class_.id} has no deployment_cluster set")
        return
    
    try:
        proxmox = get_proxmox_admin_for_cluster(int(cluster_id))
    except Exception as e:
        logger.error(f"Failed to get Proxmox connection for cluster {cluster_id}: {e}")
        return
    
    for vm in vms:
        try:
            # Check hour restrictions first (highest priority)
            if class_.restrict_hours:
                if not is_within_allowed_hours(current_hour, class_.hours_start, class_.hours_end):
                    shutdown_vm_outside_hours(vm, proxmox, class_)
                    continue
            
            # Check session duration limits
            if class_.max_session_minutes > 0:
                if should_shutdown_for_session_limit(vm, proxmox, class_.max_session_minutes):
                    continue
            
            # Check idle auto-shutdown
            if class_.auto_shutdown_enabled:
                check_vm_and_shutdown_if_idle(
                    vm=vm,
                    proxmox=proxmox,
                    cpu_threshold=class_.auto_shutdown_cpu_threshold or 20,
                    idle_minutes=class_.auto_shutdown_idle_minutes or 30,
                    class_id=class_.id
                )
        except Exception as e:
            logger.error(f"Error checking VM {vm.proxmox_vmid}: {e}", exc_info=True)


def is_within_allowed_hours(current_hour: int, start_hour: int, end_hour: int) -> bool:
    """Check if current hour is within allowed range."""
    if start_hour <= end_hour:
        # Normal range (e.g., 8-17)
        return start_hour <= current_hour < end_hour
    else:
        # Wraps midnight (e.g., 22-6)
        return current_hour >= start_hour or current_hour < end_hour


def shutdown_vm_outside_hours(vm: VMAssignment, proxmox, class_: Class):
    """Shutdown VM that's running outside allowed hours."""
    vmid = vm.proxmox_vmid
    node = vm.node
    
    if not node:
        return
    
    try:
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
        if status.get('status') == 'running':
            logger.info(
                f"VM {vmid} running outside allowed hours "
                f"(current: {datetime.now().hour}, allowed: {class_.hours_start}-{class_.hours_end}), shutting down..."
            )
            proxmox.nodes(node).qemu(vmid).status.shutdown.post()
            logger.info(f"Successfully initiated shutdown for VM {vmid} (outside hours)")
    except Exception as e:
        logger.error(f"Failed to shutdown VM {vmid} (outside hours): {e}")


def should_shutdown_for_session_limit(vm: VMAssignment, proxmox, max_minutes: int) -> bool:
    """Check if VM has exceeded session duration and shut it down."""
    vmid = vm.proxmox_vmid
    node = vm.node
    
    if not node:
        return False
    
    try:
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
        if status.get('status') != 'running':
            return False
        
        # Get uptime in seconds
        uptime_seconds = status.get('uptime', 0)
        uptime_minutes = uptime_seconds / 60
        
        if uptime_minutes >= max_minutes:
            logger.info(
                f"VM {vmid} exceeded session limit "
                f"(uptime: {uptime_minutes:.1f}min, max: {max_minutes}min), shutting down..."
            )
            proxmox.nodes(node).qemu(vmid).status.shutdown.post()
            logger.info(f"Successfully initiated shutdown for VM {vmid} (session limit)")
            return True
    except Exception as e:
        logger.error(f"Failed to check/shutdown VM {vmid} (session limit): {e}")
    
    return False


def check_vm_and_shutdown_if_idle(vm: VMAssignment, proxmox, cpu_threshold: int, idle_minutes: int, class_id: int):
    """Check a single VM and shut it down if idle."""
    vmid = vm.proxmox_vmid
    node = vm.node
    
    if not node:
        logger.debug(f"VM {vmid} has no node assigned, skipping")
        return
    
    # Check if VM is running
    try:
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
        if status.get('status') != 'running':
            # VM not running, clear from tracker
            if vmid in vm_idle_tracker:
                del vm_idle_tracker[vmid]
            return
    except Exception as e:
        logger.debug(f"Failed to get status for VM {vmid}: {e}")
        return
    
    # Get CPU usage
    try:
        # CPU usage is a percentage (0.0 to 1.0)
        cpu_usage = status.get('cpu', 0) * 100
    except Exception as e:
        logger.debug(f"Failed to get CPU for VM {vmid}: {e}")
        return
    
    logger.debug(f"VM {vmid}: CPU={cpu_usage:.1f}%")
    
    # Check if CPU is below threshold
    if cpu_usage < cpu_threshold:
        # VM is idle
        if vmid not in vm_idle_tracker:
            # First time seeing this VM idle
            vm_idle_tracker[vmid] = {
                'cluster': vm.cluster,
                'class_id': class_id,
                'idle_since': datetime.now(),
                'low_cpu_checks': 1,
                'node': node
            }
            logger.info(f"VM {vmid} idle detected (CPU {cpu_usage:.1f}% < {cpu_threshold}%)")
        else:
            # VM was already idle, increment counter
            vm_idle_tracker[vmid]['low_cpu_checks'] += 1
            
            # Check if VM has been idle long enough
            idle_duration = datetime.now() - vm_idle_tracker[vmid]['idle_since']
            
            # Calculate how many checks needed (CHECK_INTERVAL is in seconds)
            checks_needed = (idle_minutes * 60) / CHECK_INTERVAL
            
            if vm_idle_tracker[vmid]['low_cpu_checks'] >= checks_needed:
                # VM has been idle long enough, shut it down
                logger.info(
                    f"VM {vmid} has been idle for {idle_duration.total_seconds()/60:.1f} minutes "
                    f"({vm_idle_tracker[vmid]['low_cpu_checks']} checks), shutting down..."
                )
                
                try:
                    # Shutdown the VM
                    proxmox.nodes(node).qemu(vmid).status.shutdown.post()
                    logger.info(f"Successfully initiated shutdown for VM {vmid}")
                    
                    # Remove from tracker
                    del vm_idle_tracker[vmid]
                except Exception as e:
                    logger.error(f"Failed to shutdown VM {vmid}: {e}")
            else:
                logger.debug(
                    f"VM {vmid} idle: {vm_idle_tracker[vmid]['low_cpu_checks']}/{int(checks_needed)} checks, "
                    f"{idle_duration.total_seconds()/60:.1f}/{idle_minutes} minutes"
                )
    else:
        # VM is active, clear from tracker
        if vmid in vm_idle_tracker:
            logger.info(f"VM {vmid} is now active (CPU {cpu_usage:.1f}%), clearing idle state")
            del vm_idle_tracker[vmid]


def get_idle_vm_stats() -> Dict:
    """Get statistics about currently tracked idle VMs."""
    stats = {
        'tracked_vms': len(vm_idle_tracker),
        'vms': []
    }
    
    for vmid, data in vm_idle_tracker.items():
        idle_duration = datetime.now() - data['idle_since']
        stats['vms'].append({
            'vmid': vmid,
            'class_id': data['class_id'],
            'idle_minutes': idle_duration.total_seconds() / 60,
            'low_cpu_checks': data['low_cpu_checks']
        })
    
    return stats
