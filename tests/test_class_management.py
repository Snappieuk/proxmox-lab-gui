#!/usr/bin/env python3
"""
Tests for class-based lab management system.

Run with: python -m pytest tests/test_class_management.py -v
Or directly: python tests/test_class_management.py
"""

import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_database_models_exist():
    """Test that database models can be imported."""
    from app.models import Class, Template, User, VMAssignment
    
    assert User is not None
    assert Class is not None
    assert Template is not None
    assert VMAssignment is not None
    print("✓ Database models exist")


def test_user_password_hashing():
    """Test that user password hashing works correctly."""
    from app.models import User
    
    user = User(username='testuser', role='user')
    user.set_password('testpassword123')
    
    assert user.password_hash is not None
    assert user.password_hash != 'testpassword123'
    assert user.check_password('testpassword123')
    assert not user.check_password('wrongpassword')
    
    print("✓ User password hashing works")


def test_user_roles():
    """Test that user role properties work correctly."""
    from app.models import User
    
    admin = User(username='admin', role='adminer')
    teacher = User(username='teacher', role='teacher')
    student = User(username='student', role='user')
    
    assert admin.is_adminer
    assert not admin.is_teacher
    assert not admin.is_user
    
    assert not teacher.is_adminer
    assert teacher.is_teacher
    assert not teacher.is_user
    
    assert not student.is_adminer
    assert not student.is_teacher
    assert student.is_user
    
    print("✓ User role properties work")


def test_class_token_generation():
    """Test that class invite token generation works."""
    from app.models import Class
    
    class_ = Class(name='Test Class', teacher_id=1)
    
    # Generate token that expires
    token = class_.generate_join_token(expires_in_days=7)
    assert token is not None
    assert len(token) > 20  # Token should be long enough
    assert class_.join_token == token
    assert class_.token_expires_at is not None
    assert not class_.token_never_expires
    assert class_.is_token_valid()
    
    # Generate token that never expires
    class_.generate_join_token(expires_in_days=0)
    assert class_.token_never_expires
    assert class_.token_expires_at is None
    assert class_.is_token_valid()
    
    # Invalidate token
    class_.invalidate_token()
    assert class_.join_token is None
    assert not class_.is_token_valid()
    
    print("✓ Class invite token generation works")


def test_vm_assignment_states():
    """Test VM assignment state transitions."""
    from app.models import User, VMAssignment
    
    user = User(username='student', role='user')
    user.id = 1  # Simulate database ID
    
    assignment = VMAssignment(class_id=1, proxmox_vmid=100, status='available')
    assert assignment.status == 'available'
    assert assignment.assigned_user_id is None
    
    # Assign to user
    assignment.assign_to_user(user)
    assert assignment.status == 'assigned'
    assert assignment.assigned_user_id == 1
    assert assignment.assigned_at is not None
    
    # Unassign
    assignment.unassign()
    assert assignment.status == 'available'
    assert assignment.assigned_user_id is None
    assert assignment.assigned_at is None
    
    print("✓ VM assignment state transitions work")


def test_model_to_dict():
    """Test that model to_dict methods work correctly."""
    from app.models import Template, User, VMAssignment
    
    user = User(username='testuser', role='teacher')
    user.id = 1
    
    user_dict = user.to_dict()
    assert user_dict['username'] == 'testuser'
    assert user_dict['role'] == 'teacher'
    assert user_dict['id'] == 1
    assert 'password_hash' not in user_dict  # Should not expose password
    
    template = Template(name='Linux Template', proxmox_vmid=9000)
    template.id = 1
    template_dict = template.to_dict()
    assert template_dict['name'] == 'Linux Template'
    assert template_dict['proxmox_vmid'] == 9000
    
    assignment = VMAssignment(class_id=1, proxmox_vmid=100, status='available')
    assignment.id = 1
    assignment_dict = assignment.to_dict()
    assert assignment_dict['proxmox_vmid'] == 100
    assert assignment_dict['status'] == 'available'
    
    print("✓ Model to_dict methods work")


