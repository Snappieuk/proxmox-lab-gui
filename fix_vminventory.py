#!/usr/bin/env python3
"""
Migration script to fix VMInventory table duplicates.

This fixes the "Failed to create or find VMInventory" error by:
1. Removing duplicate VMInventory records (keeping most recent)
2. Creating unique index on (cluster_id, vmid) if not exists

Run this on the remote server where the database exists:
    python3 fix_vminventory.py
"""

import sys
from app import create_app
from app.models import db, VMInventory
from sqlalchemy import text, inspect

def main():
    app = create_app()
    
    with app.app_context():
        print("=" * 60)
        print("VMInventory Table Repair Tool")
        print("=" * 60)
        
        # Check if table exists
        inspector = inspect(db.engine)
        if 'vm_inventory' not in inspector.get_table_names():
            print("✓ vm_inventory table does not exist yet - will be created correctly on first run")
            return
        
        print("\n1. Checking for duplicate records...")
        
        # Find duplicates
        duplicates = db.session.execute(text("""
            SELECT cluster_id, vmid, COUNT(*) as count
            FROM vm_inventory
            GROUP BY cluster_id, vmid
            HAVING COUNT(*) > 1
        """)).fetchall()
        
        if duplicates:
            print(f"   Found {len(duplicates)} duplicate cluster_id/vmid combinations:")
            
            for cluster_id, vmid, count in duplicates:
                print(f"   - cluster={cluster_id}, vmid={vmid} ({count} records)")
                
                # Keep the newest record (highest id), delete older ones
                records = VMInventory.query.filter_by(
                    cluster_id=cluster_id,
                    vmid=vmid
                ).order_by(VMInventory.id.desc()).all()
                
                # Keep first (newest), delete rest
                for record in records[1:]:
                    db.session.delete(record)
                    print(f"     Deleted duplicate id={record.id}")
            
            db.session.commit()
            print("   ✓ Duplicates removed")
        else:
            print("   ✓ No duplicates found")
        
        print("\n2. Checking unique constraint/index...")
        
        # Check constraints and indexes
        unique_constraints = inspector.get_unique_constraints('vm_inventory')
        indexes = inspector.get_indexes('vm_inventory')
        
        has_constraint = any(
            c.get('name') in ('uix_cluster_vmid', 'uq_cluster_vmid') 
            for c in unique_constraints
        )
        
        has_unique_index = any(
            idx.get('unique') and 
            set(idx.get('column_names', [])) == {'cluster_id', 'vmid'}
            for idx in indexes
        )
        
        if has_constraint or has_unique_index:
            print("   ✓ Unique constraint already exists")
        else:
            print("   Creating unique index...")
            try:
                db.session.execute(text("""
                    CREATE UNIQUE INDEX idx_cluster_vmid 
                    ON vm_inventory(cluster_id, vmid)
                """))
                db.session.commit()
                print("   ✓ Unique index created")
            except Exception as e:
                print(f"   ⚠ Could not create unique index: {e}")
                print("   Note: The table will be recreated with proper constraints on app restart")
        
        print("\n3. Verifying final state...")
        
        # Verify no duplicates remain
        remaining_dupes = db.session.execute(text("""
            SELECT cluster_id, vmid, COUNT(*) as count
            FROM vm_inventory
            GROUP BY cluster_id, vmid
            HAVING COUNT(*) > 1
        """)).fetchall()
        
        if remaining_dupes:
            print(f"   ✗ ERROR: {len(remaining_dupes)} duplicates still exist!")
            for cluster_id, vmid, count in remaining_dupes:
                print(f"     - cluster={cluster_id}, vmid={vmid}, count={count}")
            sys.exit(1)
        
        total = VMInventory.query.count()
        print(f"   ✓ Database clean: {total} unique VM records")
        
        print("\n" + "=" * 60)
        print("✓ VMInventory repair completed successfully")
        print("=" * 60)
        print("\nNext steps:")
        print("  1. Restart the application: sudo systemctl restart proxmox-gui")
        print("  2. Check logs for any remaining errors")

if __name__ == "__main__":
    main()
