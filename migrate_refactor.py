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
        
        # Step 5: Verify migrations
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
