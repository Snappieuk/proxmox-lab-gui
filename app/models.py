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
from datetime import datetime, timedelta
from typing import Optional

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

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


class Cluster(db.Model):
    """Proxmox cluster connection configuration."""
    __tablename__ = 'clusters'
    
    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.String(50), unique=True, nullable=False, index=True)  # Internal identifier
    name = db.Column(db.String(120), nullable=False)  # Display name
    host = db.Column(db.String(255), nullable=False)  # IP address or hostname
    port = db.Column(db.Integer, default=8006)  # Proxmox API port
    user = db.Column(db.String(80), nullable=False)  # Proxmox user (e.g., root@pam)
    password = db.Column(db.String(256), nullable=False)  # Proxmox password
    verify_ssl = db.Column(db.Boolean, default=False)  # SSL certificate verification
    is_default = db.Column(db.Boolean, default=False)  # Default cluster for new users
    is_active = db.Column(db.Boolean, default=True)  # Enable/disable cluster
    
    # Deployment and management options
    allow_vm_deployment = db.Column(db.Boolean, default=True)  # Allow creating VMs on this cluster
    allow_template_sync = db.Column(db.Boolean, default=True)  # Sync templates from this cluster
    allow_iso_sync = db.Column(db.Boolean, default=True)  # Sync ISO images from this cluster
    auto_shutdown_enabled = db.Column(db.Boolean, default=False)  # Enable auto-shutdown monitoring for this cluster
    priority = db.Column(db.Integer, default=50)  # Deployment priority (higher = preferred), 0-100
    
    # Storage configuration
    default_storage = db.Column(db.String(100), nullable=True)  # Default storage pool for VMs (e.g., 'local-lvm')
    template_storage = db.Column(db.String(100), nullable=True)  # Storage for template exports
    iso_storage = db.Column(db.String(100), nullable=True)  # Storage pool for ISO images (e.g., 'local')
    qcow2_template_path = db.Column(db.String(512), nullable=True)  # Path for QCOW2 templates (e.g., '/mnt/pve/TRUENAS-NFS/images')
    qcow2_images_path = db.Column(db.String(512), nullable=True)  # Path for VM overlay images
    
    # Admin configuration (per-cluster admin groups and users)
    admin_group = db.Column(db.String(100), nullable=True)  # Proxmox group for admins (e.g., 'adminers')
    admin_users = db.Column(db.Text, nullable=True)  # Comma-separated list of admin usernames
    
    # IP discovery settings (per-cluster ARP subnets)
    arp_subnets = db.Column(db.Text, nullable=True)  # Comma-separated broadcast addresses (e.g., '10.220.15.255,192.168.1.255')
    
    # Cache settings (per-cluster overrides)
    vm_cache_ttl = db.Column(db.Integer, nullable=True)  # VM cache TTL in seconds (NULL = use global default)
    enable_ip_lookup = db.Column(db.Boolean, default=True)  # Enable IP lookup via guest agent
    enable_ip_persistence = db.Column(db.Boolean, default=False)  # Save IPs to Proxmox descriptions
    
    # Metadata
    description = db.Column(db.Text, nullable=True)  # Optional description/notes
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self) -> dict:
        """Convert to dictionary format (compatible with config.py CLUSTERS format)."""
        return {
            'id': self.cluster_id,
            'name': self.name,
            'host': self.host,
            'port': self.port,
            'user': self.user,
            'password': self.password,  # ⚠️ Sensitive - don't expose in API responses
            'verify_ssl': self.verify_ssl,
            'is_default': self.is_default,
            'is_active': self.is_active,
            'allow_vm_deployment': self.allow_vm_deployment,
            'allow_template_sync': self.allow_template_sync,
            'allow_iso_sync': self.allow_iso_sync,
            'auto_shutdown_enabled': self.auto_shutdown_enabled,
            'priority': self.priority,
            'default_storage': self.default_storage,
            'template_storage': self.template_storage,
            'qcow2_template_path': self.qcow2_template_path,
            'qcow2_images_path': self.qcow2_images_path,
            'admin_group': self.admin_group,
            'admin_users': self.admin_users,
            'arp_subnets': self.arp_subnets,
            'vm_cache_ttl': self.vm_cache_ttl,
            'enable_ip_lookup': self.enable_ip_lookup,
            'enable_ip_persistence': self.enable_ip_persistence,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
    
    def to_safe_dict(self) -> dict:
        """Convert to dictionary without sensitive data."""
        return {
            'id': self.cluster_id,
            'db_id': self.id,  # Include numeric database ID
            'name': self.name,
            'host': self.host,
            'port': self.port,
            'user': self.user,
            'verify_ssl': self.verify_ssl,
            'is_default': self.is_default,
            'is_active': self.is_active,
            'allow_vm_deployment': self.allow_vm_deployment,
            'allow_template_sync': self.allow_template_sync,
            'allow_iso_sync': self.allow_iso_sync,
            'auto_shutdown_enabled': self.auto_shutdown_enabled,
            'priority': self.priority,
            'default_storage': self.default_storage,
            'template_storage': self.template_storage,
            'qcow2_template_path': self.qcow2_template_path,
            'qcow2_images_path': self.qcow2_images_path,
            'admin_group': self.admin_group,
            'admin_users': self.admin_users,
            'arp_subnets': self.arp_subnets,
            'vm_cache_ttl': self.vm_cache_ttl,
            'enable_ip_lookup': self.enable_ip_lookup,
            'enable_ip_persistence': self.enable_ip_persistence,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
    
    def __repr__(self):
        return f'<Cluster {self.name} ({self.host})>'


class SystemSettings(db.Model):
    """Global system settings stored in database."""
    __tablename__ = 'system_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Settings categories
    # Cache settings: vm_cache_ttl, proxmox_cache_ttl, db_ip_cache_ttl
    # IP discovery: enable_ip_lookup, enable_ip_persistence, arp_subnets
    # Admin access: admin_users, admin_group
    # Storage: qcow2_template_path, qcow2_images_path
    # Features: enable_template_replication
    
    @staticmethod
    def get(key, default=None):
        """Get setting value by key."""
        setting = SystemSettings.query.filter_by(key=key).first()
        return setting.value if setting else default
    
    @staticmethod
    def set(key, value, description=None):
        """Set setting value by key."""
        setting = SystemSettings.query.filter_by(key=key).first()
        if setting:
            setting.value = value
            if description:
                setting.description = description
            setting.updated_at = datetime.utcnow()
        else:
            setting = SystemSettings(key=key, value=value, description=description)
            db.session.add(setting)
        return setting
    
    def __repr__(self):
        return f'<SystemSettings {self.key}={self.value}>'


class ISOImage(db.Model):
    """ISO image cache for fast lookups."""
    __tablename__ = 'iso_images'
    
    id = db.Column(db.Integer, primary_key=True)
    volid = db.Column(db.String(255), unique=True, nullable=False, index=True)  # e.g., "local:iso/ubuntu-22.04.iso"
    name = db.Column(db.String(255), nullable=False, index=True)  # Filename
    size = db.Column(db.BigInteger, nullable=False)  # Size in bytes
    node = db.Column(db.String(50), nullable=False, index=True)  # Node where ISO exists
    storage = db.Column(db.String(50), nullable=False, index=True)  # Storage name
    cluster_id = db.Column(db.String(50), nullable=False, index=True)  # Cluster identifier
    
    # Timestamps
    discovered_at = db.Column(db.DateTime, default=datetime.utcnow)  # First time found
    last_seen = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)  # Last sync
    
    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            'volid': self.volid,
            'name': self.name,
            'size': self.size,
            'node': self.node,
            'storage': self.storage,
            'cluster_id': self.cluster_id,
            'discovered_at': self.discovered_at.isoformat() if self.discovered_at else None,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
        }
    
    def __repr__(self):
        return f'<ISOImage {self.name} on {self.node}:{self.storage}>'


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
    join_token = db.Column(db.String(128), unique=True, nullable=True, index=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    token_never_expires = db.Column(db.Boolean, default=False)
    pool_size = db.Column(db.Integer, default=0)  # Target number of VMs in pool
    cpu_cores = db.Column(db.Integer, default=2)  # CPU cores per VM
    memory_mb = db.Column(db.Integer, default=2048)  # RAM in MB per VM
    disk_size_gb = db.Column(db.Integer, default=32)  # Disk size in GB (for template-less classes)
    vmid_prefix = db.Column(db.Integer, nullable=True)  # 3-digit prefix for VMIDs (e.g., 123 -> VMs 12300-12399)
    clone_task_id = db.Column(db.String(64), nullable=True)  # Track ongoing class creation
    deployment_node = db.Column(db.String(80), nullable=True)  # Override: deploy all VMs to this specific node
    deployment_cluster = db.Column(db.String(50), nullable=True)  # Cluster ID where VMs are deployed
    deployment_method = db.Column(db.String(20), default='config_clone', nullable=False)  # "config_clone" or "linked_clone"
    
    # Auto-shutdown settings
    auto_shutdown_enabled = db.Column(db.Boolean, default=False)  # Enable auto-shutdown
    auto_shutdown_cpu_threshold = db.Column(db.Integer, default=20)  # CPU % threshold (default 20%)
    auto_shutdown_idle_minutes = db.Column(db.Integer, default=30)  # Minutes of idle time before shutdown (default 30)
    
    # Available hours settings
    restrict_hours = db.Column(db.Boolean, default=False)  # Enable hour restrictions
    hours_start = db.Column(db.Integer, default=0)  # Start hour (0-23)
    hours_end = db.Column(db.Integer, default=23)  # End hour (0-23)
    max_usage_hours = db.Column(db.Integer, default=0)  # Max cumulative hours students can use VM (0=unlimited)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    teacher = db.relationship('User', back_populates='taught_classes')
    # Use foreign_keys to disambiguate the relationship since there are two FKs between Class and Template
    template = db.relationship('Template', foreign_keys=[template_id], backref='using_classes')
    vm_assignments = db.relationship('VMAssignment', back_populates='class_', cascade='all, delete-orphan')
    
    def generate_join_token(self, expires_in_days: int = 7) -> str:
        """Generate a new join token for this class.
        
        Token format: classname-vmidprefix (e.g., 'python101-230' or 'windows-server-450')
        """
        # Sanitize class name: lowercase, replace spaces/special chars with hyphens
        import re
        sanitized_name = re.sub(r'[^a-z0-9]+', '-', self.name.lower()).strip('-')
        
        # Use VMID prefix if available, otherwise use class ID
        identifier = str(self.vmid_prefix) if self.vmid_prefix else str(self.id)
        
        self.join_token = f"{sanitized_name}-{identifier}"
        
        if expires_in_days > 0:
            self.token_expires_at = datetime.utcnow() + timedelta(days=expires_in_days)
            self.token_never_expires = False
        else:
            self.token_expires_at = None
            self.token_never_expires = True
        return self.join_token
    
    def is_token_valid(self) -> bool:
        """Check if the join token is still valid."""
        # No token = invalid
        if not self.join_token:
            return False
        
        # Token exists. Check if it's explicitly marked to never expire
        # Handle both Boolean True and integer 1 from database
        if self.token_never_expires is True or self.token_never_expires == 1:
            return True
        
        # Check if token has an expiration date
        if self.token_expires_at:
            # Token has expiration date - check if it's passed
            if datetime.utcnow() > self.token_expires_at:
                return False
            # Token hasn't expired yet
            return True
        
        # Token exists but has no expiration info
        # This happens with old classes before token_never_expires field was added
        # Safe to treat as valid (likely never expires)
        return True
    
    def invalidate_token(self) -> None:
        """Invalidate the current join token."""
        self.join_token = None
        self.token_expires_at = None
        self.token_never_expires = False
    
    @property
    def assigned_count(self) -> int:
        """Number of student VMs assigned to users (excludes teacher and template VMs).
        
        Queries database directly to ensure fresh count after manual assignment changes.
        """
        return VMAssignment.query.filter(
            VMAssignment.class_id == self.id,
            VMAssignment.assigned_user_id.isnot(None),
            ~VMAssignment.is_teacher_vm,
            ~VMAssignment.is_template_vm
        ).count()
    
    @property
    def unassigned_count(self) -> int:
        """Number of student VMs not assigned to any user (excludes teacher and template VMs).
        
        Queries database directly to ensure fresh count after manual assignment changes.
        """
        return VMAssignment.query.filter(
            VMAssignment.class_id == self.id,
            VMAssignment.assigned_user_id.is_(None),
            ~VMAssignment.is_teacher_vm,
            ~VMAssignment.is_template_vm
        ).count()
    
    @property
    def enrolled_count(self) -> int:
        """Number of students enrolled in this class.
        
        Queries database directly to ensure fresh count.
        """
        return self.students.count()
    
    def is_owner(self, user) -> bool:
        """Check if user is an owner (primary teacher or co-owner) of this class."""
        if not user:
            return False
        # Check if primary teacher
        if self.teacher_id == user.id:
            return True
        # Check if co-owner
        try:
            return user in self.co_owners.all()
        except Exception:
            return False
    
    def to_dict(self) -> dict:
        # Build students list from enrolled students with their VM assignments
        students_list = []
        
        for student in self.students:
            # Find ANY VM assignment for this student in this class
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
        
        # Safely get co_owners list
        try:
            co_owners_list = [{'id': co.id, 'username': co.username} for co in self.co_owners.all()]
        except Exception:
            co_owners_list = []
        
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'teacher_id': self.teacher_id,
            'teacher_name': self.teacher.username if self.teacher else None,
            'co_owners': co_owners_list,
            'template_id': self.template_id,
            'template_name': self.template.name if self.template else None,
            'template_original_name': self.template.original_template.name if (self.template and getattr(self.template, 'original_template', None)) else None,
            'join_token': self.join_token,
            'token_valid': self.is_token_valid(),
            'token_never_expires': self.token_never_expires,
            'token_expires_at': self.token_expires_at.isoformat() if self.token_expires_at else None,
            'pool_size': self.pool_size,
            'cpu_cores': self.cpu_cores,
            'memory_mb': self.memory_mb,
            'disk_size_gb': self.disk_size_gb,
            'vmid_prefix': self.vmid_prefix,
            'assigned_count': self.assigned_count,
            'unassigned_count': self.unassigned_count,
            'enrolled_count': self.enrolled_count,
            'students': students_list,
            'deployment_method': self.deployment_method,
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
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    is_class_template = db.Column(db.Boolean, default=False)  # True if created for a specific class
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=True)  # Scoped to class if set
    original_template_id = db.Column(db.Integer, db.ForeignKey('templates.id'), nullable=True)  # Track source template
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_verified_at = db.Column(db.DateTime, nullable=True)  # Last time we confirmed this template exists in Proxmox
    
    # Cached VM specs from Proxmox (avoid repeated API calls)
    cpu_cores = db.Column(db.Integer, nullable=True)  # Number of CPU cores
    cpu_sockets = db.Column(db.Integer, nullable=True)  # Number of CPU sockets
    memory_mb = db.Column(db.Integer, nullable=True)  # RAM in MB
    disk_size_gb = db.Column(db.Float, nullable=True)  # Primary disk size in GB
    disk_storage = db.Column(db.String(80), nullable=True)  # Storage ID where disk resides
    disk_path = db.Column(db.String(255), nullable=True)  # Full disk path (e.g., "local-lvm:vm-100-disk-0")
    disk_format = db.Column(db.String(20), nullable=True)  # Disk format (qcow2, raw, etc.)
    network_bridge = db.Column(db.String(80), nullable=True)  # Network bridge (e.g., "vmbr0")
    os_type = db.Column(db.String(20), nullable=True)  # OS type (l26, win10, etc.)
    specs_cached_at = db.Column(db.DateTime, nullable=True)  # When specs were last fetched from Proxmox
    
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
            'created_by_id': self.created_by_id,
            'created_by_name': self.created_by.username if self.created_by else None,
            'is_class_template': self.is_class_template,
            'class_id': self.class_id,
            'original_template_id': self.original_template_id,
            'original_template_name': self.original_template.name if getattr(self, 'original_template', None) else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_verified_at': self.last_verified_at.isoformat() if self.last_verified_at else None,
            # Include cached specs
            'cpu_cores': self.cpu_cores,
            'cpu_sockets': self.cpu_sockets,
            'memory_mb': self.memory_mb,
            'disk_size_gb': self.disk_size_gb,
            'disk_storage': self.disk_storage,
            'disk_path': self.disk_path,
            'disk_format': self.disk_format,
            'network_bridge': self.network_bridge,
            'os_type': self.os_type,
            'specs_cached_at': self.specs_cached_at.isoformat() if self.specs_cached_at else None,
        }
    
    def __repr__(self):
        replica_tag = " (replica)" if self.is_replica else ""
        return f'<Template {self.name} (VMID: {self.proxmox_vmid}, Node: {self.node}){replica_tag}>'


