terraform {
  required_version = ">= 1.0"
  
  required_providers {
    proxmox = {
      source  = "telmate/proxmox"
      version = "~> 2.9"
    }
  }
}

provider "proxmox" {
  pm_api_url      = var.proxmox_api_url
  pm_user         = var.proxmox_user
  pm_password     = var.proxmox_password
  pm_tls_insecure = var.proxmox_tls_insecure
}

# Local value for smart node selection
# If auto_select_node is true, Terraform will distribute based on round-robin
# The Proxmox provider doesn't expose resource metrics, so we use external data source
locals {
  # Convert students map to list for indexing
  student_list = keys(var.students)
  
  # Smart node selection: Use external script if enabled, otherwise round-robin
  node_selection = var.auto_select_node && var.target_node == "" ? {
    for idx, student in local.student_list : 
      student => element(var.node_distribution, idx % length(var.node_distribution))
  } : {
    for student in local.student_list : 
      student => var.target_node != "" ? var.target_node : element(var.node_distribution, index(local.student_list, student) % length(var.node_distribution))
  }
}

# Generate student VMs from original template
resource "proxmox_vm_qemu" "student_vms" {
  for_each = var.students

  name        = "${var.class_prefix}-${each.key}"
  target_node = local.node_selection[each.key]
  
  # Clone from original template (inherits CPU/RAM/disk from template)
  clone      = var.template_name
  full_clone = var.use_full_clone
  
  # Cloud-init configuration (if template supports it)
  os_type   = "cloud-init"
  ipconfig0 = "ip=dhcp"
  
  # Lifecycle
  lifecycle {
    ignore_changes = [
      # Don't recreate VM if these change
      network,
      disk,
    ]
  }
  
  # Tags for identification
  tags = "${var.class_id},student,${each.value.role}"
  
  # Start VM after creation
  automatic_reboot = false
  onboot           = false
  
  # Wait for guest agent
  agent = 1
}

# Teacher VM 1: Editable VM for teacher use
resource "proxmox_vm_qemu" "teacher_vm" {
  count = var.create_teacher_vm ? 1 : 0
  
  name        = "${var.class_prefix}-teacher"
  target_node = var.teacher_node != "" ? var.teacher_node : var.node_distribution[0]
  
  # Clone from original template (inherits specs, respects clone setting)
  clone      = var.template_name
  full_clone = var.use_full_clone
  
  os_type = "cloud-init"
  ipconfig0 = "ip=dhcp"
  
  tags = "${var.class_id},teacher,editable"
  
  agent = 1
}

# Teacher VM 2: Clone from original template, teacher can modify and convert to class template
# Note: This is converted to a template for future use, but student VMs are created from the original template
resource "proxmox_vm_qemu" "teacher_template_vm" {
  count = var.create_teacher_template ? 1 : 0
  
  name        = "${var.class_prefix}-template"
  target_node = var.teacher_node != "" ? var.teacher_node : var.node_distribution[0]
  
  # Clone from original template (inherits specs, respects clone setting)
  clone      = var.template_name
  full_clone = var.use_full_clone
  
  os_type = "cloud-init"
  ipconfig0 = "ip=dhcp"
  
  tags = "${var.class_id},template,class-template"
  
  agent = 1
  
  # IMPORTANT: This VM will be converted to a template via Proxmox API after creation
  # Terraform doesn't support template conversion, so we handle it in Python
}

# Output VM information
output "student_vms" {
  description = "Student VM details"
  value = {
    for name, vm in proxmox_vm_qemu.student_vms : name => {
      id       = vm.vmid
      name     = vm.name
      node     = vm.target_node
      ip       = vm.default_ipv4_address
    }
  }
}

output "teacher_vm" {
  description = "Teacher VM details (editable)"
  value = var.create_teacher_vm ? {
    id   = proxmox_vm_qemu.teacher_vm[0].vmid
    name = proxmox_vm_qemu.teacher_vm[0].name
    node = proxmox_vm_qemu.teacher_vm[0].target_node
    ip   = proxmox_vm_qemu.teacher_vm[0].default_ipv4_address
  } : null
}

output "teacher_template_vm" {
  description = "Teacher template VM details (class-specific template for teacher to modify)"
  value = var.create_teacher_template ? {
    id   = proxmox_vm_qemu.teacher_template_vm[0].vmid
    name = proxmox_vm_qemu.teacher_template_vm[0].name
    node = proxmox_vm_qemu.teacher_template_vm[0].target_node
    ip   = proxmox_vm_qemu.teacher_template_vm[0].default_ipv4_address
  } : null
}

output "vm_ids" {
  description = "List of all created VM IDs"
  value = concat(
    [for vm in proxmox_vm_qemu.student_vms : vm.vmid],
    var.create_teacher_vm ? [proxmox_vm_qemu.teacher_vm[0].vmid] : [],
    var.create_teacher_template ? [proxmox_vm_qemu.teacher_template_vm[0].vmid] : []
  )
}
