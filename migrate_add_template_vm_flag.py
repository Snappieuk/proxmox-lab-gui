#!/usr/bin/env python3
"""
Database migration: Add is_template_vm column to vm_assignments table.

This migration adds a boolean field to track which VM in a class is the
template reference VM (the original converted template kept for reference).
"""

import sys
from app import create_app
from app.models import db

def migrate():
    app = create_app()
    with app.app_context():
        try:
            # Check if column already exists
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            columns = [col['name'] for col in inspector.get_columns('vm_assignments')]
            
            if 'is_template_vm' in columns:
                print("✓ Column 'is_template_vm' already exists in vm_assignments table")
                return
            
            # Add the column
            print("Adding 'is_template_vm' column to vm_assignments table...")
            from sqlalchemy import text
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE vm_assignments ADD COLUMN is_template_vm BOOLEAN DEFAULT 0'))
                conn.commit()
            
            print("✓ Successfully added is_template_vm column")
            print("\nMigration complete!")
            
        except Exception as e:
            print(f"✗ Migration failed: {e}")
            sys.exit(1)

if __name__ == '__main__':
    migrate()
