#!/usr/bin/env python3
"""
Database migration: Add clone_task_id column to classes table

Run this script to add the new clone_task_id field needed for progress tracking.
"""

from app import create_app
from app.models import db
from sqlalchemy import text

def migrate():
    app = create_app()
    with app.app_context():
        try:
            # Check if column already exists
            result = db.session.execute(text("PRAGMA table_info(classes)"))
            columns = [row[1] for row in result]
            
            if 'clone_task_id' in columns:
                print("✓ Column 'clone_task_id' already exists in classes table")
                return
            
            print("Adding 'clone_task_id' column to classes table...")
            db.session.execute(text("ALTER TABLE classes ADD COLUMN clone_task_id VARCHAR(64)"))
            db.session.commit()
            print("✓ Migration complete! Column 'clone_task_id' added successfully")
            
        except Exception as e:
            print(f"✗ Migration failed: {e}")
            db.session.rollback()
            raise

if __name__ == '__main__':
    migrate()