def test_class_service_functions_exist():
    """Test that class service functions exist."""
    from app.services.class_service import (
        assign_vm_to_user,
        create_class,
        create_template,
        create_vm_assignment,
        generate_class_invite,
        get_available_templates,
        get_class_by_id,
        get_classes_for_student,
        get_classes_for_teacher,
        get_user_vm_in_class,
        get_vm_assignments_for_class,
        join_class_via_token,
        unassign_vm,
    )
    
    assert callable(create_class)
    assert callable(get_class_by_id)
    assert callable(get_classes_for_teacher)
    assert callable(get_classes_for_student)
    assert callable(generate_class_invite)
    assert callable(join_class_via_token)
    assert callable(create_template)
    assert callable(get_available_templates)
    assert callable(create_vm_assignment)
    assert callable(assign_vm_to_user)
    assert callable(unassign_vm)
    assert callable(get_vm_assignments_for_class)
    assert callable(get_user_vm_in_class)
    
    print("✓ Class service functions exist")


def test_proxmox_operations_functions_exist():
    """Test that Proxmox operations functions exist."""
    from app.services.proxmox_operations import (
        CLASS_CLUSTER_IP,
        clone_vm_from_template,
        convert_vm_to_template,
        create_vm_snapshot,
        delete_vm,
        get_vm_status,
        list_proxmox_templates,
        revert_vm_to_snapshot,
        start_class_vm,
        stop_class_vm,
    )
    
    # DELETED: clone_vms_for_class() was removed - replaced by class_vm_service.create_class_vms()
    # which uses disk export + overlay approach instead of slow qm clone
    
    assert callable(list_proxmox_templates)
    assert callable(clone_vm_from_template)
    assert callable(convert_vm_to_template)
    assert callable(create_vm_snapshot)
    assert callable(revert_vm_to_snapshot)
    assert callable(delete_vm)
    assert callable(start_class_vm)
    assert callable(stop_class_vm)
    assert callable(get_vm_status)
    assert CLASS_CLUSTER_IP == '10.220.15.249'
    
    print("✓ Proxmox operations functions exist")


def test_api_routes_registered():
    """Test that API routes are registered with the Flask app."""
    import os
    import sys

    # Ensure the app package is imported from the correct location
    app_dir = os.path.join(os.path.dirname(__file__), '..')
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    
    from app import create_app
    
    app = create_app()
    
    # Collect all class-related routes
    class_routes = []
    for rule in app.url_map.iter_rules():
        if 'class' in rule.rule.lower():
            class_routes.append(rule.rule)
    
    # Check essential routes exist
    assert '/classes' in class_routes
    assert '/api/classes' in class_routes
    assert '/api/classes/templates' in class_routes
    
    print(f"✓ API routes registered ({len(class_routes)} class-related routes)")


def test_flask_app_with_database():
    """Test that Flask app initializes with database."""
    import os
    import sys

    # Ensure the app package is imported from the correct location
    app_dir = os.path.join(os.path.dirname(__file__), '..')
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    
    from app import create_app
    from app.models import db

    # Create app with temp database
    app = create_app({
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'TESTING': True
    })
    
    with app.app_context():
        # Database should be initialized
        from app.models import User

        # Create tables
        db.create_all()
        
        # Test creating a user
        user = User(username='testadmin', role='adminer')
        user.set_password('testpass')
        db.session.add(user)
        db.session.commit()
        
        # Verify user was created
        fetched = User.query.filter_by(username='testadmin').first()
        assert fetched is not None
        assert fetched.is_adminer
    
    print("✓ Flask app works with database")


