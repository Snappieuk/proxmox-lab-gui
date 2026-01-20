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
    ('classes', 'auto_shutdown_enabled', 'BOOLEAN', '0'),  # Enable auto-shutdown for idle VMs
    ('classes', 'auto_shutdown_cpu_threshold', 'INTEGER', '20'),  # CPU % threshold for idle detection
    ('classes', 'auto_shutdown_idle_minutes', 'INTEGER', '30'),  # Minutes of idle time before shutdown
    ('classes', 'restrict_hours', 'BOOLEAN', '0'),  # Enable hour restrictions
    ('classes', 'hours_start', 'INTEGER', '0'),  # Start hour (0-23)
    ('classes', 'hours_end', 'INTEGER', '23'),  # End hour (0-23)
    ('classes', 'max_usage_hours', 'INTEGER', '0'),  # Max cumulative hours students can use VM (0=unlimited)
    ('vm_assignments', 'manually_added', 'BOOLEAN', '0'),
    ('vm_assignments', 'is_teacher_vm', 'BOOLEAN', '0'),
    ('vm_assignments', 'vm_name', 'TEXT', 'NULL'),
    ('vm_assignments', 'usage_hours', 'REAL', '0.0'),  # Cumulative hours VM has been used
    ('vm_assignments', 'usage_last_reset', 'TIMESTAMP', 'NULL'),  # When usage was last reset by teacher
    ('vm_assignments', 'hostname_configured', 'BOOLEAN', '0'),  # True if hostname has been set (prevents rename loops)
    ('vm_assignments', 'target_hostname', 'VARCHAR(63)', 'NULL'),  # Intended hostname for this VM (for auto-rename)
    # Make class_id nullable for direct assignments (builder VMs)
    ('vm_assignments', 'class_id_nullable', 'INTEGER', 'NULL'),  # Placeholder for nullable class_id migration
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
    # Cluster management columns
    ('clusters', 'allow_vm_deployment', 'BOOLEAN', '1'),
    ('clusters', 'allow_template_sync', 'BOOLEAN', '1'),
    ('clusters', 'allow_iso_sync', 'BOOLEAN', '1'),
    ('clusters', 'auto_shutdown_enabled', 'BOOLEAN', '0'),
    ('clusters', 'priority', 'INTEGER', '50'),
    ('clusters', 'default_storage', 'VARCHAR(100)', 'NULL'),
    ('clusters', 'template_storage', 'VARCHAR(100)', 'NULL'),
    ('clusters', 'iso_storage', 'VARCHAR(100)', 'NULL'),
    ('clusters', 'qcow2_template_path', 'VARCHAR(512)', 'NULL'),
    ('clusters', 'qcow2_images_path', 'VARCHAR(512)', 'NULL'),
    ('clusters', 'admin_group', 'VARCHAR(100)', 'NULL'),
    ('clusters', 'admin_users', 'TEXT', 'NULL'),
    ('clusters', 'arp_subnets', 'TEXT', 'NULL'),
    ('clusters', 'vm_cache_ttl', 'INTEGER', 'NULL'),
    ('clusters', 'enable_ip_lookup', 'BOOLEAN', '1'),
    ('clusters', 'enable_ip_persistence', 'BOOLEAN', '0'),
    ('clusters', 'description', 'TEXT', 'NULL'),
]

# System settings table schema
SYSTEM_SETTINGS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS system_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key VARCHAR(100) UNIQUE NOT NULL,
    value TEXT,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

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
    allow_vm_deployment BOOLEAN DEFAULT 1,
    allow_template_sync BOOLEAN DEFAULT 1,
    allow_iso_sync BOOLEAN DEFAULT 1,
    auto_shutdown_enabled BOOLEAN DEFAULT 0,
    priority INTEGER DEFAULT 50,
    default_storage VARCHAR(100),
    template_storage VARCHAR(100),
    qcow2_template_path VARCHAR(512),
    qcow2_images_path VARCHAR(512),
    admin_group VARCHAR(100),
    admin_users TEXT,
    arp_subnets TEXT,
    vm_cache_ttl INTEGER,
    enable_ip_lookup BOOLEAN DEFAULT 1,
    enable_ip_persistence BOOLEAN DEFAULT 0,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# ISO images cache table schema
