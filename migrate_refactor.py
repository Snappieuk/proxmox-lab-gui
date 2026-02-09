#!/usr/bin/env python3
"""
Database migration for code refactoring.

This script applies database schema changes needed for the code simplification refactor:
1. Drop vm_ip_cache table (VMIPCache model removed, replaced by VMInventory)
2. Drop source_vmid and source_node columns from templates table (redundant with original_template_id)

Run this AFTER deploying the code changes.
"""

import os
import sys
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add app directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.models import db


def backup_database():
    """Create a backup of the database before migration."""
    db_path = 'app/lab_portal.db'
    if not os.path.exists(db_path):
        logger.warning(f"Database file not found at {db_path}")
        return None
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f'app/lab_portal.db.backup_{timestamp}'
    
    try:
        import shutil
        shutil.copy2(db_path, backup_path)
        logger.info(f"✓ Database backed up to: {backup_path}")
        return backup_path
    except Exception as e:
        logger.error(f"Failed to backup database: {e}")
        return None


def check_table_exists(table_name):
    """Check if a table exists in the database."""
    try:
        result = db.session.execute(
            db.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:table_name"),
            {'table_name': table_name}
        )
        return result.fetchone() is not None
    except Exception as e:
        logger.error(f"Error checking table existence: {e}")
        return False


def check_column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    try:
        result = db.session.execute(db.text(f"PRAGMA table_info({table_name})"))
        columns = [row[1] for row in result.fetchall()]
        return column_name in columns
    except Exception as e:
        logger.error(f"Error checking column existence: {e}")
        return False


def migrate_vmipcache_data():
    """
    Copy any remaining VMIPCache data to VMInventory before dropping table.
    
    Note: This is mostly for safety - VMInventory should already have all VM data
    from background sync. VMIPCache was only used for legacy VMs.
    """
    if not check_table_exists('vm_ip_cache'):
        logger.info("vm_ip_cache table doesn't exist, skipping data migration")
        return True
    
    try:
        # Count records in VMIPCache
        result = db.session.execute(db.text("SELECT COUNT(*) FROM vm_ip_cache"))
        count = result.scalar()
        
        if count == 0:
            logger.info("vm_ip_cache table is empty, no data to migrate")
            return True
        
        logger.info(f"Found {count} records in vm_ip_cache table")
        
        # Get all VMIPCache entries with valid IPs
        result = db.session.execute(
            db.text("""
                SELECT vmid, cluster_id, cached_ip, mac_address, ip_updated_at
                FROM vm_ip_cache
                WHERE cached_ip IS NOT NULL
            """)
        )
        
        migrated = 0
        skipped = 0
        
        for row in result.fetchall():
            vmid, cluster_id, cached_ip, mac_address, ip_updated_at = row
            
            # Check if VM exists in VMInventory
            vm_check = db.session.execute(
                db.text("""
                    SELECT id FROM vm_inventory
                    WHERE vmid = :vmid AND cluster_id = :cluster_id
                """),
                {'vmid': vmid, 'cluster_id': cluster_id}
            )
            
            if vm_check.fetchone():
                # Update existing VMInventory record with cached IP if it doesn't have one
                db.session.execute(
                    db.text("""
                        UPDATE vm_inventory
                        SET ip = COALESCE(ip, :cached_ip),
                            mac_address = COALESCE(mac_address, :mac_address),
                            last_updated = COALESCE(last_updated, :last_updated)
                        WHERE vmid = :vmid AND cluster_id = :cluster_id
                          AND (ip IS NULL OR ip = '')
                    """),
                    {
                        'vmid': vmid,
                        'cluster_id': cluster_id,
                        'cached_ip': cached_ip,
                        'mac_address': mac_address,
                        'last_updated': ip_updated_at
                    }
                )
                migrated += 1
            else:
                # VM not in VMInventory - it's a legacy VM that's no longer in Proxmox
                # Background sync will add it if it's still there, so we can skip
                skipped += 1
                logger.debug(f"Skipped VM {vmid} - not in VMInventory (legacy/deleted VM)")
        
        db.session.commit()
        logger.info(f"✓ Migrated IP cache data: {migrated} updated, {skipped} skipped")
        return True
        
    except Exception as e:
        logger.error(f"Failed to migrate VMIPCache data: {e}", exc_info=True)
        db.session.rollback()
        return False


