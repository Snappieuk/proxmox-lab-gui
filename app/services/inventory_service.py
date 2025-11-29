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
    status, ip, type, category.
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
    cluster_vm_pairs = [(vm.get("cluster_id"), int(vm.get("vmid"))) for vm in vms if vm.get("vmid") is not None]
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

        row.cluster_name = vm.get("cluster_name") or row.cluster_name
        row.name = vm.get("name") or row.name
        row.node = vm.get("node") or row.node
        row.status = vm.get("status") or row.status
        row.ip = ip_effective
        row.type = vm.get("type") or row.type
        row.category = vm.get("category") or row.category
        row.last_updated = now

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
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