class VMAssignment(db.Model):
    """VM cloned for a class, optionally assigned to a user."""
    __tablename__ = 'vm_assignments'
    
    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=True, index=True)  # NULL for direct assignments (builder VMs)
    proxmox_vmid = db.Column(db.Integer, nullable=False, index=True)  # Cloned VM's VMID in Proxmox
    vm_name = db.Column(db.String(120), nullable=True, index=True)  # VM name in Proxmox
    mac_address = db.Column(db.String(17), nullable=True, index=True)  # VM's MAC address (e.g., "AA:BB:CC:DD:EE:FF")
    cached_ip = db.Column(db.String(45), nullable=True)  # Cached IP address (IPv4 or IPv6)
    ip_updated_at = db.Column(db.DateTime, nullable=True)  # When IP was last updated
    node = db.Column(db.String(80), nullable=True)  # Proxmox node where VM resides
    assigned_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    status = db.Column(db.String(20), default='available')  # available, assigned, deleting
    is_template_vm = db.Column(db.Boolean, default=False)  # True if this is the class template VM (master copy)
    is_teacher_vm = db.Column(db.Boolean, default=False)  # True if this is the teacher's personal VM
    manually_added = db.Column(db.Boolean, default=False)  # True if VM was manually added (don't auto-assign)
    hostname_configured = db.Column(db.Boolean, default=False)  # True if hostname has been set (prevents rename loops)
    target_hostname = db.Column(db.String(63), nullable=True)  # Intended hostname for this VM (for auto-rename)
    usage_hours = db.Column(db.Float, default=0.0)  # Cumulative hours this VM has been used by student
    usage_last_reset = db.Column(db.DateTime, nullable=True)  # When usage was last reset by teacher
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
            'vm_name': self.vm_name,
            'mac_address': self.mac_address,
            'mac': self.mac_address,  # Alias for template compatibility
            'ip': self.cached_ip,  # Alias for template compatibility
            'cached_ip': self.cached_ip,
            'ip_updated_at': self.ip_updated_at.isoformat() if self.ip_updated_at else None,
            'node': self.node,
            'assigned_user_id': self.assigned_user_id,
            'assigned_user_name': self.assigned_user.username if self.assigned_user else None,
            'status': self.status,
            'is_template_vm': self.is_template_vm,
            'is_teacher_vm': self.is_teacher_vm,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'assigned_at': self.assigned_at.isoformat() if self.assigned_at else None,
        }
    
    def __repr__(self):
        return f'<VMAssignment {self.proxmox_vmid} -> {self.assigned_user.username if self.assigned_user else "unassigned"}>'





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


