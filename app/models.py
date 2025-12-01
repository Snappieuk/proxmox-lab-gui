#!/usr/bin/env python3
"""
Database models for class-based lab management system.

Schema:
- User: Local users with roles (adminer/teacher/user)
- Class: Lab classes created by teachers, linked to templates
- Template: Proxmox VM templates, can be cluster-wide or class-specific
- VMAssignment: VMs cloned for classes, assigned to users
- Token: Invite links for joining classes

All tables use SQLite via SQLAlchemy.
"""

import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from werkzeug.security import generate_password_hash, check_password_hash

# Initialize SQLAlchemy (will be bound to Flask app in create_app)
db = SQLAlchemy()


# Association table for class enrollments (many-to-many)
class_enrollments = db.Table('class_enrollments',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('class_id', db.Integer, db.ForeignKey('classes.id'), primary_key=True),
    db.Column('enrolled_at', db.DateTime, default=datetime.utcnow)
)

# Association table for class co-owners (many-to-many)
class_co_owners = db.Table('class_co_owners',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('class_id', db.Integer, db.ForeignKey('classes.id'), primary_key=True),
    db.Column('added_at', db.DateTime, default=datetime.utcnow)
)


class User(db.Model):
    """Local user account with role-based access."""
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')  # adminer, teacher, user
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    taught_classes = db.relationship('Class', back_populates='teacher', lazy='dynamic')
    created_templates = db.relationship('Template', back_populates='created_by', lazy='dynamic')
    vm_assignments = db.relationship('VMAssignment', back_populates='assigned_user', lazy='dynamic')
    enrolled_classes = db.relationship('Class', secondary=class_enrollments, 
                                      backref=db.backref('students', lazy='dynamic'))
    co_owned_classes = db.relationship('Class', secondary=class_co_owners,
                                       backref=db.backref('co_owners', lazy='dynamic'))
    
    def set_password(self, password: str) -> None:
        """Hash and store password."""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password: str) -> bool:
        """Verify password against stored hash."""
        return check_password_hash(self.password_hash, password)
    
    @property
    def is_adminer(self) -> bool:
        return self.role == 'adminer'
    
    @property
    def is_teacher(self) -> bool:
        return self.role == 'teacher'
    
    @property
    def is_user(self) -> bool:
        return self.role == 'user'
    
    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
    
    def __repr__(self):
        return f'<User {self.username} ({self.role})>'


