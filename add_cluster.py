#!/usr/bin/env python3
"""
Quick script to add a cluster to the database.
Run this to configure your first Proxmox cluster.
"""

import sys
import getpass
from app import create_app
from app.models import db, Cluster

def add_cluster():
    """Interactive cluster addition."""
    app = create_app()
    
    with app.app_context():
        print("\n🔧 Add Proxmox Cluster Configuration")
        print("=" * 50)
        
        # Get cluster details
        cluster_id = input("Cluster ID (e.g., 'pve1'): ").strip()
        if not cluster_id:
            print("❌ Cluster ID is required")
            return 1
        
        # Check if cluster already exists
        existing = Cluster.query.filter_by(cluster_id=cluster_id).first()
        if existing:
            print(f"❌ Cluster '{cluster_id}' already exists!")
            return 1
        
        name = input("Cluster Name (e.g., 'Production PVE'): ").strip() or cluster_id
        host = input("Proxmox Host (IP or hostname): ").strip()
        if not host:
            print("❌ Host is required")
            return 1
        
        port = input("Port [8006]: ").strip() or "8006"
        user = input("Admin Username (e.g., 'root@pam'): ").strip()
        if not user:
            print("❌ Username is required")
            return 1
        
        password = getpass.getpass("Admin Password: ")
        if not password:
            print("❌ Password is required")
            return 1
        
        verify_ssl = input("Verify SSL? (y/N): ").strip().lower() == 'y'
        is_default = input("Set as default cluster? (Y/n): ").strip().lower() != 'n'
        
        # Optional settings
        print("\n📋 Optional Settings (press Enter to skip)")
        qcow2_template_path = input("QCOW2 Template Path [/mnt/pve/templates]: ").strip() or "/mnt/pve/templates"
        qcow2_images_path = input("QCOW2 Images Path [/mnt/pve/images]: ").strip() or "/mnt/pve/images"
        admin_group = input("Admin Group [adminers]: ").strip() or "adminers"
        
        # Create cluster
        cluster = Cluster(
            cluster_id=cluster_id,
            name=name,
            host=host,
            port=int(port),
            user=user,
            password=password,
            verify_ssl=verify_ssl,
            is_default=is_default,
            is_active=True,
            qcow2_template_path=qcow2_template_path,
            qcow2_images_path=qcow2_images_path,
            admin_group=admin_group,
            enable_ip_lookup=True,
            enable_ip_persistence=False,
            vm_cache_ttl=300,
        )
        
        try:
            # If this is default, unset other defaults
            if is_default:
                Cluster.query.update({'is_default': False})
            
            db.session.add(cluster)
            db.session.commit()
            
            print(f"\n✅ Cluster '{name}' added successfully!")
            print(f"   Cluster ID: {cluster_id}")
            print(f"   Host: {host}:{port}")
            print(f"   User: {user}")
            print(f"   Default: {is_default}")
            print("\n🚀 You can now login to the application!")
            return 0
            
        except Exception as e:
            db.session.rollback()
            print(f"\n❌ Failed to add cluster: {e}")
            import traceback
            traceback.print_exc()
            return 1

if __name__ == '__main__':
    sys.exit(add_cluster())
