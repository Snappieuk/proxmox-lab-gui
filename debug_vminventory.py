#!/usr/bin/env python3
"""
Debug script to check VMInventory table status and test insertion.
"""

from app import create_app
from app.models import db, VMInventory
from sqlalchemy import text, inspect

def main():
    app = create_app()
    
    with app.app_context():
        print("=" * 60)
        print("VMInventory Debug Tool")
        print("=" * 60)
        
        # Check table exists
        inspector = inspect(db.engine)
        if 'vm_inventory' not in inspector.get_table_names():
            print("\n✗ Table vm_inventory does not exist!")
            return
        
        print("\n1. Table Schema:")
        columns = inspector.get_columns('vm_inventory')
        print(f"   Columns: {len(columns)}")
        for col in columns[:5]:  # Show first 5
            print(f"   - {col['name']}: {col['type']}")
        
        print("\n2. Constraints:")
        unique_constraints = inspector.get_unique_constraints('vm_inventory')
        for c in unique_constraints:
            print(f"   Unique: {c['name']} on {c['column_names']}")
        
        print("\n3. Indexes:")
        indexes = inspector.get_indexes('vm_inventory')
        for idx in indexes:
            unique = "UNIQUE" if idx.get('unique') else "NON-UNIQUE"
            print(f"   {unique}: {idx['name']} on {idx['column_names']}")
        
        print("\n4. Current Data:")
        total = db.session.execute(text("SELECT COUNT(*) FROM vm_inventory")).scalar()
        print(f"   Total records: {total}")
        
        if total > 0:
            # Show sample records
            samples = db.session.execute(text("""
                SELECT cluster_id, vmid, name, status 
                FROM vm_inventory 
                LIMIT 5
            """)).fetchall()
            print("   Sample records:")
            for cluster_id, vmid, name, status in samples:
                print(f"   - {cluster_id}/{vmid}: {name} [{status}]")
        
        # Check for duplicates
        dupes = db.session.execute(text("""
            SELECT cluster_id, vmid, COUNT(*) as count
            FROM vm_inventory
            GROUP BY cluster_id, vmid
            HAVING COUNT(*) > 1
        """)).fetchall()
        
        if dupes:
            print(f"\n   ⚠ DUPLICATES FOUND: {len(dupes)}")
            for cluster_id, vmid, count in dupes[:5]:
                print(f"     - {cluster_id}/{vmid}: {count} copies")
        else:
            print("   ✓ No duplicates")
        
        print("\n5. Test Insert:")
        test_cluster = "test_cluster"
        test_vmid = 999999
        
        # Try to insert a test record
        try:
            test_vm = VMInventory(
                cluster_id=test_cluster,
                vmid=test_vmid,
                name="Test VM",
                node="test-node",
                status="unknown",
                type="qemu"
            )
            db.session.add(test_vm)
            db.session.flush()
            print(f"   ✓ Successfully created test record {test_cluster}/{test_vmid}")
            
            # Clean up
            db.session.delete(test_vm)
            db.session.commit()
            print("   ✓ Cleaned up test record")
            
        except Exception as e:
            print(f"   ✗ Failed to create test record: {e}")
            db.session.rollback()
        
        print("\n" + "=" * 60)
        print("Debug complete")
        print("=" * 60)

if __name__ == "__main__":
    main()