class Class(db.Model):
    """Lab class managed by a teacher."""
    __tablename__ = 'classes'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey('templates.id'), nullable=True)
    join_token = db.Column(db.String(64), unique=True, nullable=True, index=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    token_never_expires = db.Column(db.Boolean, default=False)
    pool_size = db.Column(db.Integer, default=0)  # Target number of VMs in pool
    clone_task_id = db.Column(db.String(64), nullable=True)  # Track ongoing class creation
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    teacher = db.relationship('User', back_populates='taught_classes')
    # Use foreign_keys to disambiguate the relationship since there are two FKs between Class and Template
    template = db.relationship('Template', foreign_keys=[template_id], backref='using_classes')
    vm_assignments = db.relationship('VMAssignment', back_populates='class_', cascade='all, delete-orphan')
    
    def generate_join_token(self, expires_in_days: int = 7) -> str:
        """Generate a new join token for this class."""
        self.join_token = secrets.token_urlsafe(32)
        if expires_in_days > 0:
            self.token_expires_at = datetime.utcnow() + timedelta(days=expires_in_days)
            self.token_never_expires = False
        else:
            self.token_expires_at = None
            self.token_never_expires = True
        return self.join_token
    
    def is_token_valid(self) -> bool:
        """Check if the join token is still valid."""
        if not self.join_token:
            return False
        if self.token_never_expires:
            return True
        if self.token_expires_at and datetime.utcnow() > self.token_expires_at:
            return False
        return True
    
    def invalidate_token(self) -> None:
        """Invalidate the current join token."""
        self.join_token = None
        self.token_expires_at = None
        self.token_never_expires = False
    
    @property
    def assigned_count(self) -> int:
        """Number of VMs assigned to users."""
        return sum(1 for vm in self.vm_assignments if vm.assigned_user_id is not None)
    
    @property
    def unassigned_count(self) -> int:
        """Number of VMs not assigned to any user."""
        return sum(1 for vm in self.vm_assignments if vm.assigned_user_id is None)
    
    @property
    def enrolled_count(self) -> int:
        """Number of students enrolled in this class."""
        return self.students.count()
    
    def is_owner(self, user) -> bool:
        """Check if user is an owner (primary teacher or co-owner) of this class."""
        if not user:
            return False
        # Check if primary teacher
        if self.teacher_id == user.id:
            return True
        # Check if co-owner
        return user in self.co_owners.all()
    
    def to_dict(self) -> dict:
        # Get enrolled students with their VM assignments
        students_list = []
        for student in self.students:
            # Find VM assigned to this student in this class
            vm_assignment = VMAssignment.query.filter_by(
                class_id=self.id,
                assigned_user_id=student.id
            ).first()
            
            students_list.append({
                'id': student.id,
                'username': student.username,
                'vmid': vm_assignment.proxmox_vmid if vm_assignment else None,
                'vm_status': vm_assignment.status if vm_assignment else None
            })
        
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'teacher_id': self.teacher_id,
            'teacher_name': self.teacher.username if self.teacher else None,
            'co_owners': [{'id': co.id, 'username': co.username} for co in self.co_owners.all()],
            'template_id': self.template_id,
            'template_name': self.template.name if self.template else None,
            'template_original_name': self.template.original_template.name if (self.template and getattr(self.template, 'original_template', None)) else None,
            'join_token': self.join_token,
            'token_valid': self.is_token_valid(),
            'token_never_expires': self.token_never_expires,
            'token_expires_at': self.token_expires_at.isoformat() if self.token_expires_at else None,
            'pool_size': self.pool_size,
            'assigned_count': self.assigned_count,
            'unassigned_count': self.unassigned_count,
            'enrolled_count': self.enrolled_count,
            'students': students_list,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
    
    def __repr__(self):
        return f'<Class {self.name}>'


class Template(db.Model):
    """Proxmox VM template reference."""
    __tablename__ = 'templates'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    proxmox_vmid = db.Column(db.Integer, nullable=False, index=True)  # Proxmox template VMID
    cluster_ip = db.Column(db.String(45), default='10.220.15.249', index=True)  # Proxmox cluster IP
    node = db.Column(db.String(80), nullable=True, index=True)  # Proxmox node name
    is_replica = db.Column(db.Boolean, default=False, index=True)  # True if replicated from another node
    source_vmid = db.Column(db.Integer, nullable=True)  # Original template VMID if this is a replica
    source_node = db.Column(db.String(80), nullable=True)  # Original template node if this is a replica
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    is_class_template = db.Column(db.Boolean, default=False)  # True if created for a specific class
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=True)  # Scoped to class if set
    original_template_id = db.Column(db.Integer, db.ForeignKey('templates.id'), nullable=True)  # Track source template
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_verified_at = db.Column(db.DateTime, nullable=True)  # Last time we confirmed this template exists in Proxmox
    
    # Relationships
    created_by = db.relationship('User', back_populates='created_templates')
    # Relationship to the class that owns this template (for class-specific templates)
    owning_class = db.relationship('Class', foreign_keys=[class_id], backref='owned_templates')
    original_template = db.relationship('Template', remote_side=[id], uselist=False)
    
    __table_args__ = (
        db.UniqueConstraint('cluster_ip', 'node', 'proxmox_vmid', name='uix_cluster_node_vmid'),
    )
    
    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'name': self.name,
            'proxmox_vmid': self.proxmox_vmid,
            'cluster_ip': self.cluster_ip,
            'node': self.node,
            'is_replica': self.is_replica,
            'source_vmid': self.source_vmid,
            'source_node': self.source_node,
            'created_by_id': self.created_by_id,
            'created_by_name': self.created_by.username if self.created_by else None,
            'is_class_template': self.is_class_template,
            'class_id': self.class_id,
            'original_template_id': self.original_template_id,
            'original_template_name': self.original_template.name if getattr(self, 'original_template', None) else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_verified_at': self.last_verified_at.isoformat() if self.last_verified_at else None,
        }
    
    def __repr__(self):
        replica_tag = " (replica)" if self.is_replica else ""
        return f'<Template {self.name} (VMID: {self.proxmox_vmid}, Node: {self.node}){replica_tag}>'


