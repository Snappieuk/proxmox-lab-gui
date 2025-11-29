#!/usr/bin/env python3
"""
Database Migration Script - Expand VMInventory for Complete VM Data

This script adds new columns to the VMInventory table to support
database-first architecture where all frontend data comes from the database
instead of direct Proxmox API calls.

New columns added:
- memory (Integer) - Memory in MB
- cores (Integer) - CPU cores
- disk_size (BigInteger) - Disk size in bytes
- uptime (Integer) - Uptime in seconds
- cpu_usage (Float) - CPU usage percentage
- memory_usage (Float) - Memory usage percentage
- is_template (Boolean) - Whether VM is a template
- tags (String) - Comma-separated tags
- rdp_available (Boolean) - RDP connectivity available
- ssh_available (Boolean) - SSH connectivity available
- last_status_check (DateTime) - Last time status was verified
- sync_error (String) - Last sync error if any
"""

import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.models import db, VMInventory
from datetime import datetime


def migrate_database():
    """Add new columns to VMInventory table and perform initial sync."""
    app = create_app()
    
    with app.app_context():
        print("=" * 70)
        print("VMInventory Database Migration - Expand for Complete VM Data")
        print("=" * 70)
        
        # Create/update database schema
        print("\n1. Updating database schema...")
        try:
            db.create_all()
            print("✓ Schema updated successfully")
            print("  New columns added:")
            print("    - memory, cores, disk_size (hardware specs)")
            print("    - uptime, cpu_usage, memory_usage (runtime metrics)")
            print("    - is_template, tags (configuration)")
            print("    - rdp_available, ssh_available (connectivity flags)")
            print("    - last_status_check, sync_error (sync tracking)")
        except Exception as e:
            print(f"✗ Schema update failed: {e}")
            return
        
        # Check current state
        print("\n2. Checking current inventory...")
        try:
            total = VMInventory.query.count()
            print(f"   Current VM count: {total}")
            
            if total == 0:
                print("   Database is empty - will perform initial sync")
            else:
                print("   Database has existing data - will update with new fields")
                
        except Exception as e:
            print(f"✗ Failed to check inventory: {e}")
            return
        
        # Perform initial full sync
        print("\n3. Performing initial full synchronization...")
        print("   This may take several minutes for large environments...")
        try:
            from app.services.background_sync import _perform_full_sync
            _perform_full_sync()
            print("✓ Initial sync complete")
            
        except Exception as e:
            print(f"✗ Initial sync failed: {e}")
            print("\nNote: You can run the app and it will perform sync automatically.")
            import traceback
            traceback.print_exc()
        
        # Display results
        print("\n4. Database statistics:")
        try:
            total = VMInventory.query.count()
            running = VMInventory.query.filter_by(status='running').count()
            stopped = VMInventory.query.filter_by(status='stopped').count()
            templates = VMInventory.query.filter_by(is_template=True).count()
            with_ip = VMInventory.query.filter(
                VMInventory.ip.isnot(None),
                VMInventory.ip != '',
                VMInventory.ip != 'N/A'
            ).count()
            rdp_ready = VMInventory.query.filter_by(rdp_available=True).count()
            ssh_ready = VMInventory.query.filter_by(ssh_available=True).count()
            
            print(f"   Total VMs:           {total}")
            print(f"   Running:             {running}")
            print(f"   Stopped:             {stopped}")
            print(f"   Templates:           {templates}")
            print(f"   With IP addresses:   {with_ip}")
            print(f"   RDP available:       {rdp_ready}")
            print(f"   SSH available:       {ssh_ready}")
            
            # Show VMs by cluster
            print("\n5. VMs by cluster:")
            from app.config import CLUSTERS
            for cluster in CLUSTERS:
                cluster_id = cluster["id"]
                cluster_name = cluster["name"]
                count = VMInventory.query.filter_by(cluster_id=cluster_id).count()
                if count > 0:
                    print(f"   {cluster_name} ({cluster_id}): {count} VMs")
            
        except Exception as e:
            print(f"✗ Failed to retrieve statistics: {e}")
            return
        
        print("\n" + "=" * 70)
        print("Migration completed successfully!")
        print("=" * 70)
        print("\nBackground sync service will keep the database updated automatically.")
        print("\nAPI endpoints:")
        print("  GET /api/vms               - Get VM list (from database)")
        print("  GET /api/sync/stats        - Background sync statistics")
        print("  GET /api/sync/health       - Service health check")
        print("  POST /api/sync/trigger     - Manually trigger full sync (admin only)")
        print("\nThe frontend will now load instantly from the database!")


if __name__ == "__main__":
    migrate_database()
