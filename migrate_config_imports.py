#!/usr/bin/env python3
"""
Automated migration script to replace config.py imports with settings_service.

This script updates all files that import from app.config to use the new
database-first architecture with app.services.settings_service.
"""

import re
import sys
from pathlib import Path

# Define replacement patterns
REPLACEMENTS = [
    # Simple import replacements
    (
        r'from app\.config import CLUSTERS\n',
        'from app.services.proxmox_service import get_clusters_from_db\n'
    ),
    (
        r'from app\.config import CLUSTERS, ',
        'from app.services.proxmox_service import get_clusters_from_db\nfrom app.config import '
    ),
    # CLUSTERS usage (need context-aware replacement)
    (
        r'\bCLUSTERS\b',
        'get_clusters_from_db()'
    ),
    # ARP_SUBNETS import
    (
        r'from app\.config import ARP_SUBNETS',
        '# ARP_SUBNETS now from settings_service.get_arp_subnets(cluster_dict)'
    ),
    # ADMIN imports
    (
        r'from app\.config import ADMIN_GROUP, ADMIN_USERS',
        'from app.services.settings_service import get_all_admin_users, get_all_admin_groups'
    ),
    (
        r'from app\.config import ADMIN_USERS, ADMIN_GROUP',
        'from app.services.settings_service import get_all_admin_users, get_all_admin_groups'
    ),
    # VALID_NODES
    (
        r'from app\.config import.*VALID_NODES.*\n',
        '# VALID_NODES deprecated - no longer used\n'
    ),
]

def process_file(filepath: Path, dry_run=True):
    """Process a single file with replacements."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        original_content = content
        modified = False
        
        # Apply replacements
        for pattern, replacement in REPLACEMENTS:
            if re.search(pattern, content):
                content = re.sub(pattern, replacement, content)
                modified = True
        
        if modified:
            if dry_run:
                print(f"✏️  Would modify: {filepath}")
                # Show diff
                print("   Changes:")
                for pattern, replacement in REPLACEMENTS:
                    matches = re.findall(pattern, original_content)
                    if matches:
                        print(f"      {pattern} -> {replacement}")
            else:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f"✅ Modified: {filepath}")
            return True
        else:
            print(f"⏭️  Skipped (no changes): {filepath}")
            return False
            
    except Exception as e:
        print(f"❌ Error processing {filepath}: {e}")
        return False

def main():
    """Main migration function."""
    dry_run = '--apply' not in sys.argv
    
    if dry_run:
        print("🔍 DRY RUN MODE - No files will be modified")
        print("   Run with --apply to actually make changes\n")
    else:
        print("⚠️  APPLY MODE - Files will be modified!\n")
    
    # Files to process (from grep results)
    files_to_process = [
        'app/routes/api/vms.py',
        'app/routes/api/class_api.py',
        'app/routes/api/class_template.py',
        'app/routes/api/class_ip_refresh.py',
        'app/routes/api/templates.py',
        'app/routes/api/template_migrate.py',
        'app/routes/api/clusters.py',
        'app/routes/api/vnc_proxy.py',
        'app/routes/auth.py',
        'app/services/class_vm_service.py',
        'app/services/class_service.py',
        'app/services/proxmox_operations.py',
        'app/services/vm_deployment_service.py',
        'app/services/template_sync.py',
        'app/services/cluster_manager.py',
        'app/services/mappings_service.py',
    ]
    
    base_path = Path(__file__).parent
    modified_count = 0
    
    for file_path in files_to_process:
        full_path = base_path / file_path
        if full_path.exists():
            if process_file(full_path, dry_run):
                modified_count += 1
        else:
            print(f"⚠️  File not found: {full_path}")
    
    print(f"\n📊 Summary: {modified_count} files {'would be' if dry_run else 'were'} modified")
    
    if dry_run:
        print("\n💡 Run with --apply to make actual changes")

if __name__ == '__main__':
    main()