class VMAssignment(db.Model):
    """VM cloned for a class, optionally assigned to a user."""
    __tablename__ = 'vm_assignments'
    
    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=False, index=True)
    proxmox_vmid = db.Column(db.Integer, nullable=False, index=True)  # Cloned VM's VMID in Proxmox
    mac_address = db.Column(db.String(17), nullable=True, index=True)  # VM's MAC address (e.g., "AA:BB:CC:DD:EE:FF")
    cached_ip = db.Column(db.String(45), nullable=True)  # Cached IP address (IPv4 or IPv6)
    ip_updated_at = db.Column(db.DateTime, nullable=True)  # When IP was last updated
    node = db.Column(db.String(80), nullable=True)  # Proxmox node where VM resides
    assigned_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    status = db.Column(db.String(20), default='available')  # available, assigned, deleting
    is_template_vm = db.Column(db.Boolean, default=False)  # True if this is the template reference VM
    manually_added = db.Column(db.Boolean, default=False)  # True if VM was manually added (don't auto-assign)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    assigned_at = db.Column(db.DateTime, nullable=True)
    
    # Relationships
    class_ = db.relationship('Class', back_populates='vm_assignments')
    assigned_user = db.relationship('User', back_populates='vm_assignments')
    
    def assign_to_user(self, user: User) -> None:
        """Assign this VM to a user."""
        self.assigned_user_id = user.id
        self.assigned_at = datetime.utcnow()
        self.status = 'assigned'
    
    def unassign(self) -> None:
        """Remove user assignment, making VM available again."""
        self.assigned_user_id = None
        self.assigned_at = None
        self.status = 'available'
    
    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'class_id': self.class_id,
            'class_name': self.class_.name if self.class_ else None,
            'proxmox_vmid': self.proxmox_vmid,
            'mac_address': self.mac_address,
            'cached_ip': self.cached_ip,
            'ip_updated_at': self.ip_updated_at.isoformat() if self.ip_updated_at else None,
            'node': self.node,
            'assigned_user_id': self.assigned_user_id,
            'assigned_user_name': self.assigned_user.username if self.assigned_user else None,
            'status': self.status,
            'is_template_vm': self.is_template_vm,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'assigned_at': self.assigned_at.isoformat() if self.assigned_at else None,
        }
    
    def __repr__(self):
        return f'<VMAssignment {self.proxmox_vmid} -> {self.assigned_user.username if self.assigned_user else "unassigned"}>'


class VMIPCache(db.Model):
    """IP address cache for VMs (legacy mappings.json VMs)."""
    __tablename__ = 'vm_ip_cache'
    
    vmid = db.Column(db.Integer, primary_key=True, index=True)
    mac_address = db.Column(db.String(17), nullable=True, index=True)
    cached_ip = db.Column(db.String(45), nullable=True)
    ip_updated_at = db.Column(db.DateTime, nullable=True)
    cluster_id = db.Column(db.String(50), nullable=True)
    
    def __repr__(self):
        return f'<VMIPCache vmid={self.vmid} ip={self.cached_ip}>'


