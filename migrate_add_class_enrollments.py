#!/usr/bin/env python3
"""
Database migration script to add class enrollment tracking.

This allows students to join classes before VMs are assigned.
Run this script to update the database schema.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.models import db, User, Class, VMAssignment

def migrate_database():
    """Add class_enrollments table and migrate existing data."""
    app = create_app()
    
    with app.app_context():
        print("Starting database migration for class enrollments...")
        
        # Check if table already exists
        inspector = db.inspect(db.engine)
        tables = inspector.get_table_names()
        
        if 'class_enrollments' in tables:
            print("✓ Table 'class_enrollments' already exists. No migration needed.")
            return
        
        print("Creating 'class_enrollments' table...")
        
        try:
            # Create the association table
            db.create_all()
            
            print("✓ Successfully created class_enrollments table!")
            
            # Migrate existing VM assignments to enrollments
            print("\nMigrating existing VM assignments to enrollments...")
            
            assignments = VMAssignment.query.filter(
                VMAssignment.assigned_user_id.isnot(None)
            ).all()
            
            enrolled_count = 0
            for assignment in assignments:
                user = User.query.get(assignment.assigned_user_id)
                class_ = Class.query.get(assignment.class_id)
                
                if user and class_ and user not in class_.students:
                    class_.students.append(user)
                    enrolled_count += 1
            
            db.session.commit()
            
            print(f"✓ Migrated {enrolled_count} existing VM assignments to class enrollments")
            print("\nMigration complete!")
            print("\nUsers can now join classes even if no VMs are available.")
            print("They will be automatically assigned a VM when one becomes available.")
            
        except Exception as e:
            print(f"✗ Migration failed: {e}")
            sys.exit(1)

if __name__ == '__main__':
    migrate_database()
