
---

```mermaid
erDiagram

%% =========================
%% Core Entities
%% =========================

    Cluster {
        int id PK
        string cluster_id UK
        string name
        string host
        int port
        string user
        string password
        bool verify_ssl
        bool is_default
        bool is_active
        bool allow_vm_deployment
        bool allow_template_sync
        bool allow_iso_sync
        bool auto_shutdown_enabled
        int priority
        string default_storage
        string template_storage
        string qcow2_template_path
        string qcow2_images_path
        string admin_group
        text admin_users
        text arp_subnets
        int vm_cache_ttl
        bool enable_ip_lookup
        bool enable_ip_persistence
        text description
        datetime created_at
        datetime updated_at
    }
    
    SystemSettings {
        int id PK
        string key UK
        text value
        text description
        datetime updated_at
    }
    
    User {
        int id PK
        string username UK
        string password_hash
        string role "adminer, teacher, or user"
        datetime created_at
    }
    
    Class {
        int id PK
        string name
        text description
        int teacher_id FK
        int template_id FK
        string join_token
        datetime token_expires_at
        bool token_never_expires
        int pool_size
        int cpu_cores
        int memory_mb
        int disk_size_gb
        int vmid_prefix
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
    
    Template {
        int id PK
        string name
        int proxmox_vmid
        string cluster_ip
        string node
        bool is_replica
        int source_vmid
        string source_node
        int created_by_id FK
        bool is_class_template
        int class_id FK
        int original_template_id FK
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
        datetime created_at
        datetime last_verified_at
    }
    
    VMAssignment {
        int id PK
        int class_id FK
        int proxmox_vmid
        string vm_name
        string mac_address
        string cached_ip
        datetime ip_updated_at
        string node
        int assigned_user_id FK
        string status "available, assigned, or deleting"
        bool is_template_vm
        bool is_teacher_vm
        bool manually_added
        float usage_hours
        datetime usage_last_reset
        datetime created_at
        datetime assigned_at
    }
    
    VMInventory {
        int id PK
        string cluster_id
        int vmid
        string name
        string node
        string status "running, stopped, or unknown"
        string type "qemu or lxc"
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
    
    VMIPCache {
        int vmid PK
        string mac_address
        string cached_ip
        datetime ip_updated_at
        string cluster_id
    }
    
    ISOImage {
        int id PK
        string volid UK
        string name
        bigint size
        string node
        string storage
        string cluster_id
        datetime discovered_at
        datetime last_seen
    }
    
    ClassEnrollment {
        int user_id FK
        int class_id FK
        datetime enrolled_at
    }
    
    ClassCoOwner {
        int user_id FK
        int class_id FK
        datetime added_at
    }

%% =========================
%% Relationships
%% =========================

    User ||--o{ Class : "teaches"
    User ||--o{ Template : "creates"
    User ||--o{ VMAssignment : "assigned_to"
    
    Class ||--o{ VMAssignment : "has_pool"
    Class }o--|| Template : "uses_base"
    Class ||--o{ Template : "owns_class_templates"
    Class }o--|| User : "taught_by"
    
    Template }o--o| Template : "cloned_from"
    Template }o--|| User : "created_by"
    Template }o--o| Class : "class_specific"
    
    Cluster ||--o{ VMInventory : "stores_inventory"
    Cluster ||--o{ VMIPCache : "caches_ips"
    Cluster ||--o{ ISOImage : "stores_isos"
    
    ClassEnrollment }o--|| User : "student"
    ClassEnrollment }o--|| Class : "enrolled_in"
    
    ClassCoOwner }o--|| User : "co_owner"
    ClassCoOwner }o--|| Class : "co_owned_class"
