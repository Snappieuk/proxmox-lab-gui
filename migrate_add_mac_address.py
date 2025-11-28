#!/usr/bin/env python3
"""
Database migration script to add MAC address tracking to VMAssignment table.

Run this script to update the database schema.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.models import db

def migrate_database():
    """Add mac_address column to vm_assignments table."""
    app = create_app()
    
    with app.app_context():
        print("Starting database migration...")
        
        # Check if column already exists
        inspector = db.inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('vm_assignments')]
        
        if 'mac_address' in columns:
            print("✓ Column 'mac_address' already exists. No migration needed.")
            return
        
        print("Adding 'mac_address' column to vm_assignments table...")
        
        try:
            # SQLite doesn't support ALTER TABLE ADD COLUMN with constraints easily,
            # so we use raw SQL
            with db.engine.connect() as conn:
                conn.execute(db.text(
                    "ALTER TABLE vm_assignments ADD COLUMN mac_address VARCHAR(17)"
                ))
                conn.commit()
            
            print("✓ Successfully added mac_address column!")
            print("\nNote: Existing VM assignments will have NULL mac_address.")
            print("MAC addresses will be populated when VMs are next created or pushed.")
            
        except Exception as e:
            print(f"✗ Migration failed: {e}")
            sys.exit(1)

if __name__ == '__main__':
    migrate_database()