class ProxmoxNode(db.Model):
    """Cached mapping of Proxmox node hostnames to IP addresses.
    
    Used to avoid repeated SSH hostname lookups during VM deployment.
    Hostnames resolved once and cached for 7 days.
    """
    __tablename__ = 'proxmox_nodes'
    
    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.String(50), nullable=False, index=True)  # Cluster identifier
    hostname = db.Column(db.String(255), nullable=False)  # Node hostname (e.g., netlab2)
    ip_address = db.Column(db.String(45), nullable=False)  # IPv4 or IPv6 address
    last_resolved_at = db.Column(db.DateTime, default=datetime.utcnow)  # When the IP was resolved
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    __table_args__ = (
        db.UniqueConstraint('cluster_id', 'hostname', name='unique_cluster_hostname'),
    )
    
    @classmethod
    def get_cached_ip(cls, cluster_id: str, hostname: str, cache_days: int = 7) -> Optional[str]:
        """Get cached IP for a hostname if it exists and is still fresh.
        
        Args:
            cluster_id: Cluster identifier
            hostname: Node hostname to look up
            cache_days: How many days to trust the cached IP (default 7 days)
        
        Returns:
            Cached IP address if fresh, None if not found or stale
        """
        node = cls.query.filter_by(cluster_id=cluster_id, hostname=hostname).first()
        if not node:
            return None
        
        # Check if cache is still fresh
        age = datetime.utcnow() - node.last_resolved_at
        if age > timedelta(days=cache_days):
            return None  # Cache is stale
        
        return node.ip_address
    
    @classmethod
    def cache_ip(cls, cluster_id: str, hostname: str, ip_address: str) -> 'ProxmoxNode':
        """Cache a hostname-to-IP mapping.
        
        Args:
            cluster_id: Cluster identifier
            hostname: Node hostname
            ip_address: Resolved IP address
        
        Returns:
            ProxmoxNode object (newly created or updated)
        """
        node = cls.query.filter_by(cluster_id=cluster_id, hostname=hostname).first()
        
        if node:
            # Update existing entry
            node.ip_address = ip_address
            node.last_resolved_at = datetime.utcnow()
            node.updated_at = datetime.utcnow()
        else:
            # Create new entry
            node = cls(
                cluster_id=cluster_id,
                hostname=hostname,
                ip_address=ip_address,
                last_resolved_at=datetime.utcnow()
            )
            db.session.add(node)
        
        db.session.commit()
        return node
    
    def __repr__(self):
        return f'<ProxmoxNode {self.cluster_id}:{self.hostname}={self.ip_address}>'


