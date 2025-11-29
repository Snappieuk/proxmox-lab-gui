#!/usr/bin/env python3
"""
Migration script to fix VMInventory table duplicates and schema.

This fixes the "Failed to create or find VMInventory" error by:
1. Backing up existing VMInventory data
2. Dropping and recreating table with correct schema
3. Restoring data (removing duplicates)

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
            print("\n✓ vm_inventory table does not exist yet")
            print("  It will be created correctly on first app run")
            return
        
        print("\n1. Backing up existing data...")
        
        # Get all existing records
        try:
            # First check if we can even read the table
            total_count = db.session.execute(text("""
                SELECT COUNT(*) FROM vm_inventory
            """)).scalar()
            
            print(f"   Found {total_count} total records in table")
            
            # Check for duplicates before backup
            duplicates = db.session.execute(text("""
                SELECT cluster_id, vmid, COUNT(*) as count
                FROM vm_inventory
                GROUP BY cluster_id, vmid
                HAVING COUNT(*) > 1
            """)).fetchall()
            
            if duplicates:
                print(f"   ⚠ Warning: Found {len(duplicates)} duplicate keys:")
                for cluster_id, vmid, count in duplicates[:5]:  # Show first 5
                    print(f"     - {cluster_id}/{vmid} ({count} copies)")
                if len(duplicates) > 5:
                    print(f"     ... and {len(duplicates) - 5} more")
            
            all_records = db.session.execute(text("""
                SELECT * FROM vm_inventory ORDER BY id
            """)).fetchall()
            
            if all_records:
                print(f"   Found {len(all_records)} records to backup")
                
                # Get column names
                columns = db.session.execute(text("""
                    SELECT * FROM vm_inventory LIMIT 0
                """)).keys()
                
                backup_data = []
                for row in all_records:
                    backup_data.append(dict(zip(columns, row)))
                
                print(f"   ✓ Backed up {len(backup_data)} records")
            else:
                backup_data = []
                print("   ✓ No data to backup")
        except Exception as e:
            print(f"   ✗ Error reading table: {e}")
            print("\n   The table may be corrupted. Proceeding with recreation...")
            backup_data = []
        
        print("\n2. Dropping and recreating table...")
        
        try:
            # Drop the table
            db.session.execute(text("DROP TABLE IF EXISTS vm_inventory"))
            db.session.commit()
            print("   ✓ Old table dropped")
            
            # Recreate with correct schema
            VMInventory.__table__.create(db.engine)
            print("   ✓ New table created with correct schema")
            
        except Exception as e:
            print(f"   ✗ Error recreating table: {e}")
            sys.exit(1)
        
        if backup_data:
            print(f"\n3. Restoring data (removing duplicates)...")
            
            # Track unique (cluster_id, vmid) pairs to skip duplicates
            seen = set()
            restored = 0
            skipped = 0
            
            for record in backup_data:
                key = (record.get('cluster_id'), record.get('vmid'))
                
                if key in seen:
                    skipped += 1
                    continue
                
                seen.add(key)
                
                try:
                    # Create new inventory record (don't include id to get new autoincrement)
                    inv = VMInventory(
                        cluster_id=record.get('cluster_id'),
                        vmid=record.get('vmid'),
                        name=record.get('name'),
                        node=record.get('node'),
                        status=record.get('status', 'unknown'),
                        type=record.get('type', 'qemu'),
                        category=record.get('category'),
                        ip=record.get('ip'),
                        mac_address=record.get('mac_address'),
                        memory=record.get('memory'),
                        cores=record.get('cores'),
                        disk_size=record.get('disk_size'),
                        uptime=record.get('uptime'),
                        cpu_usage=record.get('cpu_usage'),
                        memory_usage=record.get('memory_usage'),
                        is_template=record.get('is_template', False),
                        tags=record.get('tags'),
                        rdp_available=record.get('rdp_available', False),
                        ssh_available=record.get('ssh_available', False),
                        sync_error=record.get('sync_error')
                    )
                    db.session.add(inv)
                    restored += 1
                    
                except Exception as e:
                    print(f"   Warning: Could not restore {key}: {e}")
            
            db.session.commit()
            print(f"   ✓ Restored {restored} records")
            if skipped > 0:
                print(f"   ✓ Skipped {skipped} duplicate records")
        else:
            print("\n3. No data to restore")
        
        print("\n4. Verifying final state...")
        
        # Verify schema
        inspector = inspect(db.engine)
        unique_constraints = inspector.get_unique_constraints('vm_inventory')
        indexes = inspector.get_indexes('vm_inventory')
        
        has_unique = any(
            c.get('name') in ('uix_cluster_vmid', 'uq_cluster_vmid') 
            for c in unique_constraints
        )
        
        if has_unique:
            print("   ✓ Unique constraint exists")
        else:
            print("   ⚠ Warning: Unique constraint not found (may be OK)")
        
        # Check for any duplicates
        dupes = db.session.execute(text("""
            SELECT cluster_id, vmid, COUNT(*) as count
            FROM vm_inventory
            GROUP BY cluster_id, vmid
            HAVING COUNT(*) > 1
        """)).fetchall()
        
        if dupes:
            print(f"   ✗ ERROR: {len(dupes)} duplicates found!")
            for cluster_id, vmid, count in dupes:
                print(f"     - cluster={cluster_id}, vmid={vmid}, count={count}")
            sys.exit(1)
        
        total = VMInventory.query.count()
        print(f"   ✓ Database clean: {total} unique VM records")
        
        print("\n" + "=" * 60)
        print("✓ VMInventory repair completed successfully")
        print("=" * 60)
        print("\nNext steps:")
        print("  1. Restart the application:")
        print("     sudo systemctl restart proxmox-gui")
        print("  2. Check logs for any remaining errors:")
        print("     sudo journalctl -u proxmox-gui -f")

if __name__ == "__main__":
    main()
