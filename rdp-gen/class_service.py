#!/usr/bin/env python3
"""
Class management service for lab portal.

Handles class CRUD, template management, VM pool operations, and invite links.
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict, Any

from models import db, User, Class, Template, VMAssignment

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
    if not username or not password:
        return False, "Username and password are required"
    
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


# ---------------------------------------------------------------------------
# Class Management
# ---------------------------------------------------------------------------

def create_class(name: str, teacher_id: int, description: str = None, 
                 template_id: int = None, pool_size: int = 0) -> Tuple[Optional[Class], str]:
    """Create a new class.
    
    Returns: (class, message/error)
    """
    if not name:
        return None, "Class name is required"
    
    teacher = User.query.get(teacher_id)
    if not teacher:
        return None, "Teacher not found"
    
    if not teacher.is_teacher and not teacher.is_adminer:
        return None, "User must be a teacher or adminer to create classes"
    
    # Verify template exists if specified
    if template_id:
        template = Template.query.get(template_id)
        if not template:
            return None, "Template not found"
    
    class_ = Class(
        name=name,
        description=description,
        teacher_id=teacher_id,
        template_id=template_id,
        pool_size=pool_size
    )
    
    try:
        db.session.add(class_)
        db.session.commit()
        logger.info("Created class: %s by teacher %s", name, teacher.username)
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
    """Get all classes taught by a teacher."""
    return Class.query.filter_by(teacher_id=teacher_id).order_by(Class.created_at.desc()).all()


def get_classes_for_student(user_id: int) -> List[Class]:
    """Get all classes a student is enrolled in (has a VM assignment)."""
    assignments = VMAssignment.query.filter_by(assigned_user_id=user_id).all()
    class_ids = set(a.class_id for a in assignments)
    return Class.query.filter(Class.id.in_(class_ids)).order_by(Class.name).all()


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
    
    Auto-assigns an available VM to the user if one exists.
    
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
    
    # Check if user already has a VM in this class
    existing = VMAssignment.query.filter_by(
        class_id=class_.id, 
        assigned_user_id=user_id
    ).first()
    
    if existing:
        return True, "You are already enrolled in this class", existing
    
    # Find an unassigned VM in the class
    available_vm = VMAssignment.query.filter_by(
        class_id=class_.id,
        assigned_user_id=None,
        status='available'
    ).first()
    
    if not available_vm:
        return False, "No available VMs in this class. Contact your teacher.", None
    
    # Assign the VM to the user
    available_vm.assign_to_user(user)
    
    try:
        db.session.commit()
        logger.info("User %s joined class %s, assigned VM %d", 
                    user.username, class_.name, available_vm.proxmox_vmid)
        return True, f"Successfully joined class {class_.name}", available_vm
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to join class %s for user %s: %s", class_.name, user.username, e)
        return False, f"Failed to join class: {str(e)}", None


# ---------------------------------------------------------------------------
# Template Management
# ---------------------------------------------------------------------------

def create_template(name: str, proxmox_vmid: int, cluster_ip: str = '10.220.15.249',
                   node: str = None, created_by_id: int = None, 
                   is_class_template: bool = False, class_id: int = None) -> Tuple[Optional[Template], str]:
    """Register a Proxmox template in the database.
    
    Returns: (template, message/error)
    """
    if not name or not proxmox_vmid:
        return None, "Name and Proxmox VMID are required"
    
    template = Template(
        name=name,
        proxmox_vmid=proxmox_vmid,
        cluster_ip=cluster_ip,
        node=node,
        created_by_id=created_by_id,
        is_class_template=is_class_template,
        class_id=class_id
    )
    
    try:
        db.session.add(template)
        db.session.commit()
        logger.info("Registered template: %s (VMID: %d)", name, proxmox_vmid)
        return template, "Template registered successfully"
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
        (Template.is_class_template == False) |
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

def create_vm_assignment(class_id: int, proxmox_vmid: int, node: str = None) -> Tuple[Optional[VMAssignment], str]:
    """Create a VM assignment record for a cloned VM.
    
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
        status='available'
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
