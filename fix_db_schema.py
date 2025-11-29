#!/usr/bin/env python3
"""
Fix database schema by dropping and recreating vm_inventory table.
Run this once to update the database schema after adding cluster_id column.
"""

import sqlite3
import os

# Path to your database
db_path = os.path.join(os.path.dirname(__file__), 'app', 'lab_portal.db')

if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
    print("It will be created automatically when you run the app.")
    exit(0)

print(f"Fixing database schema at {db_path}")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Drop the old vm_inventory table if it exists
cursor.execute("DROP TABLE IF EXISTS vm_inventory")
print("Dropped old vm_inventory table")

# Create the new table with all required columns
cursor.execute("""
CREATE TABLE vm_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id VARCHAR(50) NOT NULL,
    cluster_name VARCHAR(120),
    vmid INTEGER NOT NULL,
    name VARCHAR(200),
    node VARCHAR(120),
    status VARCHAR(40),
    ip VARCHAR(45),
    type VARCHAR(20),
    category VARCHAR(40),
    last_updated DATETIME,
    CONSTRAINT uix_cluster_vmid UNIQUE (cluster_id, vmid)
)
""")
print("Created new vm_inventory table with cluster_id column")

# Create indexes
cursor.execute("CREATE INDEX ix_vm_inventory_cluster_id ON vm_inventory (cluster_id)")
cursor.execute("CREATE INDEX ix_vm_inventory_vmid ON vm_inventory (vmid)")
cursor.execute("CREATE INDEX ix_vm_inventory_name ON vm_inventory (name)")
cursor.execute("CREATE INDEX ix_vm_inventory_node ON vm_inventory (node)")
cursor.execute("CREATE INDEX ix_vm_inventory_status ON vm_inventory (status)")
cursor.execute("CREATE INDEX ix_vm_inventory_ip ON vm_inventory (ip)")
cursor.execute("CREATE INDEX ix_vm_inventory_last_updated ON vm_inventory (last_updated)")
print("Created indexes")

conn.commit()
conn.close()

print("\nDatabase schema fixed successfully!")
print("You can now restart the application.")