def test_vm_utils_functions_exist():
    """Test that VM utility functions can be imported."""
    from app.services.vm_utils import (
        build_vm_name,
        format_mac_address,
        get_next_available_vmid_api,
        get_next_available_vmid_ssh,
        get_vm_mac_address_api,
        get_vm_mac_address_ssh,
        normalize_mac_address,
        parse_disk_config,
        sanitize_vm_name,
    )
    
    assert callable(sanitize_vm_name)
    assert callable(get_next_available_vmid_ssh)
    assert callable(get_next_available_vmid_api)
    assert callable(get_vm_mac_address_ssh)
    assert callable(get_vm_mac_address_api)
    assert callable(normalize_mac_address)
    assert callable(format_mac_address)
    assert callable(parse_disk_config)
    assert callable(build_vm_name)
    
    # Test sanitize_vm_name
    assert sanitize_vm_name("Test Class 123!") == "test-class-123"
    assert sanitize_vm_name("") == "vm"
    assert sanitize_vm_name("abc") == "abc"
    
    # Test normalize_mac_address
    assert normalize_mac_address("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"
    assert normalize_mac_address("aa-bb-cc-dd-ee-ff") == "aabbccddeeff"
    
    # Test format_mac_address
    assert format_mac_address("aabbccddeeff") == "AA:BB:CC:DD:EE:FF"
    
    # Test parse_disk_config
    disk_info = parse_disk_config("TRUENAS-NFS:100/vm-100-disk-0.qcow2,size=32G")
    assert disk_info["storage"] == "TRUENAS-NFS"
    assert disk_info["size_gb"] == 32.0
    
    # Test build_vm_name
    assert build_vm_name("myclass", "student", 1) == "myclass-student-1"
    
    print("✓ VM utility functions exist and work")


def test_vm_core_functions_exist():
    """Test that VM core functions can be imported."""
    from app.services.vm_core import (
        attach_disk_to_vm,
        convert_disk_to_qcow2,
        create_overlay_disk,
        create_snapshot_ssh,
        create_vm_shell,
        create_vm_with_disk,
        delete_snapshot_ssh,
        destroy_vm,
        get_vm_config_ssh,
        get_vm_disk_path,
        get_vm_status_ssh,
        rollback_snapshot_ssh,
        set_vm_options,
        start_vm,
        stop_vm,
        vm_exists,
        wait_for_vm_stopped,
    )
    
    assert callable(create_vm_shell)
    assert callable(create_vm_with_disk)
    assert callable(destroy_vm)
    assert callable(start_vm)
    assert callable(stop_vm)
    assert callable(wait_for_vm_stopped)
    assert callable(get_vm_status_ssh)
    assert callable(attach_disk_to_vm)
    assert callable(create_overlay_disk)
    assert callable(convert_disk_to_qcow2)
    assert callable(create_snapshot_ssh)
    assert callable(rollback_snapshot_ssh)
    assert callable(delete_snapshot_ssh)
    assert callable(get_vm_config_ssh)
    assert callable(set_vm_options)
    assert callable(vm_exists)
    assert callable(get_vm_disk_path)
    
    print("✓ VM core functions exist")


def test_vm_template_functions_exist():
    """Test that VM template functions can be imported."""
    from app.services.vm_template import (
        cleanup_base_image,
        convert_to_template,
        create_overlay_vm,
        create_student_overlays,
        export_template_to_qcow2,
        full_clone_vm,
        get_template_disk_info,
        linked_clone_vm,
        push_base_to_students,
        update_overlay_backing_file,
        verify_base_image_exists,
    )
    
    assert callable(export_template_to_qcow2)
    assert callable(create_overlay_vm)
    assert callable(create_student_overlays)
    assert callable(full_clone_vm)
    assert callable(linked_clone_vm)
    assert callable(convert_to_template)
    assert callable(update_overlay_backing_file)
    assert callable(push_base_to_students)
    assert callable(get_template_disk_info)
    assert callable(verify_base_image_exists)
    assert callable(cleanup_base_image)
    
    print("✓ VM template functions exist")


def run_all_tests():
    """Run all tests."""
    print("\n=== Running Class Management Tests ===\n")
    
    tests = [
        test_database_models_exist,
        test_user_password_hashing,
        test_user_roles,
        test_class_token_generation,
        test_vm_assignment_states,
        test_model_to_dict,
        test_class_service_functions_exist,
        test_proxmox_operations_functions_exist,
        test_vm_utils_functions_exist,
        test_vm_core_functions_exist,
        test_vm_template_functions_exist,
        test_api_routes_registered,
        test_flask_app_with_database,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1
    
    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
