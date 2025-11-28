#!/usr/bin/env python3
"""
Database migration script to add missing columns.

This script safely adds columns to the database if they don't exist.
Run this after pulling changes that modify the database schema.
"""

import sqlite3
import sys
from pathlib import Path

# Database path
DB_PATH = Path(__file__).parent / "app" / "lab_portal.db"

def column_exists(cursor, table, column):
    """Check if a column exists in a table."""
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    return column in columns

def add_column_if_missing(cursor, table, column, definition):
    """Add a column to a table if it doesn't exist."""
    if column_exists(cursor, table, column):
        print(f"✓ Column {table}.{column} already exists")
        return False
    
    print(f"Adding column {table}.{column}...")
    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    print(f"✓ Added column {table}.{column}")
    return True

def table_exists(cursor, table):
    """Check if a table exists."""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cursor.fetchone() is not None

def create_class_enrollments_table(cursor):
    """Create class_enrollments table if it doesn't exist."""
    if table_exists(cursor, 'class_enrollments'):
        print("✓ Table class_enrollments already exists")
        return False
    
    print("Creating table class_enrollments...")
    cursor.execute("""
        CREATE TABLE class_enrollments (
            class_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            enrolled_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (class_id, user_id),
            FOREIGN KEY (class_id) REFERENCES classes (id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX ix_class_enrollments_class_id ON class_enrollments (class_id)")
    cursor.execute("CREATE INDEX ix_class_enrollments_user_id ON class_enrollments (user_id)")
    print("✓ Created table class_enrollments")
    return True

def main():
    """Run all migrations."""
    if not DB_PATH.exists():
        print(f"❌ Database not found at {DB_PATH}")
        print("Run the application first to create the database.")
        return 1
    
    print("=" * 60)
    print("Database Migration Script")
    print("=" * 60)
    print(f"Database: {DB_PATH}")
    print()
    
    # Backup database
    backup_path = DB_PATH.with_suffix('.db.backup')
    import shutil
    print(f"Creating backup: {backup_path}")
    shutil.copy2(DB_PATH, backup_path)
    print("✓ Backup created")
    print()
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        changes = []
        
        # Add mac_address column to vm_assignments
        if add_column_if_missing(cursor, 'vm_assignments', 'mac_address', 'VARCHAR(17)'):
            changes.append('vm_assignments.mac_address')
        
        # Add cached_ip column to vm_assignments
        if add_column_if_missing(cursor, 'vm_assignments', 'cached_ip', 'VARCHAR(45)'):
            changes.append('vm_assignments.cached_ip')
        
        # Add ip_updated_at column to vm_assignments
        if add_column_if_missing(cursor, 'vm_assignments', 'ip_updated_at', 'DATETIME'):
            changes.append('vm_assignments.ip_updated_at')
        
        # Create vm_ip_cache table
        if not table_exists(cursor, 'vm_ip_cache'):
            print("Creating table vm_ip_cache...")
            cursor.execute("""
                CREATE TABLE vm_ip_cache (
                    vmid INTEGER PRIMARY KEY,
                    mac_address VARCHAR(17),
                    cached_ip VARCHAR(45),
                    ip_updated_at DATETIME,
                    cluster_id VARCHAR(50)
                )
            """)
            cursor.execute("CREATE INDEX ix_vm_ip_cache_vmid ON vm_ip_cache (vmid)")
            cursor.execute("CREATE INDEX ix_vm_ip_cache_mac_address ON vm_ip_cache (mac_address)")
            print("✓ Created table vm_ip_cache")
            changes.append('vm_ip_cache table')
        
        # Create class_enrollments table
        if create_class_enrollments_table(cursor):
            changes.append('class_enrollments table')
        
        # Commit changes
        if changes:
            conn.commit()
            print()
            print("=" * 60)
            print("✅ Migration completed successfully!")
            print(f"Changes applied: {len(changes)}")
            for change in changes:
                print(f"  - {change}")
        else:
            print()
            print("=" * 60)
            print("✅ Database is already up to date!")
        print("=" * 60)
        
        conn.close()
        return 0
        
    except Exception as e:
        print()
        print("=" * 60)
        print(f"❌ Migration failed: {e}")
        print("=" * 60)
        print(f"Restoring backup from {backup_path}...")
        shutil.copy2(backup_path, DB_PATH)
        print("✓ Database restored from backup")
        return 1

if __name__ == "__main__":
    sys.exit(main())
