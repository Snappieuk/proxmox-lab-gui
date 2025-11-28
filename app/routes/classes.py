#!/usr/bin/env python3
"""
Classes page routes blueprint.

Handles class management UI pages for teachers and students.
"""

import logging

from flask import Blueprint, render_template, redirect, url_for, request, session, flash

from app.utils.decorators import login_required

# Import path setup is no longer needed
import app.utils.paths

from app.services.class_service import (
    get_class_by_id,
    get_class_by_token,
    get_classes_for_teacher,
    get_classes_for_student,
    list_all_classes,
    get_user_by_username,
    get_vm_assignments_for_class,
    get_user_vm_in_class,
    join_class_via_token,
)

logger = logging.getLogger(__name__)

classes_bp = Blueprint('classes', __name__, url_prefix='/classes')


def get_current_user():
    """Get current logged-in user from database, or create admin user if needed."""
    username = session.get('user')
    if not username:
        return None
    
    # Remove @pve suffix if present for local user lookup
    if '@' in username:
        username_clean = username.split('@')[0]
    else:
        username_clean = username
    
    user = get_user_by_username(username_clean)
    
    # If no local user exists but they're a Proxmox admin, auto-create admin account
    if not user:
        from app.services.user_manager import is_admin_user
        from app.services.class_service import create_local_user
        
        if is_admin_user(session.get('user')):
            # Auto-create local admin account for Proxmox admins
            logger.info(f"Auto-creating local admin account for Proxmox admin: {username_clean}")
            # Use a placeholder password since admins authenticate via Proxmox
            success, msg = create_local_user(username_clean, 'proxmox-admin-placeholder', role='adminer')
            if success:
                user = get_user_by_username(username_clean)
                logger.info(f"Successfully created local admin account for: {username_clean}")
            else:
                logger.error(f"Failed to create local admin account: {msg}")
    
    return user


@classes_bp.route("")
@login_required
def classes_list():
    """List all classes accessible to current user."""
    user = get_current_user()
    
    if not user:
        # User doesn't have a local account yet
        return render_template(
            "classes/list.html",
            classes=[],
            user_role=None,
            message="Create a local account to access classes."
        )
    
    if user.is_adminer:
        classes = list_all_classes()
    elif user.is_teacher:
        classes = get_classes_for_teacher(user.id)
    else:
        classes = get_classes_for_student(user.id)
    
    return render_template(
        "classes/list.html",
        classes=[c.to_dict() for c in classes],
        user_role=user.role,
        user=user
    )


@classes_bp.route("/create", methods=["GET"])
@login_required
def create_class():
    """Create new class form (teacher/adminer only)."""
    user = get_current_user()
    
    if not user or (not user.is_teacher and not user.is_adminer):
        flash("You need teacher or admin privileges to create classes.", "error")
        return redirect(url_for('classes.classes_list'))
    
    return render_template(
        "classes/create.html",
        user_role=user.role,
        user=user
    )


@classes_bp.route("/<int:class_id>")
@login_required
def view_class(class_id: int):
    """View class details."""
    user = get_current_user()
    class_ = get_class_by_id(class_id)
    
    if not class_:
        flash("Class not found.", "error")
        return redirect(url_for('classes.classes_list'))
    
    # Check access
    can_manage = False
    if user:
        if user.is_adminer:
            can_manage = True
        elif user.is_teacher and class_.teacher_id == user.id:
            can_manage = True
        elif not user.is_teacher:
            # Student - check if they have a VM
            vm = get_user_vm_in_class(user.id, class_id)
            if not vm:
                flash("You don't have access to this class.", "error")
                return redirect(url_for('classes.classes_list'))
    
    # Get VM assignments for this class
    if can_manage:
        vm_assignments = get_vm_assignments_for_class(class_id)
    else:
        # Student only sees their own VM
        student_vm = get_user_vm_in_class(user.id if user else 0, class_id)
        vm_assignments = [student_vm] if student_vm else []
    
    return render_template(
        "classes/view.html",
        class_=class_.to_dict(),
        vm_assignments=[v.to_dict() for v in vm_assignments] if vm_assignments else [],
        can_manage=can_manage,
        user_role=user.role if user else 'user',
        user=user
    )


@classes_bp.route("/join/<token>")
@login_required
def join_class_page(token: str):
    """Join class via invite link."""
    user = get_current_user()
    
    if not user:
        flash("You need a local account to join classes. Please register first.", "error")
        return redirect(url_for('auth.register'))
    
    # Check if token is valid
    class_ = get_class_by_token(token)
    if not class_:
        flash("Invalid or expired invite link.", "error")
        return redirect(url_for('classes.classes_list'))
    
    if not class_.is_token_valid():
        flash("This invite link has expired.", "error")
        return redirect(url_for('classes.classes_list'))
    
    # Check if user already has a VM in this class
    existing_vm = get_user_vm_in_class(user.id, class_.id)
    if existing_vm:
        flash(f"You are already enrolled in {class_.name}!", "info")
        return redirect(url_for('classes.view_class', class_id=class_.id))
    
    # Show join confirmation page
    return render_template(
        "classes/join.html",
        class_=class_.to_dict(),
        token=token,
        user=user
    )


@classes_bp.route("/join/<token>/confirm", methods=["POST"])
@login_required
def confirm_join_class(token: str):
    """Confirm joining a class via invite link."""
    user = get_current_user()
    
    if not user:
        flash("You need a local account to join classes.", "error")
        return redirect(url_for('auth.register'))
    
    success, msg, assignment = join_class_via_token(token, user.id)
    
    if success:
        flash(msg, "success")
        if assignment:
            return redirect(url_for('classes.view_class', class_id=assignment.class_id))
    else:
        flash(msg, "error")
    
    return redirect(url_for('classes.classes_list'))
