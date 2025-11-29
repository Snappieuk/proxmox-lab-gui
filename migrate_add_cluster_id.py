#!/usr/bin/env python3
"""
Database migration script to add cluster_id column to vm_inventory table.

This script safely adds the cluster_id column and sets default values.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.models import db
from sqlalchemy import text

def migrate_add_cluster_id():
    """Add cluster_id column to vm_inventory table."""
    app = create_app()
    
    with app.app_context():
        try:
            # Check if column already exists
            result = db.session.execute(text(
                "SELECT COUNT(*) as cnt FROM pragma_table_info('vm_inventory') WHERE name='cluster_id'"
            ))
            row = result.fetchone()
            
            if row[0] > 0:
                print("✓ Column 'cluster_id' already exists in vm_inventory table")
                return True
            
            print("Adding 'cluster_id' column to vm_inventory table...")
            
            # Add the column (SQLite doesn't support NOT NULL with default in ALTER TABLE)
            db.session.execute(text(
                "ALTER TABLE vm_inventory ADD COLUMN cluster_id VARCHAR(50)"
            ))
            
            # Set default value for existing rows (use first cluster from config)
            from app.config import CLUSTERS
            if CLUSTERS:
                default_cluster_id = CLUSTERS[0]['id']
                print(f"Setting default cluster_id='{default_cluster_id}' for existing rows...")
                db.session.execute(text(
                    f"UPDATE vm_inventory SET cluster_id = :cluster_id WHERE cluster_id IS NULL"
                ), {"cluster_id": default_cluster_id})
            
            db.session.commit()
            print("✓ Successfully added cluster_id column and updated existing rows")
            return True
            
        except Exception as e:
            db.session.rollback()
            print(f"✗ Migration failed: {e}")
            import traceback
            traceback.print_exc()
            return False

if __name__ == "__main__":
    success = migrate_add_cluster_id()
    sys.exit(0 if success else 1)