def init_db(app):
    """Initialize database with Flask app context.
    
    Call this in create_app() to set up the database.
    """
    # Set database URI if not already configured
    if 'SQLALCHEMY_DATABASE_URI' not in app.config:
        db_path = os.path.join(os.path.dirname(__file__), 'lab_portal.db')
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
        # Improve SQLite concurrency: allow cross-thread access and increase lock timeout to 60s
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'connect_args': {
                'check_same_thread': False,
                'timeout': 60,  # 60 second timeout for write locks (handles background thread contention)
            },
            'pool_pre_ping': True,  # Verify connections are alive
            'pool_recycle': 3600,  # Recycle connections after 1 hour
            'pool_size': 20,  # Increase from default 5 to handle background threads
            'max_overflow': 30,  # Increase from default 10 to handle bursts
        }
    
    # Disable modification tracking (not needed and impacts performance)
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    # Initialize SQLAlchemy with the app
    db.init_app(app)
    
    # Create tables if they don't exist
    with app.app_context():
        # Enable WAL journal mode to reduce write-lock contention
        # WAL allows multiple concurrent readers with one writer
        try:
            db.session.execute("PRAGMA journal_mode=WAL;")
            db.session.execute("PRAGMA busy_timeout=60000;")  # 60 second busy timeout in milliseconds
            db.session.execute("PRAGMA synchronous=NORMAL;")  # Faster writes, still crash-safe with WAL
            db.session.commit()
        except Exception:
            db.session.rollback()
        db.create_all()
