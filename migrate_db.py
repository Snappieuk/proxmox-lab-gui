#!/usr/bin/env python3
"""Database migration script to add cpu_cores and memory_mb columns to Class table."""

import sqlite3
import sys

DB_PATH = 'app/lab_portal.db'

def migrate():
    """Add cpu_cores and memory_mb columns if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Check if columns already exist
        cursor.execute("PRAGMA table_info(classes)")
        columns = [col[1] for col in cursor.fetchall()]
        
        needs_migration = False
        
        if 'cpu_cores' not in columns:
            print("Adding cpu_cores column...")
            cursor.execute("ALTER TABLE classes ADD COLUMN cpu_cores INTEGER DEFAULT 2")
            needs_migration = True
        else:
            print("cpu_cores column already exists")
        
        if 'memory_mb' not in columns:
            print("Adding memory_mb column...")
            cursor.execute("ALTER TABLE classes ADD COLUMN memory_mb INTEGER DEFAULT 2048")
            needs_migration = True
        else:
            print("memory_mb column already exists")
        
        if needs_migration:
            conn.commit()
            print("✓ Migration completed successfully")
        else:
            print("✓ No migration needed - all columns exist")
        
    except Exception as e:
        conn.rollback()
        print(f"✗ Migration failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

if __name__ == '__main__':
    migrate()