def drop_vm_ip_cache_table():
    """Drop the vm_ip_cache table (VMIPCache model removed)."""
    if not check_table_exists('vm_ip_cache'):
        logger.info("vm_ip_cache table doesn't exist, skipping drop")
        return True
    
    try:
        logger.info("Dropping vm_ip_cache table...")
        db.session.execute(db.text("DROP TABLE vm_ip_cache"))
        db.session.commit()
        logger.info("✓ Dropped vm_ip_cache table")
        return True
    except Exception as e:
        logger.error(f"Failed to drop vm_ip_cache table: {e}")
        db.session.rollback()
        return False


def drop_template_source_columns():
    """
    Drop source_vmid and source_node columns from templates table.
    
    These are redundant with original_template_id foreign key relationship.
    """
    if not check_table_exists('templates'):
        logger.info("templates table doesn't exist, skipping column drop")
        return True
    
    # Check if columns exist
    has_source_vmid = check_column_exists('templates', 'source_vmid')
    has_source_node = check_column_exists('templates', 'source_node')
    
    if not has_source_vmid and not has_source_node:
        logger.info("source_vmid and source_node columns don't exist, skipping drop")
        return True
    
    try:
        logger.info("Dropping source_vmid and source_node columns from templates table...")
        
        # SQLite doesn't support DROP COLUMN directly, need to recreate table
        # Get current table schema
        result = db.session.execute(db.text("PRAGMA table_info(templates)"))
        columns = result.fetchall()
        
        # Build new column list (exclude source_vmid and source_node)
        new_columns = []
        for col in columns:
            col_name = col[1]
            if col_name not in ('source_vmid', 'source_node'):
                new_columns.append(col_name)
        
        # Create new table without source columns
        db.session.execute(db.text("""
            CREATE TABLE templates_new AS
            SELECT {} FROM templates
        """.format(', '.join(new_columns))))
        
        # Drop old table and rename new one
        db.session.execute(db.text("DROP TABLE templates"))
        db.session.execute(db.text("ALTER TABLE templates_new RENAME TO templates"))
        
        # Recreate indexes (SQLite may need these recreated)
        db.session.execute(db.text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uix_cluster_node_vmid 
            ON templates (cluster_ip, node, proxmox_vmid)
        """))
        
        db.session.commit()
        logger.info("✓ Dropped source_vmid and source_node columns from templates")
        return True
        
    except Exception as e:
        logger.error(f"Failed to drop template source columns: {e}", exc_info=True)
        db.session.rollback()
        return False


def rebuild_templates_table_with_constraints():
    """
    Rebuild templates table to restore primary key and constraints.

    The refactor migration used CREATE TABLE AS SELECT, which drops the PK.
    This rebuilds the table with the proper schema and de-duplicates rows by ID.
    """
    if not check_table_exists('templates'):
        logger.info("templates table doesn't exist, skipping rebuild")
        return True

    try:
        result = db.session.execute(db.text("PRAGMA table_info(templates)"))
        columns = result.fetchall()
        id_pk = any(col[1] == 'id' and col[5] == 1 for col in columns)
        if id_pk:
            logger.info("templates table already has a primary key, skipping rebuild")
            return True
    except Exception as e:
        logger.error(f"Failed to inspect templates schema: {e}")
        return False

    try:
        dup_count = db.session.execute(db.text(
            "SELECT COUNT(*) FROM (SELECT id FROM templates GROUP BY id HAVING COUNT(*) > 1)"
        )).scalar() or 0
        if dup_count:
            logger.warning("Found %d duplicate template IDs; de-duplicating by latest rowid", dup_count)

        logger.info("Rebuilding templates table with primary key and indexes...")

        db.session.execute(db.text("""
            CREATE TABLE templates_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(120) NOT NULL,
                proxmox_vmid INTEGER NOT NULL,
                cluster_ip VARCHAR(45) DEFAULT '10.220.15.249',
                node VARCHAR(80),
                is_replica BOOLEAN DEFAULT 0,
                created_by_id INTEGER,
                is_class_template BOOLEAN DEFAULT 0,
                class_id INTEGER,
                original_template_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_verified_at TIMESTAMP,
                cpu_cores INTEGER,
                cpu_sockets INTEGER,
                memory_mb INTEGER,
                disk_size_gb REAL,
                disk_storage VARCHAR(80),
                disk_path VARCHAR(255),
                disk_format VARCHAR(20),
                network_bridge VARCHAR(80),
                os_type VARCHAR(20),
                specs_cached_at TIMESTAMP
            )
        """))

        db.session.execute(db.text("""
            INSERT INTO templates_new (
                id, name, proxmox_vmid, cluster_ip, node, is_replica, created_by_id,
                is_class_template, class_id, original_template_id, created_at, last_verified_at,
                cpu_cores, cpu_sockets, memory_mb, disk_size_gb, disk_storage, disk_path,
                disk_format, network_bridge, os_type, specs_cached_at
            )
            SELECT
                t.id, t.name, t.proxmox_vmid, t.cluster_ip, t.node, t.is_replica, t.created_by_id,
                t.is_class_template, t.class_id, t.original_template_id, t.created_at, t.last_verified_at,
                t.cpu_cores, t.cpu_sockets, t.memory_mb, t.disk_size_gb, t.disk_storage, t.disk_path,
                t.disk_format, t.network_bridge, t.os_type, t.specs_cached_at
            FROM templates t
            JOIN (
                SELECT id, MAX(rowid) AS max_rowid
                FROM templates
                GROUP BY id
            ) d ON t.id = d.id AND t.rowid = d.max_rowid
        """))

        db.session.execute(db.text("DROP TABLE templates"))
        db.session.execute(db.text("ALTER TABLE templates_new RENAME TO templates"))

        db.session.execute(db.text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uix_cluster_node_vmid
            ON templates (cluster_ip, node, proxmox_vmid)
        """))
        db.session.execute(db.text("CREATE INDEX IF NOT EXISTS ix_templates_proxmox_vmid ON templates (proxmox_vmid)"))
        db.session.execute(db.text("CREATE INDEX IF NOT EXISTS ix_templates_cluster_ip ON templates (cluster_ip)"))
        db.session.execute(db.text("CREATE INDEX IF NOT EXISTS ix_templates_node ON templates (node)"))
        db.session.execute(db.text("CREATE INDEX IF NOT EXISTS ix_templates_is_replica ON templates (is_replica)"))

        db.session.commit()
        logger.info("✓ Rebuilt templates table with primary key and indexes")
        return True
    except Exception as e:
        logger.error(f"Failed to rebuild templates table: {e}", exc_info=True)
        db.session.rollback()
        return False


