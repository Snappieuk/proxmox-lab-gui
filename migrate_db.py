#!/usr/bin/env python3
"""
Comprehensive database migration script.
Ensures all tables and columns exist as defined in models.py.
Migrates cluster configuration from clusters.json to database.
"""

import os
import sqlite3
import sys
import json
from datetime import datetime

DB_PATH = 'app/lab_portal.db'
CLUSTER_CONFIG_FILE = 'app/clusters.json'

# Critical columns that must exist
CRITICAL_MIGRATIONS = [
    ('classes', 'cpu_cores', 'INTEGER', '2'),
    ('classes', 'memory_mb', 'INTEGER', '2048'),
    ('classes', 'disk_size_gb', 'INTEGER', '32'),
    ('classes', 'vmid_prefix', 'INTEGER', 'NULL'),  # 3-digit prefix for VMID allocation (200-999)
    ('classes', 'deployment_node', 'TEXT', 'NULL'),  # Optional: deploy all VMs to specific node
    ('classes', 'deployment_cluster', 'TEXT', 'NULL'),  # Optional: target cluster for deployment
    ('vm_assignments', 'manually_added', 'BOOLEAN', '0'),
    ('vm_assignments', 'is_teacher_vm', 'BOOLEAN', '0'),
    ('vm_assignments', 'vm_name', 'TEXT', 'NULL'),
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

# Cluster table schema
CLUSTER_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(120) NOT NULL,
    host VARCHAR(255) NOT NULL,
    port INTEGER DEFAULT 8006,
    user VARCHAR(80) NOT NULL,
    password VARCHAR(256) NOT NULL,
    verify_ssl BOOLEAN DEFAULT 0,
    is_default BOOLEAN DEFAULT 0,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

def create_clusters_table(cursor):
    """Create clusters table if it doesn't exist."""
    print("\n📋 Checking clusters table...")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clusters'")
    if not cursor.fetchone():
        print("➕ Creating clusters table")
        cursor.execute(CLUSTER_TABLE_SCHEMA)
        print("   ✓ Clusters table created")
        return True
    else:
        print("✓ Clusters table already exists")
        return False


def migrate_clusters_from_json(cursor):
    """Migrate cluster configuration from clusters.json to database."""
    if not os.path.exists(CLUSTER_CONFIG_FILE):
        print("\n⚠ No clusters.json found - skipping cluster migration")
        print("   You can add clusters via the admin UI or manually in the database")
        return 0
    
    try:
        print(f"\n📥 Loading cluster config from {CLUSTER_CONFIG_FILE}")
        with open(CLUSTER_CONFIG_FILE, 'r', encoding='utf-8') as f:
            clusters = json.load(f)
        
        if not clusters or not isinstance(clusters, list):
            print("⚠ No clusters found in JSON file")
            return 0
        
        print(f"   Found {len(clusters)} cluster(s) in JSON")
        
        migrated_count = 0
        for cluster in clusters:
            cluster_id = cluster.get('id')
            if not cluster_id:
                print(f"   ⚠ Skipping cluster with no ID: {cluster.get('name', 'unknown')}")
                continue
            
            # Check if cluster already exists in database
            cursor.execute("SELECT id FROM clusters WHERE cluster_id = ?", (cluster_id,))
            if cursor.fetchone():
                print(f"   ✓ Cluster '{cluster_id}' already exists in database - skipping")
                continue
            
            # Insert cluster into database
            cursor.execute("""
                INSERT INTO clusters (cluster_id, name, host, port, user, password, verify_ssl, is_default, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cluster_id,
                cluster.get('name', f'Cluster {cluster_id}'),
                cluster.get('host', ''),
                cluster.get('port', 8006),
                cluster.get('user', 'root@pam'),
                cluster.get('password', ''),
                1 if cluster.get('verify_ssl', False) else 0,
                1 if cluster.get('is_default', False) else 0,
                1 if cluster.get('is_active', True) else 0,
            ))
            migrated_count += 1
            print(f"   ✅ Migrated cluster '{cluster_id}' ({cluster.get('name')})")
        
        if migrated_count > 0:
            print(f"\n✅ Migrated {migrated_count} cluster(s) from JSON to database")
            print(f"   You can now manage clusters via admin UI")
            print(f"   The clusters.json file can be backed up and removed")
        else:
            print("\n✓ All clusters already in database")
        
        return migrated_count
        
    except json.JSONDecodeError as e:
        print(f"\n❌ Failed to parse clusters.json: {e}")
        return 0
    except Exception as e:
        print(f"\n❌ Failed to migrate clusters: {e}")
        import traceback
        traceback.print_exc()
        return 0


def migrate():
    """Add all missing columns to existing tables and migrate clusters."""
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found: {DB_PATH}")
        print("   Run the Flask app first to create the database")
        return 1
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        total_changes = 0
        
        # Step 1: Create clusters table if needed
        if create_clusters_table(cursor):
            total_changes += 1
        
        # Step 2: Migrate column additions
        print("\n📋 Checking column migrations...")
        for table_name, col_name, col_type, default_value in CRITICAL_MIGRATIONS:
            # Check if table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
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
                    print("   ✓ Successfully added")
                except Exception as e:
                    print(f"   ❌ Failed: {e}")
            else:
                print(f"✓ {table_name}.{col_name} already exists")
        
        # Step 2.5: Regenerate join tokens to use new simple format
        print("\n🔄 Checking join token format...")
        cursor.execute("SELECT id, name, vmid_prefix, join_token FROM classes WHERE join_token IS NOT NULL")
        classes_with_tokens = cursor.fetchall()
        
        regenerated = 0
        for class_id, class_name, vmid_prefix, current_token in classes_with_tokens:
            # Check if token is old format (long random string)
            if len(current_token) > 30 or '-' not in current_token:
                import re
                # Generate new simple token
                sanitized_name = re.sub(r'[^a-z0-9]+', '-', class_name.lower()).strip('-')
                identifier = str(vmid_prefix) if vmid_prefix else str(class_id)
                new_token = f"{sanitized_name}-{identifier}"
                
                print(f"   🔄 Updating class '{class_name}': {current_token[:20]}... → {new_token}")
                
                try:
                    cursor.execute("UPDATE classes SET join_token = ? WHERE id = ?", (new_token, class_id))
                    regenerated += 1
                except Exception as e:
                    print(f"   ⚠ Failed to update class {class_id}: {e}")
        
        if regenerated > 0:
            print(f"   ✅ Regenerated {regenerated} join token(s) to simple format")
            total_changes += regenerated
        else:
            print("   ✓ All join tokens already use simple format")
        
        # Step 3: Migrate clusters from JSON to database
        clusters_migrated = migrate_clusters_from_json(cursor)
        if clusters_migrated > 0:
            total_changes += clusters_migrated
        
        # Commit all changes
        if total_changes > 0:
            conn.commit()
            print(f"\n✅ Migration completed - {total_changes} change(s) applied")
            print("   Restart your Flask app for changes to take effect")
        else:
            print("\n✅ Database is up to date - no changes needed")
        
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
