#!/usr/bin/env python3
"""Check actual assignments for a specific student."""

import sys
from app import create_app
from app.models import db, User, VMAssignment, Class

app = create_app()

with app.app_context():
    # Get the class
    class_obj = Class.query.filter_by(name='Sys-Security').first()
    if not class_obj:
        print("❌ Class 'Sys-Security' not found")
        sys.exit(1)
    
    print(f"✓ Found class: Sys-Security (ID: {class_obj.id})")
    
    # Get enrolled students properly
    students = class_obj.students.all()
    print(f"  Enrolled students: {len(students)}")
    for student in students:
        print(f"    - {student.username} (user_id: {student.id})")
    print()
    
    # Check all assignments for VM 53518
    print("=== ALL ASSIGNMENTS FOR VM 53518 ===")
    all_53518 = VMAssignment.query.filter_by(proxmox_vmid=53518).all()
    if not all_53518:
        print("❌ VM 53518 has NO assignments in database!")
    else:
        for i, assign in enumerate(all_53518, 1):
            print(f"[{i}] Assignment ID: {assign.id}")
            print(f"    class_id: {assign.class_id}")
            if assign.class_id:
                cls = Class.query.get(assign.class_id)
                print(f"    in class: {cls.name if cls else 'UNKNOWN'}")
            print(f"    assigned_user_id: {assign.assigned_user_id}")
            if assign.assigned_user_id:
                user = User.query.get(assign.assigned_user_id)
                print(f"    assigned to: {user.username if user else 'UNKNOWN'}")
            else:
                print(f"    assigned to: UNASSIGNED")
            print(f"    status: {assign.status}")
            print()
    
    # Check what the query returns
    print("=== WHAT THE CLASS PAGE QUERY RETURNS ===")
    students = class_obj.students.all()
    for student in students:
        assignment = VMAssignment.query.filter_by(
            class_id=class_obj.id,
            assigned_user_id=student.id
        ).first()
        
        if assignment:
            print(f"✓ {student.username}: VM {assignment.proxmox_vmid} (status: {assignment.status})")
        else:
            print(f"✗ {student.username}: NO VM in this class")
    
    print()
    print("=== DEBUG: Check ALL assignments for all enrolled students ===")
    students = class_obj.students.all()
    for student in students:
        all_assignments = VMAssignment.query.filter_by(assigned_user_id=student.id).all()
        print(f"\n{student.username} (user_id: {student.id}) has {len(all_assignments)} total assignment(s):")
        for assign in all_assignments:
            cls_name = Class.query.get(assign.class_id).name if assign.class_id else "DIRECT"
            print(f"  - VM {assign.proxmox_vmid} in {cls_name} (class_id={assign.class_id}, status={assign.status})")
