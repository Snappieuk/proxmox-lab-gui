erDiagram
    %% =========================
    %% Core Entities
    %% =========================
    CLUSTER {
        int id PK
        string cluster_id  "unique"
        string name
        string host
        int port
        bool is_default
        bool is_active
    }

    ISO_IMAGE {
        int id PK
        string volid  "unique"
        string name
        bigint size
        string node
        string storage
        string cluster_id
    }

    USER {
        int id PK
        string username  "unique"
        string password_hash
        string role       "adminer|teacher|user"
        datetime created_at
    }

    CLASS {
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
        int hours_start
        int hours_end
        int max_usage_hours
        datetime created_at
        datetime updated_at
    }

    TEMPLATE {
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
        datetime created_at
        datetime last_verified_at
    }

    VM_ASSIGNMENT {
        int id PK
        int class_id FK          "nullable for direct assignments"
        int proxmox_vmid
        string vm_name
        string mac_address
        string cached_ip
        datetime ip_updated_at
        string node
        int assigned_user_id FK
        string status            "available|assigned|deleting"
        bool is_template_vm
        bool is_teacher_vm
        bool manually_added
        float usage_hours
        datetime usage_last_reset
        datetime created_at
        datetime assigned_at
    }

    VM_IP_CACHE {
        int vmid PK
        string mac_address
        string cached_ip
        datetime ip_updated_at
        string cluster_id
    }

    VM_INVENTORY {
        int id PK
        string cluster_id
        int vmid
        string name
        string node
        string status
        string type
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

    CLASS_ENROLLMENT {
        int user_id FK
        int class_id FK
        datetime enrolled_at
    }

    CLASS_CO_OWNER {
        int user_id FK
        int class_id FK
        datetime added_at
    }

    %% =========================
    %% Relationships
    %% =========================

    %% Users & Classes
    USER ||--o{ CLASS : "teaches"
    CLASS }o--o{ USER : "students via CLASS_ENROLLMENT"
    USER ||--o{ CLASS_ENROLLMENT : "enrollment"
    CLASS ||--o{ CLASS_ENROLLMENT : ""

    %% Class co-owners (teachers with extra rights)
    USER ||--o{ CLASS_CO_OWNER : "co-owns"
    CLASS ||--o{ CLASS_CO_OWNER : ""

    %% Users & VMs
    USER ||--o{ VM_ASSIGNMENT : "has VMs"

    %% Classes & VMs
    CLASS ||--o{ VM_ASSIGNMENT : "VM pool"

    %% Templates & Classes
    TEMPLATE ||--o{ CLASS : "used as base (template_id)"
    CLASS ||--o{ TEMPLATE : "owns class templates (class_id)"

    %% Users & Templates
    USER ||--o{ TEMPLATE : "creates (created_by_id)"

    %% Template self-reference
    TEMPLATE ||--o{ TEMPLATE : "derived from (original_template_id)"

    %% Cluster-related (logical via cluster_id)
    CLUSTER ||--o{ VM_INVENTORY : "VMs in cluster"
    CLUSTER ||--o{ ISO_IMAGE : "ISOs in cluster"
    CLUSTER ||--o{ VM_IP_CACHE : "legacy IP cache"

    %% Inventory ↔ IP cache (logical mapping by vmid+cluster)
    VM_INVENTORY ||--o{ VM_IP_CACHE : "cached IP (legacy)"