ISO_IMAGES_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS iso_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    volid VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    size BIGINT NOT NULL,
    node VARCHAR(50) NOT NULL,
    storage VARCHAR(50) NOT NULL,
    cluster_id VARCHAR(50) NOT NULL,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

ISO_IMAGES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_iso_volid ON iso_images(volid)",
    "CREATE INDEX IF NOT EXISTS idx_iso_name ON iso_images(name)",
    "CREATE INDEX IF NOT EXISTS idx_iso_node ON iso_images(node)",
    "CREATE INDEX IF NOT EXISTS idx_iso_storage ON iso_images(storage)",
    "CREATE INDEX IF NOT EXISTS idx_iso_cluster ON iso_images(cluster_id)",
]

def create_system_settings_table(cursor):
    """Create system_settings table if it doesn't exist."""
    print("\n📋 Checking system_settings table...")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='system_settings'")
    if not cursor.fetchone():
        print("➕ Creating system_settings table")
        cursor.execute(SYSTEM_SETTINGS_TABLE_SCHEMA)
        print("   ✓ System settings table created")
        return True
    else:
        print("✓ System settings table already exists")
        return False


def insert_default_system_settings(cursor):
    """Insert default system settings if they don't exist."""
    print("\n📋 Checking default system settings...")
    
    default_settings = [
        ('enable_template_replication', 'false', 'Enable automatic template replication across all nodes at startup'),
        ('template_sync_interval', '1800', 'Template sync interval in seconds (default: 30 minutes)'),
        ('vm_sync_interval', '300', 'VM inventory sync interval in seconds (default: 5 minutes)'),
        ('vm_sync_quick_interval', '30', 'Quick VM status sync interval in seconds (default: 30 seconds)'),
        ('ip_discovery_delay', '15', 'Delay in seconds after VM start before IP discovery (default: 15 seconds)'),
    ]
    
    inserted_count = 0
    for key, value, description in default_settings:
        cursor.execute("SELECT id FROM system_settings WHERE key = ?", (key,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO system_settings (key, value, description, updated_at) VALUES (?, ?, ?, ?)",
                (key, value, description, datetime.utcnow())
            )
            print(f"   ✓ Inserted default setting: {key} = {value}")
            inserted_count += 1
        else:
            print(f"   ⏭️  Setting already exists: {key}")
    
    if inserted_count > 0:
        print(f"   ✓ Inserted {inserted_count} default settings")
    return inserted_count


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


def create_iso_images_table(cursor):
    """Create iso_images table if it doesn't exist."""
    print("\n📋 Checking iso_images table...")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='iso_images'")
    if not cursor.fetchone():
        print("➕ Creating iso_images table")
        cursor.execute(ISO_IMAGES_TABLE_SCHEMA)
        print("   ✓ ISO images table created")
        
        # Create indexes
        print("➕ Creating indexes for iso_images table")
        for index_sql in ISO_IMAGES_INDEXES:
            cursor.execute(index_sql)
        print("   ✓ Indexes created")
        return True
    else:
        print("✓ ISO images table already exists")
        return False


def make_class_id_nullable(cursor):
    """Make VMAssignment.class_id nullable for builder VMs.
    
    SQLite doesn't support ALTER COLUMN, so we need to:
    1. Create new table with nullable class_id
    2. Copy data
    3. Drop old table
    4. Rename new table
    """
    print("\n📋 Checking VMAssignment.class_id nullable constraint...")
    
    # Check if class_id is already nullable
    cursor.execute("PRAGMA table_info(vm_assignments)")
    columns = cursor.fetchall()
    
    for col in columns:
        # col format: (cid, name, type, notnull, dflt_value, pk)
        if col[1] == 'class_id':
            if col[3] == 0:  # notnull=0 means nullable
                print("✓ VMAssignment.class_id is already nullable")
                return False
            else:
                print("➕ Making VMAssignment.class_id nullable for builder VMs...")
                break
    else:
        print("⚠ class_id column not found in vm_assignments table")
        return False
    
    try:
        # Get all column definitions
        cursor.execute("PRAGMA table_info(vm_assignments)")
        columns = cursor.fetchall()
        
        # Build new table schema with nullable class_id
        col_defs = []
        for col in columns:
            col_name = col[1]
            col_type = col[2]
            is_pk = col[5] == 1
            
            if col_name == 'class_id':
                # Make it nullable and add foreign key
                col_defs.append(f"{col_name} {col_type}")
            elif is_pk:
                col_defs.append(f"{col_name} {col_type} PRIMARY KEY AUTOINCREMENT")
            else:
                not_null = col[3] == 1
                default = col[4]
                
                col_def = f"{col_name} {col_type}"
                if not_null:
                    col_def += " NOT NULL"
                if default:
                    col_def += f" DEFAULT {default}"
                col_defs.append(col_def)
        
        # Add foreign key constraints
        col_defs.append("FOREIGN KEY (class_id) REFERENCES classes(id)")
        col_defs.append("FOREIGN KEY (assigned_user_id) REFERENCES users(id)")
        
        # Create new table
        new_table_sql = f"CREATE TABLE vm_assignments_new ({', '.join(col_defs)})"
        cursor.execute(new_table_sql)
        
        # Copy data
        cursor.execute("INSERT INTO vm_assignments_new SELECT * FROM vm_assignments")
        
        # Drop old table and rename
        cursor.execute("DROP TABLE vm_assignments")
        cursor.execute("ALTER TABLE vm_assignments_new RENAME TO vm_assignments")
        
        # Recreate indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vm_assignments_class ON vm_assignments(class_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vm_assignments_vmid ON vm_assignments(proxmox_vmid)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vm_assignments_user ON vm_assignments(assigned_user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vm_assignments_vm_name ON vm_assignments(vm_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vm_assignments_mac ON vm_assignments(mac_address)")
        
        print("   ✅ Successfully made class_id nullable")
        return True
        
    except Exception as e:
        print(f"   ❌ Failed to make class_id nullable: {e}")
        raise


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
    
    # Create database if it doesn't exist
    if not os.path.exists(DB_PATH):
        print(f"📦 Database not found - creating new database: {DB_PATH}")
        # Ensure directory exists
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        
        # Create database (will be initialized by Flask models on first run)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        print(f"   ✓ Created empty database at {DB_PATH}")
        print(f"   ℹ️  Tables will be created by Flask on first startup")
    else:
        print(f"✓ Database found: {DB_PATH}")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
    
    try:
        total_changes = 0
        
        # Step 1: Create system tables if needed
        if create_system_settings_table(cursor):
            total_changes += 1
        
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
        
        # Check if classes table exists first
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='classes'")
        if not cursor.fetchone():
            print("   ⏭️  Classes table doesn't exist yet - skipping join token migration")
        else:
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
        
        # Step 3: Make VMAssignment.class_id nullable (for builder VMs)
        class_id_updated = make_class_id_nullable(cursor)
        if class_id_updated:
            total_changes += 1
        
        # Step 4: Insert default system settings
        default_settings_inserted = insert_default_system_settings(cursor)
        if default_settings_inserted > 0:
            total_changes += default_settings_inserted
        
        # Step 5: Migrate clusters from JSON to database
        clusters_migrated = migrate_clusters_from_json(cursor)
        if clusters_migrated > 0:
            total_changes += clusters_migrated
        
        # Step 6: Create ISO images cache table
        iso_table_created = create_iso_images_table(cursor)
        if iso_table_created:
            total_changes += 1
        
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
