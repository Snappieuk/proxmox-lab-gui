#!/usr/bin/env python3
"""
Comprehensive database migration script.
Ensures all tables and columns exist as defined in models.py.
"""

import sqlite3
import sys

DB_PATH = 'app/lab_portal.db'

# Define expected schema from models.py
EXPECTED_SCHEMA = {
    'users': [
        ('id', 'INTEGER', True),
        ('username', 'VARCHAR(80)', False),
        ('password_hash', 'VARCHAR(256)', False),
        ('role', 'VARCHAR(20)', False),
        ('created_at', 'DATETIME', True),
    ],
    'classes': [
        ('id', 'INTEGER', True),
        ('name', 'VARCHAR(120)', False),
        ('description', 'TEXT', True),
        ('teacher_id', 'INTEGER', False),
        ('template_id', 'INTEGER', True),
        ('join_token', 'VARCHAR(64)', True),
        ('token_expires_at', 'DATETIME', True),
        ('token_never_expires', 'BOOLEAN', True),
        ('pool_size', 'INTEGER', True),
        ('cpu_cores', 'INTEGER', True),
        ('memory_mb', 'INTEGER', True),
        ('clone_task_id', 'VARCHAR(64)', True),
        ('created_at', 'DATETIME', True),
        ('updated_at', 'DATETIME', True),
    ],
    'templates': [
        ('id', 'INTEGER', True),
        ('name', 'VARCHAR(120)', False),
        ('proxmox_vmid', 'INTEGER', False),
        ('cluster_ip', 'VARCHAR(45)', True),
        ('node', 'VARCHAR(80)', True),
        ('is_replica', 'BOOLEAN', True),
        ('source_vmid', 'INTEGER', True),
        ('source_node', 'VARCHAR(80)', True),
        ('created_by_id', 'INTEGER', True),
        ('is_class_template', 'BOOLEAN', True),
        ('class_id', 'INTEGER', True),
        ('original_template_id', 'INTEGER', True),
        ('created_at', 'DATETIME', True),
        ('last_verified_at', 'DATETIME', True),
    ],
    'vm_assignments': [
        ('id', 'INTEGER', True),
        ('class_id', 'INTEGER', False),
        ('proxmox_vmid', 'INTEGER', False),
        ('mac_address', 'VARCHAR(17)', True),
        ('cached_ip', 'VARCHAR(45)', True),
        ('ip_updated_at', 'DATETIME', True),
        ('node', 'VARCHAR(80)', True),
        ('assigned_user_id', 'INTEGER', True),
        ('status', 'VARCHAR(20)', True),
        ('is_template_vm', 'BOOLEAN', True),
        ('manually_added', 'BOOLEAN', True),
        ('created_at', 'DATETIME', True),
        ('assigned_at', 'DATETIME', True),
    ],
    'vm_ip_cache': [
        ('vmid', 'INTEGER', True),
        ('mac_address', 'VARCHAR(17)', True),
        ('cached_ip', 'VARCHAR(45)', True),
        ('ip_updated_at', 'DATETIME', True),
        ('cluster_id', 'VARCHAR(50)', True),
    ],
    'vm_inventory': [
        ('id', 'INTEGER', True),
        ('cluster_id', 'VARCHAR(50)', False),
        ('vmid', 'INTEGER', False),
        ('name', 'VARCHAR(255)', False),
        ('node', 'VARCHAR(80)', False),
        ('status', 'VARCHAR(20)', False),
        ('type', 'VARCHAR(10)', False),
        ('category', 'VARCHAR(50)', True),
        ('ip', 'VARCHAR(45)', True),
        ('mac_address', 'VARCHAR(17)', True),
        ('memory', 'BIGINT', True),
        ('cores', 'INTEGER', True),
        ('disk_size', 'BIGINT', True),
        ('uptime', 'INTEGER', True),
        ('cpu_usage', 'FLOAT', True),
        ('memory_usage', 'FLOAT', True),
        ('is_template', 'BOOLEAN', True),
        ('tags', 'TEXT', True),
        ('rdp_available', 'BOOLEAN', True),
        ('ssh_available', 'BOOLEAN', True),
        ('last_updated', 'DATETIME', False),
        ('last_status_check', 'DATETIME', True),
        ('sync_error', 'TEXT', True),
    ],
}

def get_default_value(col_type: str, nullable: bool) -> str:
    """Get appropriate default value for a column type."""
    if nullable:
        return 'NULL'
    
    if 'INTEGER' in col_type or 'BIGINT' in col_type or 'FLOAT' in col_type:
        return '0'
    elif 'BOOLEAN' in col_type:
        return '0'
    elif 'DATETIME' in col_type:
        return "CURRENT_TIMESTAMP"
    else:
        return "''"

def migrate():
    """Add all missing columns to existing tables."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        total_changes = 0
        
        for table_name, expected_columns in EXPECTED_SCHEMA.items():
            # Check if table exists
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
            if not cursor.fetchone():
                print(f"⚠ Table '{table_name}' does not exist - run app to create it")
                continue
            
            print(f"\nChecking table: {table_name}")
            
            # Get existing columns
            cursor.execute(f"PRAGMA table_info({table_name})")
            existing_columns = {col[1]: col for col in cursor.fetchall()}
            
            # Check each expected column
            for col_name, col_type, nullable in expected_columns:
                if col_name not in existing_columns:
                    # Add missing column
                    default = get_default_value(col_type, nullable)
                    null_constraint = "" if nullable else " NOT NULL"
                    
                    print(f"  ➕ Adding column: {col_name} {col_type} DEFAULT {default}")
                    
                    alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}{null_constraint} DEFAULT {default}"
                    cursor.execute(alter_sql)
                    total_changes += 1
                else:
                    print(f"  ✓ Column exists: {col_name}")
        
        if total_changes > 0:
            conn.commit()
            print(f"\n✅ Migration completed successfully - {total_changes} columns added")
        else:
            print("\n✅ No migration needed - all columns exist")
        
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
