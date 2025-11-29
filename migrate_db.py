#!/usr/bin/env python3
"""
Database migration script - adds missing columns to templates table.
Run this to update your database schema without losing data.
"""

import sqlite3
import os
import sys

# Find the database
db_path = os.path.join(os.path.dirname(__file__), 'app', 'lab_portal.db')

if not os.path.exists(db_path):
    # Try other locations
    for path in ['lab_portal.db', 'app/lab_portal.db', '../lab_portal.db']:
        if os.path.exists(path):
            db_path = path
            break

if not os.path.exists(db_path):
    print(f"ERROR: Database not found. Searched for: {db_path}")
    print("Please provide the path to lab_portal.db as an argument:")
    print("  python migrate_db.py /path/to/lab_portal.db")
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        sys.exit(1)

print(f"Migrating database: {db_path}")

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check current schema
    cursor.execute("PRAGMA table_info(templates)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    print(f"Existing columns: {existing_columns}")
    
    # Add missing columns one by one (SQLite doesn't support multiple columns in one ALTER)
    migrations = [
        ("cluster_name", "TEXT"),
        ("is_replica", "INTEGER DEFAULT 0"),
        ("source_vmid", "INTEGER"),
        ("source_node", "TEXT"),
        ("is_class_template", "INTEGER DEFAULT 0"),
        ("class_id", "INTEGER"),
        ("original_template_id", "INTEGER"),
        ("last_verified_at", "DATETIME"),
    ]
    
    for col_name, col_type in migrations:
        if col_name not in existing_columns:
            try:
                sql = f"ALTER TABLE templates ADD COLUMN {col_name} {col_type};"
                print(f"Adding column: {col_name}")
                cursor.execute(sql)
                conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    print(f"WARNING: Failed to add {col_name}: {e}")
        else:
            print(f"Column {col_name} already exists, skipping")
    
    # Verify final schema
    cursor.execute("PRAGMA table_info(templates)")
    final_columns = {row[1] for row in cursor.fetchall()}
    print(f"\nFinal columns: {final_columns}")
    
    conn.close()
    print("\n✓ Migration completed successfully!")
    print("You can now start the application with: python run.py")
    
except Exception as e:
    print(f"\n✗ Migration failed: {e}")
    sys.exit(1)
