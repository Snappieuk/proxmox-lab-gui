#!/bin/bash
# Fix vm_inventory table schema for Debian/Linux

DB_PATH="app/lab_portal.db"

if [ -f "$DB_PATH" ]; then
    echo "Database found at $DB_PATH"
    echo "Fixing vm_inventory table..."
    
    python3 << 'EOF'
import sqlite3
conn = sqlite3.connect('app/lab_portal.db')
cursor = conn.cursor()

# Check if column exists
cursor.execute('PRAGMA table_info(vm_inventory)')
columns = [row[1] for row in cursor.fetchall()]

if 'cluster_id' not in columns:
    print('Adding cluster_id column...')
    cursor.execute("ALTER TABLE vm_inventory ADD COLUMN cluster_id VARCHAR(50) NOT NULL DEFAULT 'cluster1'")
    cursor.execute('CREATE INDEX IF NOT EXISTS ix_vm_inventory_cluster_id ON vm_inventory (cluster_id)')
    conn.commit()
    print('Done! cluster_id column added.')
else:
    print('cluster_id column already exists.')

conn.close()
EOF
    
    if [ $? -eq 0 ]; then
        echo -e "\033[0;32mDatabase fixed successfully!\033[0m"
    else
        echo -e "\033[0;31mError fixing database\033[0m"
    fi
else
    echo -e "\033[0;33mDatabase not found at $DB_PATH - it will be created on first run\033[0m"
fi
