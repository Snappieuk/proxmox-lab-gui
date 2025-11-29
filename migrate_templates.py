#!/usr/bin/env python3
"""
Database Migration Script - Add Template Tracking Columns

This script adds the new columns to the Template table if they don't exist:
- is_replica (Boolean)
- source_vmid (Integer)
- source_node (String)
- last_verified_at (DateTime)

It also scans Proxmox and populates the database with all existing templates.
"""

import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.models import db, Template
from datetime import datetime

def migrate_database():
    """Add new columns to Template table and populate with existing templates."""
    app = create_app()
    
    with app.app_context():
        print("=" * 60)
        print("Template Database Migration")
        print("=" * 60)
        
        # Create tables if they don't exist
        print("\n1. Creating/updating database schema...")
        try:
            db.create_all()
            print("✓ Schema updated successfully")
        except Exception as e:
            print(f"✗ Schema update failed: {e}")
            return
        
        # Scan Proxmox and populate templates
        print("\n2. Scanning Proxmox for templates...")
        try:
            from app.services.proxmox_operations import replicate_templates_to_all_nodes
            from app.config import CLUSTERS
            
            for cluster in CLUSTERS:
                cluster_ip = cluster["host"]
                cluster_name = cluster["name"]
                print(f"\n   Scanning cluster: {cluster_name} ({cluster_ip})")
                
                # This function now registers templates in the database
                replicate_templates_to_all_nodes(cluster_ip=cluster_ip)
                
            print("\n✓ Template scan complete")
            
        except Exception as e:
            print(f"✗ Template scan failed: {e}")
            import traceback
            traceback.print_exc()
            return
        
        # Display results
        print("\n3. Database statistics:")
        try:
            total = Template.query.count()
            originals = Template.query.filter_by(is_replica=False, is_class_template=False).count()
            replicas = Template.query.filter_by(is_replica=True).count()
            class_templates = Template.query.filter_by(is_class_template=True).count()
            
            print(f"   Total templates:      {total}")
            print(f"   Original templates:   {originals}")
            print(f"   Replica templates:    {replicas}")
            print(f"   Class templates:      {class_templates}")
            
            # Show templates by cluster and node
            print("\n4. Templates by location:")
            from app.config import CLUSTERS
            for cluster in CLUSTERS:
                cluster_ip = cluster["host"]
                cluster_name = cluster["name"]
                cluster_templates = Template.query.filter_by(cluster_ip=cluster_ip).all()
                
                if cluster_templates:
                    print(f"\n   {cluster_name} ({cluster_ip}):")
                    
                    # Group by node
                    by_node = {}
                    for tpl in cluster_templates:
                        if tpl.node not in by_node:
                            by_node[tpl.node] = []
                        by_node[tpl.node].append(tpl)
                    
                    for node, templates in sorted(by_node.items()):
                        print(f"     Node {node}:")
                        for tpl in templates:
                            replica_tag = " [REPLICA]" if tpl.is_replica else ""
                            class_tag = " [CLASS]" if tpl.is_class_template else ""
                            print(f"       - {tpl.name} (VMID {tpl.proxmox_vmid}){replica_tag}{class_tag}")
            
        except Exception as e:
            print(f"✗ Failed to retrieve statistics: {e}")
            return
        
        print("\n" + "=" * 60)
        print("Migration completed successfully!")
        print("=" * 60)
        print("\nYou can now query templates using:")
        print("  GET /api/templates")
        print("  GET /api/templates/by-name/<name>")
        print("  GET /api/templates/nodes?vmid=<vmid>")
        print("  GET /api/templates/stats")


if __name__ == "__main__":
    migrate_database()