def verify_migrations():
    """Verify that migrations were applied successfully."""
    logger.info("\nVerifying migrations...")
    
    success = True
    
    # Check that vm_ip_cache table is gone
    if check_table_exists('vm_ip_cache'):
        logger.error("✗ vm_ip_cache table still exists")
        success = False
    else:
        logger.info("✓ vm_ip_cache table removed")
    
    # Check that source columns are gone from templates
    if check_table_exists('templates'):
        has_source_vmid = check_column_exists('templates', 'source_vmid')
        has_source_node = check_column_exists('templates', 'source_node')
        
        if has_source_vmid or has_source_node:
            logger.error("✗ source_vmid/source_node columns still exist in templates")
            success = False
        else:
            logger.info("✓ source_vmid and source_node columns removed from templates")
    
    return success


def main():
    """Main migration function."""
    logger.info("=" * 60)
    logger.info("Code Refactoring Database Migration")
    logger.info("=" * 60)
    
    # Create Flask app context
    app = create_app()
    
    with app.app_context():
        # Step 1: Backup database
        logger.info("\n1. Backing up database...")
        backup_path = backup_database()
        if not backup_path:
            logger.error("Failed to backup database. Aborting migration.")
            return False
        
        # Step 2: Migrate VMIPCache data to VMInventory
        logger.info("\n2. Migrating VMIPCache data to VMInventory...")
        if not migrate_vmipcache_data():
            logger.error("Failed to migrate VMIPCache data. Aborting migration.")
            logger.info(f"Database backup available at: {backup_path}")
            return False
        
        # Step 3: Drop vm_ip_cache table
        logger.info("\n3. Dropping vm_ip_cache table...")
        if not drop_vm_ip_cache_table():
            logger.error("Failed to drop vm_ip_cache table. Aborting migration.")
            logger.info(f"Database backup available at: {backup_path}")
            return False
        
        # Step 4: Drop template source columns
        logger.info("\n4. Dropping template source columns...")
        if not drop_template_source_columns():
            logger.error("Failed to drop template source columns. Aborting migration.")
            logger.info(f"Database backup available at: {backup_path}")
            return False
        
        # Step 5: Rebuild templates table with constraints (fixes lost PK)
        logger.info("\n5. Rebuilding templates table with constraints...")
        if not rebuild_templates_table_with_constraints():
            logger.error("Failed to rebuild templates table. Aborting migration.")
            logger.info(f"Database backup available at: {backup_path}")
            return False

        # Step 6: Verify migrations
        if not verify_migrations():
            logger.error("\n✗ Migration verification failed!")
            logger.info(f"Database backup available at: {backup_path}")
            return False
        
        logger.info("\n" + "=" * 60)
        logger.info("✓ All migrations completed successfully!")
        logger.info("=" * 60)
        logger.info(f"Database backup: {backup_path}")
        logger.info("\nYou can now restart the Flask application.")
        return True


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
