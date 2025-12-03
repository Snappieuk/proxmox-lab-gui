#!/usr/bin/env python3
"""
Comprehensive database migration script.
Ensures all tables and columns exist as defined in models.py.
"""

import sqlite3
import sys
import os

DB_PATH = 'app/lab_portal.db'

# Critical columns that must exist
CRITICAL_MIGRATIONS = [
    ('classes', 'cpu_cores', 'INTEGER', '2'),
    ('classes', 'memory_mb', 'INTEGER', '2048'),
    ('vm_assignments', 'manually_added', 'BOOLEAN', '0'),
    ('vm_assignments', 'is_teacher_vm', 'BOOLEAN', '0'),
    # Template spec caching columns
    ('templates', 'cpu_cores', 'INTEGER', 'NULL'),
    ('templates', 'cpu_sockets', 'INTEGER', 'NULL'),
    ('templates', 'memory_mb', 'INTEGER', 'NULL'),
    ('templates', 'disk_size_gb', 'REAL', 'NULL'),
    ('templates', 'disk_storage', 'TEXT', 'NULL'),
    ('templates', 'disk_path', 'TEXT', 'NULL'),
    ('templates', 'disk_format', 'TEXT', 'NULL'),
    ('templates', 'network_bridge', 'TEXT', 'NULL'),
    ('templates', 'os_type', 'TEXT', 'NULL'),
    ('templates', 'specs_cached_at', 'TIMESTAMP', 'NULL'),
]

def migrate():
    """Add all missing columns to existing tables."""
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found: {DB_PATH}")
        print("   Run the Flask app first to create the database")
        return 1
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        total_changes = 0
        
        for table_name, col_name, col_type, default_value in CRITICAL_MIGRATIONS:
            # Check if table exists
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
            if not cursor.fetchone():
                print(f"⚠ Table '{table_name}' does not exist - skipping")
                continue
            
            # Get existing columns
            cursor.execute(f"PRAGMA table_info({table_name})")
            existing_columns = [col[1] for col in cursor.fetchall()]
            
            if col_name not in existing_columns:
                # Add missing column
                print(f"➕ Adding {table_name}.{col_name} ({col_type} DEFAULT {default_value})")
                
                try:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type} DEFAULT {default_value}")
                    total_changes += 1
                    print(f"   ✓ Successfully added")
                except Exception as e:
                    print(f"   ❌ Failed: {e}")
            else:
                print(f"✓ {table_name}.{col_name} already exists")
        
        if total_changes > 0:
            conn.commit()
            print(f"\n✅ Migration completed - {total_changes} columns added")
            print("   Restart your Flask app for changes to take effect")
        else:
            print("\n✅ All columns already exist - no changes needed")
        
        return 0
        
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Migration failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        conn.close()

if __name__ == '__main__':
    sys.exit(migrate())
