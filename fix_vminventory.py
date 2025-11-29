#!/usr/bin/env python3
"""
Migration script to add unique constraint to VMInventory table.

This fixes the "Failed to create or find VMInventory" error by:
1. Removing duplicate VMInventory records (keeping most recent)
2. Adding unique constraint on (cluster_id, vmid)

Run this on the remote server where the database exists:
    python3 fix_vminventory.py
"""

from app import create_app
from app.models import db, VMInventory
from sqlalchemy import text

def main():
    app = create_app()
    
    with app.app_context():
        print("Checking VMInventory table...")
        
        # Find and remove duplicates (keep most recent by id)
        duplicates = db.session.execute(text("""
            SELECT cluster_id, vmid, COUNT(*) as count
            FROM vm_inventory
            GROUP BY cluster_id, vmid
            HAVING COUNT(*) > 1
        """)).fetchall()
        
        if duplicates:
            print(f"Found {len(duplicates)} duplicate cluster_id/vmid combinations")
            for cluster_id, vmid, count in duplicates:
                print(f"  Removing duplicates for cluster={cluster_id}, vmid={vmid} ({count} records)")
                
                # Keep the newest record (highest id), delete older ones
                records = VMInventory.query.filter_by(
                    cluster_id=cluster_id,
                    vmid=vmid
                ).order_by(VMInventory.id.desc()).all()
                
                # Keep first (newest), delete rest
                for record in records[1:]:
                    db.session.delete(record)
                    print(f"    Deleted id={record.id}")
            
            db.session.commit()
            print("Duplicates removed successfully")
        else:
            print("No duplicates found")
        
        # Check if constraint already exists
        inspector = db.inspect(db.engine)
        constraints = inspector.get_unique_constraints('vm_inventory')
        constraint_exists = any(c['name'] == 'uq_cluster_vmid' for c in constraints)
        
        if constraint_exists:
            print("Unique constraint already exists - nothing to do")
        else:
            print("Adding unique constraint...")
            try:
                # SQLite doesn't support ADD CONSTRAINT directly, need to recreate table
                # But SQLAlchemy will handle this automatically on next table creation
                # For now, just ensure no duplicates exist
                db.session.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_vmid 
                    ON vm_inventory(cluster_id, vmid)
                """))
                db.session.commit()
                print("Unique index created successfully")
            except Exception as e:
                print(f"Note: Could not create unique index (may need table recreation): {e}")
                print("The unique constraint in models.py will take effect on next db.create_all()")
        
        # Show current state
        total = VMInventory.query.count()
        print(f"\nFinal state: {total} total VMInventory records")
        
        # Test query to ensure no duplicates remain
        remaining_dupes = db.session.execute(text("""
            SELECT cluster_id, vmid, COUNT(*) as count
            FROM vm_inventory
            GROUP BY cluster_id, vmid
            HAVING COUNT(*) > 1
        """)).fetchall()
        
        if remaining_dupes:
            print(f"WARNING: {len(remaining_dupes)} duplicates still exist!")
            for cluster_id, vmid, count in remaining_dupes:
                print(f"  cluster={cluster_id}, vmid={vmid}, count={count}")
        else:
            print("✓ No duplicate records - database is clean")

if __name__ == "__main__":
    main()
