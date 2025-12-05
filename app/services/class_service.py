#!/usr/bin/env python3
"""
Class management service for lab portal.

Handles class CRUD, template management, VM pool operations, and invite links.
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict, Any

from app.models import db, User, Class, Template, VMAssignment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------

def get_user_by_username(username: str) -> Optional[User]:
    """Get user by username."""
    return User.query.filter_by(username=username).first()


def get_user_by_id(user_id: int) -> Optional[User]:
    """Get user by ID."""
    return User.query.get(user_id)


def create_local_user(username: str, password: str, role: str = 'user') -> Tuple[bool, str]:
    """Create a new local user.
    
    Returns: (success, message/error)
    """
    if not username:
        return False, "Username is required"
    
    # Allow None/empty password for admin accounts (they auth via Proxmox)
    if not password and role != 'adminer':
        return False, "Password is required"
    
    # Validate role
    if role not in ('adminer', 'teacher', 'user'):
        return False, "Invalid role. Must be adminer, teacher, or user"
    
    # Check if user already exists
    existing = User.query.filter_by(username=username).first()
    if existing:
        return False, "Username already exists"
    
    # Create user
    user = User(username=username, role=role)
    user.set_password(password)
    
    try:
        db.session.add(user)
        db.session.commit()
        logger.info("Created local user: %s with role %s", username, role)
        return True, f"User {username} created successfully"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to create user %s: %s", username, e)
        return False, f"Failed to create user: {str(e)}"


def update_user_role(user_id: int, new_role: str) -> Tuple[bool, str]:
    """Update a user's role (adminer only).
    
    Returns: (success, message/error)
    """
    if new_role not in ('adminer', 'teacher', 'user'):
        return False, "Invalid role. Must be adminer, teacher, or user"
    
    user = User.query.get(user_id)
    if not user:
        return False, "User not found"
    
    old_role = user.role
    user.role = new_role
    
    try:
        db.session.commit()
        logger.info("Updated user %s role: %s -> %s", user.username, old_role, new_role)
        return True, f"User {user.username} role updated to {new_role}"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to update role for user %s: %s", user.username, e)
        return False, f"Failed to update role: {str(e)}"


def authenticate_local_user(username: str, password: str) -> Optional[User]:
    """Authenticate a local user by username and password."""
    user = User.query.filter_by(username=username).first()
    if user and user.check_password(password):
        return user
    return None


def list_all_users() -> List[User]:
    """List all users."""
    return User.query.order_by(User.username).all()


def delete_user(user_id: int) -> Tuple[bool, str]:
    """Delete a user and their assignments.
    
    Returns: (success, message/error)
    """
    user = User.query.get(user_id)
    if not user:
        return False, "User not found"
    
    # Don't allow deleting adminer accounts
    if user.role == 'adminer':
        return False, "Cannot delete admin accounts"
    
    username = user.username
    
    try:
        # Delete user's VM assignments (unassign VMs, don't delete the VMs)
        assignments = VMAssignment.query.filter_by(assigned_user_id=user_id).all()
        for assignment in assignments:
            assignment.assigned_user_id = None
            assignment.status = 'available'
        
        # Delete user
        db.session.delete(user)
        db.session.commit()
        logger.info("Deleted user: %s", username)
        return True, f"User {username} deleted successfully"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to delete user %s: %s", username, e)
        return False, f"Failed to delete user: {str(e)}"


# ---------------------------------------------------------------------------
# Class Management
# ---------------------------------------------------------------------------

def create_class(name: str, teacher_id: int, description: str = None, 
                 template_id: int = None, pool_size: int = 0,
                 cpu_cores: int = 2, memory_mb: int = 2048, disk_size_gb: int = 32) -> Tuple[Optional[Class], str]:
    """Create a new class.
    
    Template is optional - classes can be created without templates and VMs can be added manually later.
    
    Args:
        disk_size_gb: Disk size in GB (only used for template-less classes, default 32)
    
    Returns: (class, message/error)
    """
    if not name:
        return None, "Class name is required"
    
    teacher = User.query.get(teacher_id)
    if not teacher:
        return None, "Teacher not found"
    
    if not teacher.is_teacher and not teacher.is_adminer:
        return None, "User must be a teacher or adminer to create classes"
    
    # Verify template exists if specified (template is now optional)
    original_template = None
    if template_id:
        original_template = Template.query.get(template_id)
        if not original_template:
            return None, "Template not found"
    
    try:
        # NO VM creation during class creation anymore
        # VMs are created later via the "Create VMs" button which calls deploy_class_vms()
        # This uses the modern disk export + overlay approach instead of qm clone
        
        # Create the class record with reference to the original template
        # (NOT a cloned template - we'll export the disk when creating VMs)
        class_ = Class(
            name=name,
            description=description,
            teacher_id=teacher_id,
            template_id=template_id,  # Reference original template directly
            pool_size=pool_size,
            cpu_cores=cpu_cores,
            memory_mb=memory_mb,
            disk_size_gb=disk_size_gb
        )
        db.session.add(class_)
        db.session.flush()

        # Auto-generate join token (7-day default) if none
        class_.generate_join_token(expires_in_days=7)

        db.session.commit()
        logger.info("Created class: %s by teacher %s (template_id=%s)", 
                   name, teacher.username, template_id)
        
        # Note: VMs are created via the "Create VMs" button in the UI
        # which calls /api/classes/{id}/vms endpoint -> deploy_class_vms()
        if pool_size > 0:
            logger.info(f"Class created with pool_size={pool_size}. Use 'Create VMs' button to deploy VMs using disk export + overlay method")
        
        return class_, "Class created successfully"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to create class %s: %s", name, e)
        return None, f"Failed to create class: {str(e)}"


def get_class_by_id(class_id: int) -> Optional[Class]:
    """Get class by ID."""
    return Class.query.get(class_id)


def get_class_by_token(token: str) -> Optional[Class]:
    """Get class by join token."""
    return Class.query.filter_by(join_token=token).first()


def get_classes_for_teacher(teacher_id: int) -> List[Class]:
    """Get all classes accessible to a teacher (taught by them OR enrolled in)."""
    user = User.query.get(teacher_id)
    if not user:
        return []
    
    # Get classes they teach
    taught_classes = Class.query.filter_by(teacher_id=teacher_id).all()
    
    # Get classes they're enrolled in as students
    enrolled_classes = list(user.enrolled_classes)
    
    # Combine and deduplicate (in case they're somehow enrolled in their own class)
    all_classes = {cls.id: cls for cls in taught_classes + enrolled_classes}
    
    # Sort by created_at descending
    return sorted(all_classes.values(), key=lambda c: c.created_at, reverse=True)


def get_classes_for_student(user_id: int) -> List[Class]:
    """Get all classes a student is enrolled in."""
    user = User.query.get(user_id)
    if not user:
        return []
    return list(user.enrolled_classes)


def list_all_classes() -> List[Class]:
    """List all classes (for adminer)."""
    return Class.query.order_by(Class.created_at.desc()).all()


def update_class(class_id: int, name: str = None, description: str = None,
                 template_id: int = None, pool_size: int = None) -> Tuple[bool, str]:
    """Update class properties.
    
    Returns: (success, message/error)
    """
    class_ = Class.query.get(class_id)
    if not class_:
        return False, "Class not found"
    
    if name:
        class_.name = name
    if description is not None:
        class_.description = description
    if template_id is not None:
        class_.template_id = template_id
    if pool_size is not None:
        class_.pool_size = pool_size
    
    try:
        db.session.commit()
        logger.info("Updated class: %s", class_.name)
        return True, "Class updated successfully"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to update class %s: %s", class_id, e)
        return False, f"Failed to update class: {str(e)}"


def delete_class(class_id: int) -> Tuple[bool, str, List[int]]:
    """Delete a class and all its VM assignments.
    
    Returns: (success, message/error, list of proxmox VMIDs to delete)
    """
    class_ = Class.query.get(class_id)
    if not class_:
        return False, "Class not found", []
    
    # Collect VMIDs that need to be deleted from Proxmox
    vmids_to_delete = [vm.proxmox_vmid for vm in class_.vm_assignments]
    
    try:
        # Delete class (cascades to VM assignments)
        db.session.delete(class_)
        db.session.commit()
        logger.info("Deleted class: %s (VMIDs to clean: %s)", class_.name, vmids_to_delete)
        return True, "Class deleted successfully", vmids_to_delete
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to delete class %s: %s", class_id, e)
        return False, f"Failed to delete class: {str(e)}", []


# ---------------------------------------------------------------------------
# Invite Token Management
# ---------------------------------------------------------------------------

def generate_class_invite(class_id: int, never_expires: bool = False) -> Tuple[Optional[str], str]:
    """Generate or regenerate invite token for a class.
    
    Returns: (token, message/error)
    """
    class_ = Class.query.get(class_id)
    if not class_:
        return None, "Class not found"
    
    # Generate token (7 days expiry or never)
    expires_in_days = 0 if never_expires else 7
    token = class_.generate_join_token(expires_in_days)
    
    try:
        db.session.commit()
        logger.info("Generated invite token for class %s (expires: %s)", 
                    class_.name, "never" if never_expires else "7 days")
        return token, "Invite token generated"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to generate token for class %s: %s", class_id, e)
        return None, f"Failed to generate token: {str(e)}"


def invalidate_class_invite(class_id: int) -> Tuple[bool, str]:
    """Invalidate (disable) the invite token for a class.
    
    Returns: (success, message/error)
    """
    class_ = Class.query.get(class_id)
    if not class_:
        return False, "Class not found"
    
    class_.invalidate_token()
    
    try:
        db.session.commit()
        logger.info("Invalidated invite token for class %s", class_.name)
        return True, "Invite token invalidated"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to invalidate token for class %s: %s", class_id, e)
        return False, f"Failed to invalidate token: {str(e)}"


def join_class_via_token(token: str, user_id: int) -> Tuple[bool, str, Optional[VMAssignment]]:
    """Join a class using an invite token.
    
    Enrolls user in class and auto-assigns an available VM if one exists.
    If no VMs are available, user is still enrolled and can be assigned later.
    
    Returns: (success, message/error, vm_assignment)
    """
    if not token:
        return False, "Token is required", None
    
    class_ = Class.query.filter_by(join_token=token).first()
    if not class_:
        return False, "Invalid invite token", None
    
    if not class_.is_token_valid():
        return False, "Invite token has expired", None
    
    user = User.query.get(user_id)
    if not user:
        return False, "User not found", None
    
    # Check if user is already enrolled
    if user in class_.students:
        # Check if they have a VM assigned
        existing_vm = VMAssignment.query.filter_by(
            class_id=class_.id, 
            assigned_user_id=user_id
        ).first()
        
        if existing_vm:
            return True, "You are already enrolled in this class with a VM assigned", existing_vm
        else:
            return True, "You are already enrolled in this class. Waiting for VM assignment.", None
    
    # Enroll user in class
    class_.students.append(user)
    
    # Try to find an unassigned VM in the class (exclude manually added VMs)
    available_vm = VMAssignment.query.filter_by(
        class_id=class_.id,
        assigned_user_id=None,
        status='available',
        manually_added=False  # Don't auto-assign manually added VMs
    ).first()
    
    if available_vm:
        # Assign the VM to the user
        available_vm.assign_to_user(user)
        
        try:
            db.session.commit()
            logger.info("User %s joined class %s, assigned VM %d", 
                        user.username, class_.name, available_vm.proxmox_vmid)
            return True, f"Successfully joined class {class_.name} with VM assigned", available_vm
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to join class %s for user %s: %s", class_.name, user.username, e)
            return False, f"Failed to join class: {str(e)}", None
    else:
        # No VMs available, but still enroll the user
        try:
            db.session.commit()
            logger.info("User %s joined class %s without VM (none available)", 
                        user.username, class_.name)
            return True, f"Successfully joined class {class_.name}. You'll be assigned a VM when one becomes available.", None
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to join class %s for user %s: %s", class_.name, user.username, e)
            return False, f"Failed to join class: {str(e)}", None


def auto_assign_vms_to_waiting_students(class_id: int) -> int:
    """Auto-assign available VMs to enrolled students who don't have VMs yet.
    
    Returns: Number of students auto-assigned
    """
    class_ = Class.query.get(class_id)
    if not class_:
        logger.warning("Class %d not found for auto-assignment", class_id)
        return 0
    
    # Find students enrolled in class without VM assignments
    students_without_vms = []
    for student in class_.students:
        existing_assignment = VMAssignment.query.filter_by(
            class_id=class_id,
            assigned_user_id=student.id
        ).first()
        
        if not existing_assignment:
            students_without_vms.append(student)
    
    if not students_without_vms:
        logger.info("No students waiting for VMs in class %s", class_.name)
        return 0
    
    # Find available VMs (exclude manually added VMs)
    available_vms = VMAssignment.query.filter_by(
        class_id=class_id,
        assigned_user_id=None,
        status='available',
        is_template_vm=False,
        manually_added=False  # Don't auto-assign manually added VMs
    ).all()
    
    if not available_vms:
        logger.info("No VMs available for auto-assignment in class %s", class_.name)
        return 0
    
    # Assign VMs to students
    assigned_count = 0
    for student, vm in zip(students_without_vms, available_vms):
        vm.assign_to_user(student)
        logger.info("Auto-assigned VM %d to student %s in class %s", 
                    vm.proxmox_vmid, student.username, class_.name)
        assigned_count += 1
    
    try:
        db.session.commit()
        logger.info("Auto-assigned %d VMs to waiting students in class %s", 
                    assigned_count, class_.name)
        return assigned_count
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to auto-assign VMs in class %s: %s", class_.name, e)
        return 0


# ---------------------------------------------------------------------------
# Template Management
# ---------------------------------------------------------------------------

def create_template(name: str, proxmox_vmid: int, cluster_ip: str = '10.220.15.249',
                   node: str = None, created_by_id: int = None, 
                   is_class_template: bool = False, class_id: int = None,
                   original_template_id: int = None) -> Tuple[Optional[Template], str]:
    """Register a Proxmox template in the database.
    
    If node is not provided, will attempt to fetch it from Proxmox.
    If a template with the same cluster_ip/node/vmid already exists, returns the existing one.
    
    Returns: (template, message/error)
    """
    if not name or not proxmox_vmid:
        return None, "Name and Proxmox VMID are required"
    
    # If node not provided, try to fetch it from Proxmox
    if not node:
        try:
            from app.services.proxmox_service import get_proxmox_admin_for_cluster
            from app.config import CLUSTERS
            
            # Find cluster by IP
            cluster_id = None
            for cluster in CLUSTERS:
                if cluster["host"] == cluster_ip:
                    cluster_id = cluster["id"]
                    break
            
            if cluster_id:
                proxmox = get_proxmox_admin_for_cluster(cluster_id)
                # Search all nodes for this VMID
                for node_info in proxmox.nodes.get():
                    node_name = node_info["node"]
                    try:
                        vm = proxmox.nodes(node_name).qemu(proxmox_vmid).status.current.get()
                        if vm:
                            node = node_name
                            logger.info(f"Auto-detected node '{node_name}' for template VMID {proxmox_vmid}")
                            break
                    except Exception:
                        continue
            
            if not node:
                logger.warning(f"Could not auto-detect node for template VMID {proxmox_vmid}")
        except Exception as e:
            logger.warning(f"Failed to auto-detect node for template: {e}")
    
    # Use "get or create" pattern with SAVEPOINT for proper nested transaction handling
    # This prevents race conditions and handles constraint violations gracefully
    
    if not node:
        # Cannot use unique constraint without node - fallback to old behavior
        logger.warning(f"Creating template without node (VMID {proxmox_vmid}) - cannot guarantee uniqueness")
        template = Template(
            name=name,
            proxmox_vmid=proxmox_vmid,
            cluster_ip=cluster_ip,
            node=node,
            created_by_id=created_by_id,
            is_class_template=is_class_template,
            class_id=class_id,
            original_template_id=original_template_id
        )
        try:
            db.session.add(template)
            db.session.commit()
            logger.info(f"Registered template: {name} (VMID: {proxmox_vmid}) - node not detected")
            return template, "Template registered successfully"
        except Exception as e:
            db.session.rollback()
            logger.exception("Failed to register template %s: %s", name, e)
            return None, f"Failed to register template: {str(e)}"
    
    # WITH node: use savepoint to handle constraint violations without poisoning outer transaction
    # Use nested transaction (savepoint) to safely handle IntegrityError
    from sqlalchemy.exc import IntegrityError
    
    # Try using a savepoint (nested transaction) for atomic get-or-create
    try:
        # Create savepoint for nested transaction
        db.session.begin_nested()
        
        template = Template(
            name=name,
            proxmox_vmid=proxmox_vmid,
            cluster_ip=cluster_ip,
            node=node,
            created_by_id=created_by_id,
            is_class_template=is_class_template,
            class_id=class_id,
            original_template_id=original_template_id
        )
        db.session.add(template)
        db.session.commit()  # Commit the nested transaction (savepoint)
        
        logger.info(f"Registered template: {name} (VMID: {proxmox_vmid}) on node {node}")
        
        # Fetch and cache template specs immediately after registration
        _fetch_and_cache_template_specs(template, cluster_ip)
        
        return template, "Template registered successfully"
        
    except IntegrityError as e:
        # Rollback only the nested transaction (savepoint), not the outer one
        db.session.rollback()
        
        # Race condition: another thread created it between our check and insert
        # Query to get the existing record (this query is safe after savepoint rollback)
        existing = Template.query.filter_by(
            cluster_ip=cluster_ip,
            node=node,
            proxmox_vmid=proxmox_vmid
        ).first()
        
        if existing:
            logger.info(f"Template already exists (race condition handled): {existing.name} (VMID: {proxmox_vmid}) on {node}")
            
            # Fetch and cache specs for existing template if not already cached
            if not existing.specs_cached_at:
                _fetch_and_cache_template_specs(existing, cluster_ip)
            
            return existing, "Template already registered"
        else:
            # Shouldn't happen, but log it
            logger.error(f"UNIQUE constraint failed but cannot find existing template: {str(e)}")
            return None, f"Database constraint error: {str(e)}"
            
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to register template %s: %s", name, e)
        return None, f"Failed to register template: {str(e)}"


def get_template_by_id(template_id: int) -> Optional[Template]:
    """Get template by ID."""
    return Template.query.get(template_id)


def get_available_templates(class_id: int = None) -> List[Template]:
    """Get templates available for class creation.
    
    Only shows:
    - Global templates (is_class_template=False)
    - Class-specific templates for the given class_id (if provided)
    """
    query = Template.query.filter(
        (Template.is_class_template.is_(False)) |
        (Template.class_id == class_id)
    )
    return query.order_by(Template.name).all()


def get_global_templates() -> List[Template]:
    """Get all global (non-class-specific) templates."""
    return Template.query.filter_by(is_class_template=False).order_by(Template.name).all()


def delete_template(template_id: int) -> Tuple[bool, str]:
    """Delete a template record (doesn't delete the Proxmox template).
    
    Returns: (success, message/error)
    """
    template = Template.query.get(template_id)
    if not template:
        return False, "Template not found"
    
    # Check if template is in use by any class
    using_classes = Class.query.filter_by(template_id=template_id).count()
    if using_classes > 0:
        return False, f"Template is in use by {using_classes} class(es)"
    
    try:
        db.session.delete(template)
        db.session.commit()
        logger.info("Deleted template: %s", template.name)
        return True, "Template deleted successfully"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to delete template %s: %s", template_id, e)
        return False, f"Failed to delete template: {str(e)}"


# ---------------------------------------------------------------------------
# VM Assignment Management
# ---------------------------------------------------------------------------

def create_vm_assignment(class_id: int, proxmox_vmid: int, node: str = None, is_template_vm: bool = False) -> Tuple[Optional[VMAssignment], str]:
    """Create a VM assignment record for a cloned VM.
    
    Args:
        class_id: Class ID
        proxmox_vmid: VM ID in Proxmox
        node: Proxmox node name
        is_template_vm: True if this is the template reference VM
    
    Returns: (assignment, message/error)
    """
    class_ = Class.query.get(class_id)
    if not class_:
        return None, "Class not found"
    
    # Check if VMID already registered
    existing = VMAssignment.query.filter_by(proxmox_vmid=proxmox_vmid).first()
    if existing:
        return None, f"VM {proxmox_vmid} is already registered"
    
    assignment = VMAssignment(
        class_id=class_id,
        proxmox_vmid=proxmox_vmid,
        node=node,
        status='available',
        is_template_vm=is_template_vm
    )
    
    try:
        db.session.add(assignment)
        db.session.commit()
        logger.info("Created VM assignment: VMID %d for class %s", proxmox_vmid, class_.name)
        return assignment, "VM assignment created"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to create VM assignment: %s", e)
        return None, f"Failed to create VM assignment: {str(e)}"


def assign_vm_to_user(assignment_id: int, user_id: int) -> Tuple[bool, str]:
    """Manually assign a VM to a user.
    
    Returns: (success, message/error)
    """
    assignment = VMAssignment.query.get(assignment_id)
    if not assignment:
        return False, "VM assignment not found"
    
    user = User.query.get(user_id)
    if not user:
        return False, "User not found"
    
    # Check if user already has a VM in this class
    existing = VMAssignment.query.filter_by(
        class_id=assignment.class_id,
        assigned_user_id=user_id
    ).first()
    
    if existing and existing.id != assignment_id:
        return False, "User already has a VM in this class"
    
    assignment.assign_to_user(user)
    
    try:
        db.session.commit()
        logger.info("Assigned VM %d to user %s", assignment.proxmox_vmid, user.username)
        return True, f"VM assigned to {user.username}"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to assign VM %d to user %s: %s", 
                        assignment.proxmox_vmid, user.username, e)
        return False, f"Failed to assign VM: {str(e)}"


def unassign_vm(assignment_id: int) -> Tuple[bool, str]:
    """Remove user from a VM assignment (make it available again).
    
    Returns: (success, message/error)
    """
    assignment = VMAssignment.query.get(assignment_id)
    if not assignment:
        return False, "VM assignment not found"
    
    old_user = assignment.assigned_user.username if assignment.assigned_user else None
    assignment.unassign()
    
    try:
        db.session.commit()
        logger.info("Unassigned VM %d from user %s", assignment.proxmox_vmid, old_user)
        return True, "VM unassigned successfully"
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to unassign VM %d: %s", assignment.proxmox_vmid, e)
        return False, f"Failed to unassign VM: {str(e)}"


def delete_vm_assignment(assignment_id: int) -> Tuple[bool, str, int]:
    """Delete a VM assignment record.
    
    Returns: (success, message/error, proxmox_vmid to delete)
    """
    assignment = VMAssignment.query.get(assignment_id)
    if not assignment:
        return False, "VM assignment not found", 0
    
    vmid = assignment.proxmox_vmid
    
    try:
        db.session.delete(assignment)
        db.session.commit()
        logger.info("Deleted VM assignment: VMID %d", vmid)
        return True, "VM assignment deleted", vmid
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to delete VM assignment %d: %s", assignment_id, e)
        return False, f"Failed to delete VM assignment: {str(e)}", 0


def get_vm_assignments_for_class(class_id: int) -> List[VMAssignment]:
    """Get all VM assignments for a class."""
    return VMAssignment.query.filter_by(class_id=class_id).order_by(VMAssignment.proxmox_vmid).all()


def get_vm_assignments_for_user(user_id: int) -> List[VMAssignment]:
    """Get all VM assignments for a user."""
    return VMAssignment.query.filter_by(assigned_user_id=user_id).order_by(VMAssignment.proxmox_vmid).all()


def get_user_vm_in_class(user_id: int, class_id: int) -> Optional[VMAssignment]:
    """Get the VM assigned to a user in a specific class (if any)."""
    return VMAssignment.query.filter_by(
        class_id=class_id,
        assigned_user_id=user_id
    ).first()


def get_unassigned_vms_in_class(class_id: int) -> List[VMAssignment]:
    """Get all unassigned VMs in a class."""
    return VMAssignment.query.filter_by(
        class_id=class_id,
        assigned_user_id=None,
        status='available'
    ).order_by(VMAssignment.proxmox_vmid).all()


def get_vm_assignment_by_vmid(proxmox_vmid: int) -> Optional[VMAssignment]:
    """Get VM assignment by Proxmox VMID."""
    return VMAssignment.query.filter_by(proxmox_vmid=proxmox_vmid).first()


def _fetch_and_cache_template_specs(template: Template, cluster_ip: str) -> None:
    """Fetch template specs from Proxmox and cache in database.
    
    Args:
        template: Template object to update
        cluster_ip: Proxmox cluster IP
    """
    from datetime import datetime
    
    try:
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        from app.config import CLUSTERS
        
        # Find cluster by IP
        cluster_id = None
        for cluster in CLUSTERS:
            if cluster["host"] == cluster_ip:
                cluster_id = cluster["id"]
                break
        
        if not cluster_id:
            logger.warning(f"Cannot fetch template specs: cluster {cluster_ip} not found")
            return
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Get VM config
        vm_config = proxmox.nodes(template.node).qemu(template.proxmox_vmid).config.get()
        
        # Extract CPU info
        template.cpu_cores = vm_config.get('cores', 1)
        template.cpu_sockets = vm_config.get('sockets', 1)
        
        # Extract memory
        template.memory_mb = vm_config.get('memory', 2048)
        
        # Extract disk info (check scsi0, virtio0, sata0, ide0)
        for disk_key in ['scsi0', 'virtio0', 'sata0', 'ide0']:
            disk_conf = vm_config.get(disk_key)
            if disk_conf:
                # Parse disk config: "local-lvm:vm-100-disk-0,size=32G"
                parts = disk_conf.split(',')
                template.disk_path = parts[0] if parts else disk_conf
                
                # Extract storage ID
                if ':' in template.disk_path:
                    template.disk_storage = template.disk_path.split(':')[0]
                
                # Extract size
                for part in parts:
                    if 'size=' in part:
                        size_str = part.split('=')[1].upper()
                        # Convert to GB
                        if 'G' in size_str:
                            template.disk_size_gb = float(size_str.replace('G', ''))
                        elif 'M' in size_str:
                            template.disk_size_gb = float(size_str.replace('M', '')) / 1024
                        elif 'T' in size_str:
                            template.disk_size_gb = float(size_str.replace('T', '')) * 1024
                
                break
        
        # Detect disk format from storage type
        if template.disk_storage:
            try:
                storage_config = proxmox.storage(template.disk_storage).get()
                storage_type = storage_config.get('type', '')
                if storage_type in ['dir', 'nfs']:
                    template.disk_format = 'qcow2'
                elif storage_type == 'lvm':
                    template.disk_format = 'raw'
                elif storage_type == 'lvmthin':
                    template.disk_format = 'raw'
                else:
                    template.disk_format = storage_type
            except Exception as e:
                logger.debug(f"Could not detect storage type for {template.disk_storage}: {e}")
        
        # Extract network bridge (check net0, net1, net2)
        for net_key in ['net0', 'net1', 'net2']:
            net_conf = vm_config.get(net_key)
            if net_conf and 'bridge=' in net_conf:
                # Parse: "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
                for part in net_conf.split(','):
                    if 'bridge=' in part:
                        template.network_bridge = part.split('=')[1]
                        break
                break
        
        # Extract OS type
        template.os_type = vm_config.get('ostype', 'other')
        
        # Update timestamp
        template.specs_cached_at = datetime.utcnow()
        
        db.session.commit()
        logger.info(f"Cached specs for template {template.name}: {template.cpu_cores} cores, {template.memory_mb} MB RAM, {template.disk_size_gb} GB disk")
        
    except Exception as e:
        logger.warning(f"Failed to fetch template specs for {template.name}: {e}")
        db.session.rollback()

