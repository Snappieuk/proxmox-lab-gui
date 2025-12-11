
---

```mermaid
classDiagram

%% =========================
%% Core entities
%% =========================

class Cluster {
    int id
    string cluster_id
    string name
    string host
    int port
    string user
    string password
    bool verify_ssl
    bool is_default
    bool is_active
    datetime created_at
    datetime updated_at
}

class ISOImage {
    int id
    string volid
    string name
    bigint size
    string node
    string storage
    string cluster_id
    datetime discovered_at
    datetime last_seen
}

class User {
    int id
    string username
    string password_hash
    string role  // adminer|teacher|user
    datetime created_at
}

class Class {
    int id
    string name
    text description
    int teacher_id
    int template_id
    string join_token
    datetime token_expires_at
    bool token_never_expires
    int pool_size
    int cpu_cores
    int memory_mb
    int disk_size_gb
    int vmid_prefix
    string clone_task_id
    string deployment_node
    string deployment_cluster
    bool auto_shutdown_enabled
    int auto_shutdown_cpu_threshold
    int auto_shutdown_idle_minutes
    bool restrict_hours
    int hours_start
    int hours_end
    int max_usage_hours
    datetime created_at
    datetime updated_at
}

class Template {
    int id
    string name
    int proxmox_vmid
    string cluster_ip
    string node
    bool is_replica
    int source_vmid
    string source_node
    int created_by_id
    bool is_class_template
    int class_id
    int original_template_id
    datetime created_at
    datetime last_verified_at
    int cpu_cores
    int cpu_sockets
    int memory_mb
    float disk_size_gb
    string disk_storage
    string disk_path
    string disk_format
    string network_bridge
    string os_type
    datetime specs_cached_at
}

class VMAssignment {
    int id
    int class_id
    int proxmox_vmid
    string vm_name
    string mac_address
    string cached_ip
    datetime ip_updated_at
    string node
    int assigned_user_id
    string status        // available|assigned|deleting
    bool is_template_vm
    bool is_teacher_vm
    bool manually_added
    float usage_hours
    datetime usage_last_reset
    datetime created_at
    datetime assigned_at
}

class VMIPCache {
    int vmid
    string mac_address
    string cached_ip
    datetime ip_updated_at
    string cluster_id
}

class VMInventory {
    int id
    string cluster_id
    int vmid
    string name
    string node
    string status        // running|stopped|unknown
    string type          // qemu|lxc
    string category
    string ip
    string mac_address
    bigint memory
    int cores
    bigint disk_size
    int uptime
    float cpu_usage
    float memory_usage
    bool is_template
    text tags
    bool rdp_available
    bool ssh_available
    datetime last_updated
    datetime last_status_check
    text sync_error
}

class ClassEnrollment {
    int user_id
    int class_id
    datetime enrolled_at
}

class ClassCoOwner {
    int user_id
    int class_id
    datetime added_at
}

%% =========================
%% Relationships
%% =========================

%% Users & Classes
User "1" --> "*" Class : teaches
User "*" --> "*" Class : enrolled_via_ClassEnrollment
User "*" --> "*" Class : co_owns_via_ClassCoOwner

ClassEnrollment "*" --> "1" User : user
ClassEnrollment "*" --> "1" Class : class

ClassCoOwner "*" --> "1" User : user
ClassCoOwner "*" --> "1" Class : class

%% Users & VM assignments
User "1" --> "*" VMAssignment : vm_assignments

%% Classes & VM assignments
Class "1" --> "*" VMAssignment : vm_pool

%% Templates & Classes
Template "1" --> "*" Class : base_for_classes
Class "1" --> "*" Template : owned_templates

%% Users & Templates
User "1" --> "*" Template : created_templates

%% Template self-reference
Template "1" --> "0..1" Template : original_template

%% Cluster relationships (logical via cluster_id / cluster_ip)
Cluster "1" --> "*" ISOImage : iso_images
Cluster "1" --> "*" VMInventory : inventory
Cluster "1" --> "*" VMIPCache : ip_cache
