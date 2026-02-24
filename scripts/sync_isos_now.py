#!/usr/bin/env python3
"""
Manual ISO sync trigger script.

Run this to immediately sync all ISOs from Proxmox to database.
"""

import sys
import os

# Add app directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.services.iso_sync import sync_isos_from_proxmox

def main():
    print("Creating Flask app context...")
    app = create_app()
    
    with app.app_context():
        print("\n" + "="*60)
        print("Starting ISO sync from Proxmox...")
        print("="*60 + "\n")
        
        stats = sync_isos_from_proxmox(full_sync=True)
        
        print("\n" + "="*60)
        print("ISO Sync Complete!")
        print("="*60)
        print(f"ISOs found:    {stats['isos_found']}")
        print(f"ISOs added:    {stats['isos_added']}")
        print(f"ISOs updated:  {stats['isos_updated']}")
        print(f"ISOs removed:  {stats['isos_removed']}")
        
        if stats['errors']:
            print(f"\nErrors ({len(stats['errors'])}):")
            for error in stats['errors']:
                print(f"  - {error}")
        
        print("\nDone! ISOs are now cached in database.")

if __name__ == '__main__':
    main()
