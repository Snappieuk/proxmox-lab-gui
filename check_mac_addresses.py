#!/usr/bin/env python3
"""
Diagnostic script to check MAC addresses in VMAssignment and VMInventory tables.
Run this after creating VMs to verify MAC addresses are being stored correctly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.models import Class, VMAssignment, VMInventory


def main():
    app = create_app()
    
    with app.app_context():
        print("=" * 80)
        print("VMAssignment Table - All Records")
        print("=" * 80)
        
        assignments = VMAssignment.query.all()
        
        if not assignments:
            print("No VMAssignment records found!")
        else:
            print(f"\nFound {len(assignments)} VMAssignment records:\n")
            
            for assignment in assignments:
                class_name = assignment.class_.name if assignment.class_ else "N/A"
                print(f"VMID: {assignment.proxmox_vmid:4d} | "
                      f"Name: {assignment.vm_name:20s} | "
                      f"MAC: {assignment.mac_address or 'NULL':17s} | "
                      f"IP: {assignment.cached_ip or 'NULL':15s} | "
                      f"Class: {class_name:20s} | "
                      f"Teacher: {assignment.is_teacher_vm}")
        
        print("\n" + "=" * 80)
        print("VMInventory Table - All Records")
        print("=" * 80)
        
        inventory_records = VMInventory.query.all()
        
        if not inventory_records:
            print("No VMInventory records found!")
        else:
            print(f"\nFound {len(inventory_records)} VMInventory records:\n")
            
            for record in inventory_records:
                print(f"VMID: {record.vmid:4d} | "
                      f"Cluster: {record.cluster_id:2d} | "
                      f"MAC: {record.mac_address or 'NULL':17s} | "
                      f"IP: {record.ip or 'NULL':15s} | "
                      f"Status: {record.status:10s} | "
                      f"Node: {record.node or 'N/A'}")
        
        print("\n" + "=" * 80)
        print("Missing MAC Addresses")
        print("=" * 80)
        
        missing_mac = VMAssignment.query.filter(
            (VMAssignment.mac_address is None) | (VMAssignment.mac_address == '')
        ).all()
        
        if missing_mac:
            print(f"\nWARNING: {len(missing_mac)} VMAssignment records have NULL or empty MAC addresses:\n")
            for assignment in missing_mac:
                class_name = assignment.class_.name if assignment.class_ else "N/A"
                print(f"  VMID: {assignment.proxmox_vmid} | Name: {assignment.vm_name} | Class: {class_name}")
        else:
            print("\n✓ All VMAssignment records have MAC addresses")
        
        print("\n" + "=" * 80)
        print("Summary by Class")
        print("=" * 80)
        
        classes = Class.query.all()
        
        if not classes:
            print("No classes found!")
        else:
            print()
            for class_ in classes:
                assignments = VMAssignment.query.filter_by(class_id=class_.id).all()
                with_mac = sum(1 for a in assignments if a.mac_address)
                without_mac = sum(1 for a in assignments if not a.mac_address)
                
                print(f"Class: {class_.name}")
                print(f"  Total VMs: {len(assignments)}")
                print(f"  With MAC: {with_mac}")
                print(f"  Without MAC: {without_mac}")
                
                if without_mac > 0:
                    print(f"  WARNING: {without_mac} VMs missing MAC addresses!")
                print()

if __name__ == "__main__":
    main()
