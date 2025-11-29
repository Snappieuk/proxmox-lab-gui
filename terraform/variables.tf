# Proxmox Connection
variable "proxmox_api_url" {
  description = "Proxmox API URL"
  type        = string
  default     = "https://10.220.15.249:8006/api2/json"
}

variable "proxmox_user" {
  description = "Proxmox API user"
  type        = string
  default     = "root@pam"
}

variable "proxmox_password" {
  description = "Proxmox API password"
  type        = string
  sensitive   = true
}

variable "proxmox_tls_insecure" {
  description = "Skip TLS verification"
  type        = bool
  default     = true
}

# Class Configuration
variable "class_id" {
  description = "Class identifier (used in tags)"
  type        = string
}

variable "class_prefix" {
  description = "Prefix for VM names (e.g., 'cs101' -> 'cs101-student1')"
  type        = string
}

variable "students" {
  description = "Map of student usernames to their properties"
  type = map(object({
    role = string  # Optional: student role/group
  }))
  
  # Example:
  # students = {
  #   "student1" = { role = "default" }
  #   "student2" = { role = "default" }
  # }
}

# Template Configuration
variable "template_name" {
  description = "Name of the template to clone from"
  type        = string
}

variable "use_full_clone" {
  description = "Use full clone instead of linked clone"
  type        = bool
  default     = false
}

# VM Resources
variable "vm_cores" {
  description = "Number of CPU cores per VM"
  type        = number
  default     = 2
}

variable "vm_sockets" {
  description = "Number of CPU sockets per VM"
  type        = number
  default     = 1
}

variable "vm_memory" {
  description = "Memory in MB per VM"
  type        = number
  default     = 2048
}

variable "disk_size" {
  description = "Disk size (e.g., '32G')"
  type        = string
  default     = "32G"
}

variable "storage_pool" {
  description = "Storage pool name"
  type        = string
  default     = "local-lvm"
}

# Network Configuration
variable "network_bridge" {
  description = "Network bridge"
  type        = string
  default     = "vmbr0"
}

variable "use_dhcp" {
  description = "Use DHCP for IP assignment"
  type        = bool
  default     = true
}

variable "subnet" {
  description = "Subnet for static IPs (if not using DHCP)"
  type        = string
  default     = "10.220.8.0/21"
}

variable "gateway" {
  description = "Gateway IP for static IPs"
  type        = string
  default     = "10.220.12.1"
}

# Node Distribution
variable "node_distribution" {
  description = "List of nodes to distribute VMs across (round-robin)"
  type        = list(string)
  default     = ["pve1", "pve2"]
}

variable "target_node" {
  description = "Override node distribution, deploy all VMs to this node"
  type        = string
  default     = ""
}

variable "auto_select_node" {
  description = "Automatically select best node based on resources (uses round-robin as fallback)"
  type        = bool
  default     = true
}

# Teacher VM Configuration
variable "create_teacher_vm" {
  description = "Create a separate teacher VM (editable)"
  type        = bool
  default     = true
}

variable "create_teacher_template" {
  description = "Create a teacher template VM (will be converted to template for students to clone from)"
  type        = bool
  default     = true
}

variable "teacher_node" {
  description = "Specific node for teacher VMs (empty = use first node in distribution)"
  type        = string
  default     = ""
}

# Cloud-init
variable "default_user" {
  description = "Default user for cloud-init"
  type        = string
  default     = "student"
}

variable "ssh_public_keys" {
  description = "SSH public keys for cloud-init (newline-separated)"
  type        = string
  default     = ""
}
