#!/usr/bin/env python3
"""
Database cleanup utility for proxmox-lab-gui.

This script allows you to manually clean up the database, including:
- Force delete classes that can't be deleted normally
- Remove orphaned VM assignments
- Clear IP cache entries
- List and inspect database contents

USAGE:
    Interactive mode:
        python cleanup_database.py

    Command-line mode:
        python cleanup_database.py list-classes
        python cleanup_database.py list-vms
        python cleanup_database.py delete-class <class_id>
        python cleanup_database.py clear-ip-cache

IMPORTANT:
    This script only modifies the database. It does NOT delete VMs from Proxmox.
    After deleting classes/VMs from the database, you may need to manually clean up
    VMs in the Proxmox web interface.
"""

import sys
import os

# Add app directory to path
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.models import db, User, Class, Template, VMAssignment, VMIPCache

def list_classes():
    """List all classes in the database."""
    classes = Class.query.all()
    if not classes:
        print("No classes found in database.")
        return
    
    print(f"\n{'ID':<5} {'Name':<30} {'Teacher':<20} {'VMs':<5} {'Students':<8}")
    print("-" * 70)
    for cls in classes:
        teacher = User.query.get(cls.teacher_id)
        teacher_name = teacher.username if teacher else "Unknown"
        vm_count = len(cls.vm_assignments)
        student_count = len(list(cls.students))
        print(f"{cls.id:<5} {cls.name:<30} {teacher_name:<20} {vm_count:<5} {student_count:<8}")

def list_vm_assignments():
    """List all VM assignments."""
    assignments = VMAssignment.query.all()
    if not assignments:
        print("No VM assignments found in database.")
        return
    
    print(f"\n{'ID':<5} {'VMID':<8} {'Node':<15} {'Class':<30} {'User':<20} {'Status':<10}")
    print("-" * 100)
    for vm in assignments:
        class_name = vm.class_.name if vm.class_ else "No class"
        user_name = vm.assigned_user.username if vm.assigned_user else "Unassigned"
        print(f"{vm.id:<5} {vm.proxmox_vmid:<8} {vm.node:<15} {class_name:<30} {user_name:<20} {vm.status:<10}")

def delete_class_by_id(class_id):
    """Force delete a class from database without touching Proxmox."""
    cls = Class.query.get(class_id)
    if not cls:
        print(f"Class with ID {class_id} not found.")
        return False
    
    print(f"\nClass: {cls.name}")
    print(f"Teacher: {User.query.get(cls.teacher_id).username if cls.teacher_id else 'Unknown'}")
    print(f"VM Assignments: {len(cls.vm_assignments)}")
    print(f"Students Enrolled: {len(list(cls.students))}")
    
    confirm = input(f"\nAre you sure you want to DELETE this class from the database? (type 'yes' to confirm): ")
    if confirm.lower() != 'yes':
        print("Cancelled.")
        return False
    
    try:
        # Delete class (cascades to VM assignments via foreign key)
        db.session.delete(cls)
        db.session.commit()
        print(f"✓ Successfully deleted class '{cls.name}' (ID: {class_id})")
        print(f"  Note: VMs in Proxmox were NOT deleted. Clean them up manually if needed.")
        return True
    except Exception as e:
        db.session.rollback()
        print(f"✗ Failed to delete class: {e}")
        return False

def clear_ip_cache():
    """Clear all IP cache entries from database."""
    count = VMIPCache.query.count()
    if count == 0:
        print("IP cache is already empty.")
        return
    
    confirm = input(f"\nDelete {count} IP cache entries? (type 'yes' to confirm): ")
    if confirm.lower() != 'yes':
        print("Cancelled.")
        return
    
    try:
        VMIPCache.query.delete()
        db.session.commit()
        print(f"✓ Cleared {count} IP cache entries")
    except Exception as e:
        db.session.rollback()
        print(f"✗ Failed to clear IP cache: {e}")

def delete_vm_assignment(assignment_id):
    """Delete a specific VM assignment."""
    vm = VMAssignment.query.get(assignment_id)
    if not vm:
        print(f"VM assignment with ID {assignment_id} not found.")
        return False
    
    print(f"\nVM Assignment:")
    print(f"  VMID: {vm.proxmox_vmid}")
    print(f"  Node: {vm.node}")
    print(f"  Class: {vm.class_.name if vm.class_ else 'None'}")
    print(f"  User: {vm.assigned_user.username if vm.assigned_user else 'Unassigned'}")
    
    confirm = input(f"\nDelete this VM assignment from database? (type 'yes' to confirm): ")
    if confirm.lower() != 'yes':
        print("Cancelled.")
        return False
    
    try:
        db.session.delete(vm)
        db.session.commit()
        print(f"✓ Successfully deleted VM assignment (ID: {assignment_id})")
        print(f"  Note: VM {vm.proxmox_vmid} in Proxmox was NOT deleted.")
        return True
    except Exception as e:
        db.session.rollback()
        print(f"✗ Failed to delete VM assignment: {e}")
        return False

def main_menu():
    """Show interactive menu."""
    while True:
        print("\n" + "="*70)
        print("Database Cleanup Utility - Proxmox Lab GUI")
        print("="*70)
        print("1. List all classes")
        print("2. List all VM assignments")
        print("3. Force delete class by ID")
        print("4. Delete VM assignment by ID")
        print("5. Clear IP cache")
        print("0. Exit")
        print()
        
        choice = input("Enter choice: ").strip()
        
        if choice == '0':
            print("Exiting...")
            break
        elif choice == '1':
            list_classes()
        elif choice == '2':
            list_vm_assignments()
        elif choice == '3':
            list_classes()
            class_id = input("\nEnter class ID to delete (or 'q' to cancel): ").strip()
            if class_id.lower() != 'q' and class_id.isdigit():
                delete_class_by_id(int(class_id))
        elif choice == '4':
            list_vm_assignments()
            vm_id = input("\nEnter VM assignment ID to delete (or 'q' to cancel): ").strip()
            if vm_id.lower() != 'q' and vm_id.isdigit():
                delete_vm_assignment(int(vm_id))
        elif choice == '5':
            clear_ip_cache()
        else:
            print("Invalid choice. Please try again.")

if __name__ == "__main__":
    print("Initializing database connection...")
    app = create_app()
    
    with app.app_context():
        print("✓ Connected to database")
        
        if len(sys.argv) > 1:
            # Command-line mode
            cmd = sys.argv[1]
            if cmd == "list-classes":
                list_classes()
            elif cmd == "list-vms":
                list_vm_assignments()
            elif cmd == "delete-class" and len(sys.argv) > 2:
                delete_class_by_id(int(sys.argv[2]))
            elif cmd == "clear-ip-cache":
                clear_ip_cache()
            else:
                print("Usage:")
                print("  python cleanup_database.py list-classes")
                print("  python cleanup_database.py list-vms")
                print("  python cleanup_database.py delete-class <class_id>")
                print("  python cleanup_database.py clear-ip-cache")
                print("  python cleanup_database.py   (interactive mode)")
        else:
            # Interactive mode
            main_menu()
