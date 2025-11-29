#!/usr/bin/env python3
"""Inventory service for persisting VM state to the database.

Provides helpers to upsert VM records returned from Proxmox queries and
retrieve them efficiently for UI filtering (cluster, search) without
re-querying Proxmox API.
"""

from datetime import datetime
from typing import List, Dict, Optional

from app.models import db, VMInventory


def persist_vm_inventory(vms: List[Dict[str, object]]) -> None:
    """Upsert a batch of VM dicts into `vm_inventory` table.

    Expected keys per vm dict: cluster_id, cluster_name, vmid, name, node,
    status, ip, type, category, memory, cores, disk_size, uptime, cpu_usage,
    memory_usage, is_template, tags, rdp_available, ssh_available.
    """
    if not vms:
        return
    
    from flask import has_app_context
    import logging
    
    if not has_app_context():
        logger = logging.getLogger(__name__)
        logger.warning("persist_vm_inventory: skipping (no app context)")
        return

    # Build map of existing rows for faster upsert (fetch only vmids in batch)
    # Filter out VMs without cluster_id (should not happen; defensive)
    cluster_vm_pairs = []
    skipped_missing_cluster = 0
    for vm in vms:
        if vm.get("vmid") is None:
            continue
        cid = vm.get("cluster_id")
        if not cid:
            skipped_missing_cluster += 1
            continue
        try:
            cluster_vm_pairs.append((cid, int(vm.get("vmid"))))
        except Exception:
            continue
    if skipped_missing_cluster:
        logging.getLogger(__name__).warning(
            "persist_vm_inventory: skipped %d VMs without cluster_id (ensure _build_vm_dict adds it)",
            skipped_missing_cluster
        )
    if not cluster_vm_pairs:
        return

    # Query existing rows
    existing = (
        VMInventory.query.filter(
            VMInventory.cluster_id.in_({c for c, _ in cluster_vm_pairs}),
            VMInventory.vmid.in_({v for _, v in cluster_vm_pairs})
        ).all()
    )
    existing_map = {(row.cluster_id, row.vmid): row for row in existing}

    now = datetime.utcnow()

    for vm in vms:
        try:
            cluster_id = vm.get("cluster_id")
            vmid = int(vm.get("vmid"))
        except Exception:
            continue
        key = (cluster_id, vmid)
        row = existing_map.get(key)
        if row is None:
            row = VMInventory(
                cluster_id=cluster_id,
                vmid=vmid,
            )
            existing_map[key] = row
            db.session.add(row)

        # Update mutable fields (preserve prior IP if new is placeholder)
        ip_new = vm.get("ip") or None
        if ip_new in ("Fetching...", "", "N/A", None):
            # Do not overwrite a previously known real IP with placeholder
            if row.ip and row.ip not in ("", "N/A", "Fetching..."):
                ip_effective = row.ip
            else:
                ip_effective = ip_new  # might remain placeholder
        else:
            ip_effective = ip_new

        # Update basic fields
        row.cluster_name = vm.get("cluster_name") or row.cluster_name
        row.name = vm.get("name") or row.name
        row.node = vm.get("node") or row.node
        row.status = vm.get("status") or row.status
        row.ip = ip_effective
        row.type = vm.get("type") or row.type
        row.category = vm.get("category") or row.category
        
        # Update hardware specs
        if vm.get("memory") is not None:
            row.memory = vm.get("memory")
        if vm.get("cores") is not None:
            row.cores = vm.get("cores")
        if vm.get("disk_size") is not None:
            row.disk_size = vm.get("disk_size")
        
        # Update runtime info
        if vm.get("uptime") is not None:
            row.uptime = vm.get("uptime")
        if vm.get("cpu_usage") is not None:
            row.cpu_usage = vm.get("cpu_usage")
        if vm.get("memory_usage") is not None:
            row.memory_usage = vm.get("memory_usage")
        
        # Update configuration
        if vm.get("is_template") is not None:
            row.is_template = vm.get("is_template")
        if vm.get("tags") is not None:
            row.tags = vm.get("tags")
        
        # Update availability flags
        if vm.get("rdp_available") is not None:
            row.rdp_available = vm.get("rdp_available")
        if vm.get("ssh_available") is not None:
            row.ssh_available = vm.get("ssh_available")
        
        # Update sync tracking
        row.last_updated = now
        row.last_status_check = now
        
        # Clear sync error on successful update
        if vm.get("sync_error"):
            row.sync_error = vm.get("sync_error")
        else:
            row.sync_error = None

    try:
        before = datetime.utcnow()
        db.session.commit()
        after = datetime.utcnow()
        import logging
        logging.getLogger(__name__).debug(
            "persist_vm_inventory: committed %d rows (batch=%d) in %.3fs",
            len(existing_map), len(vms), (after - before).total_seconds()
        )
    except Exception as e:
        db.session.rollback()
        logging.getLogger(__name__).warning(f"persist_vm_inventory: commit failed: {e}")
        raise


def fetch_vm_inventory(cluster_id: Optional[str] = None, search: Optional[str] = None) -> List[Dict[str, object]]:
    """Fetch persisted VM inventory with optional cluster & search filtering."""
    q = VMInventory.query
    if cluster_id:
        q = q.filter_by(cluster_id=cluster_id)
    if search:
        pattern = f"%{search.lower()}%"
        # Case-insensitive search on name, ip, vmid
        q = q.filter(
            db.or_(
                db.func.lower(VMInventory.name).like(pattern),
                db.func.lower(VMInventory.ip).like(pattern),
                db.cast(VMInventory.vmid, db.String).like(pattern)
            )
        )
    rows = q.order_by(VMInventory.name.asc()).all()
    return [r.to_dict() for r in rows]