class VMInventory(db.Model):
    """
    Complete VM inventory synchronized from Proxmox.
    
    This is the SINGLE SOURCE OF TRUTH for all VM data shown in the GUI.
    Background sync service keeps this table current.
    All frontend queries read from here - NEVER directly from Proxmox API.
    
    Benefits:
    - Fast queries (<100ms vs 5-30s Proxmox API)
    - Consistent data view
    - Enables complex filtering/sorting
    - Reduces Proxmox API load
    """
    __tablename__ = 'vm_inventory'
    
    # Primary key
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    
    # Identity (unique per cluster)
    cluster_id = db.Column(db.String(50), nullable=False, index=True)
    vmid = db.Column(db.Integer, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    node = db.Column(db.String(80), nullable=False, index=True)
    
    # Status & type
    status = db.Column(db.String(20), nullable=False, default='unknown', index=True)  # running, stopped, unknown
    type = db.Column(db.String(10), nullable=False, default='qemu')  # qemu or lxc
    category = db.Column(db.String(50), nullable=True)  # lab, template, production, etc.
    
    # Network
    ip = db.Column(db.String(45), nullable=True, index=True)  # IPv4 or IPv6
    mac_address = db.Column(db.String(17), nullable=True)  # Used for ARP discovery
    
    # Resources
    memory = db.Column(db.BigInteger, nullable=True)  # Memory in bytes
    cores = db.Column(db.Integer, nullable=True)  # CPU cores
    disk_size = db.Column(db.BigInteger, nullable=True)  # Disk size in bytes
    
    # Runtime metrics
    uptime = db.Column(db.Integer, nullable=True)  # Uptime in seconds
    cpu_usage = db.Column(db.Float, nullable=True)  # CPU usage percentage
    memory_usage = db.Column(db.Float, nullable=True)  # Memory usage percentage
    
    # Metadata
    is_template = db.Column(db.Boolean, default=False, index=True)
    tags = db.Column(db.Text, nullable=True)  # Comma-separated tags
    
    # Remote access
    rdp_available = db.Column(db.Boolean, default=False)  # RDP port 3389 open
    ssh_available = db.Column(db.Boolean, default=False)  # SSH port 22 open
    
    # Sync tracking
    last_updated = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    last_status_check = db.Column(db.DateTime, nullable=True)  # Last quick status check
    sync_error = db.Column(db.Text, nullable=True)  # Last error message if sync failed
    
    # Unique constraint: one record per (cluster, vmid)
    __table_args__ = (
        db.UniqueConstraint('cluster_id', 'vmid', name='uix_cluster_vmid'),
        db.Index('idx_cluster_status', 'cluster_id', 'status'),
        db.Index('idx_cluster_template', 'cluster_id', 'is_template'),
    )
    
    def to_dict(self) -> dict:
        """Convert to dict for API responses."""
        return {
            'id': self.id,
            'cluster_id': self.cluster_id,
            'vmid': self.vmid,
            'name': self.name,
            'node': self.node,
            'status': self.status,
            'type': self.type,
            'category': self.category,
            'ip': self.ip,
            'mac_address': self.mac_address,
            'memory': self.memory,
            'cores': self.cores,
            'disk_size': self.disk_size,
            'uptime': self.uptime,
            'cpu_usage': self.cpu_usage,
            'memory_usage': self.memory_usage,
            'is_template': self.is_template,
            'tags': self.tags.split(',') if self.tags else [],
            'rdp_available': self.rdp_available,
            'ssh_available': self.ssh_available,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
            'last_status_check': self.last_status_check.isoformat() if self.last_status_check else None,
            'sync_error': self.sync_error,
        }
    
    def __repr__(self):
        return f'<VMInventory {self.cluster_id}:{self.vmid} {self.name} [{self.status}]>'


def init_db(app):
    """Initialize database with Flask app context.
    
    Call this in create_app() to set up the database.
    """
    # Set database URI if not already configured
    if 'SQLALCHEMY_DATABASE_URI' not in app.config:
        db_path = os.path.join(os.path.dirname(__file__), 'lab_portal.db')
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
        # Improve SQLite concurrency: allow cross-thread access and increase lock timeout
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'connect_args': {
                'check_same_thread': False,
                'timeout': 15,
            }
        }
    
    # Disable modification tracking (not needed and impacts performance)
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    # Initialize SQLAlchemy with the app
    db.init_app(app)
    
    # Create tables if they don't exist
    with app.app_context():
        # Enable WAL journal mode to reduce write-lock contention
        try:
            db.session.execute("PRAGMA journal_mode=WAL;")
            db.session.execute("PRAGMA busy_timeout=15000;")
            db.session.commit()
        except Exception:
            db.session.rollback()
        db.create_all()
