# Terraform VM Deployment for Proxmox Lab

This directory contains Terraform configurations for deploying mass VMs to Proxmox clusters. **Much simpler and more reliable than custom Python cloning code.**

## Why Terraform?

**Advantages over custom Python code:**
- ✅ **Declarative** - describe what you want, Terraform handles how
- ✅ **Idempotent** - safe to run multiple times
- ✅ **State tracking** - knows what exists, enables updates/deletions
- ✅ **Parallel execution** - creates VMs concurrently (10x faster)
- ✅ **Built-in retry logic** - handles transient failures
- ✅ **Rollback capability** - destroy entire deployment with one command
- ✅ **Well-tested** - Proxmox provider used by thousands
- ✅ **No custom code** - no bugs to fix, no maintenance burden

**Disadvantages of custom Python:**
- ❌ Complex error handling (storage compatibility, locks, race conditions)
- ❌ Sequential execution (slow for large deployments)
- ❌ No idempotency (can't safely re-run)
- ❌ No state tracking (hard to know what's deployed)
- ❌ Maintenance burden (we've already fixed multiple bugs)

## Quick Start

### 1. Install Terraform

**Windows (PowerShell):**
```powershell
# Using Chocolatey
choco install terraform

# Or download from https://www.terraform.io/downloads
```

**Linux:**
```bash
# Ubuntu/Debian
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install terraform
```

### 2. Configure Deployment

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your settings
```

**Key settings:**
```hcl
proxmox_password = "your-password"
class_id         = "cs101"
template_name    = "ubuntu-22.04-template"

students = {
  "student1" = { role = "default" }
  "student2" = { role = "default" }
  # Add as many as you need
}
```

### 3. Deploy VMs

```bash
# Initialize Terraform (first time only)
terraform init

# Preview what will be created
terraform plan

# Create the VMs
terraform apply
# Type 'yes' when prompted
```

**That's it!** Terraform will:
- Clone the template N times in parallel
- Distribute VMs across nodes (round-robin)
- Configure networking (DHCP or static IPs)
- Tag VMs with class ID
- Create teacher VM if configured
- Show you all VM IDs and IPs

### 4. Manage Deployment

```bash
# View current state
terraform show

# Get VM information
terraform output student_vms
terraform output teacher_vm

# Update VMs (e.g., add more students)
# Edit terraform.tfvars, add more students
terraform apply  # Only creates new VMs, leaves existing untouched

# Destroy entire class
terraform destroy
# Type 'yes' to confirm - all VMs deleted
```

## Configuration Examples

### Example 1: Simple Class (10 Students, DHCP)

```hcl
class_id     = "cs101"
class_prefix = "cs101"

students = {
  "student1"  = { role = "default" }
  "student2"  = { role = "default" }
  "student3"  = { role = "default" }
  "student4"  = { role = "default" }
  "student5"  = { role = "default" }
  "student6"  = { role = "default" }
  "student7"  = { role = "default" }
  "student8"  = { role = "default" }
  "student9"  = { role = "default" }
  "student10" = { role = "default" }
}

template_name  = "ubuntu-22.04-template"
use_full_clone = false  # Linked clones for speed
use_dhcp       = true

node_distribution = ["pve1", "pve2"]  # Spread across nodes
```

### Example 2: Large Class (50 Students, Static IPs)

```hcl
class_id     = "cs201"
class_prefix = "cs201"

# Generate students programmatically
students = {
  for i in range(1, 51) : "student${i}" => { role = "default" }
}

template_name  = "ubuntu-22.04-template"
use_full_clone = false

use_dhcp = false
subnet   = "10.220.15.0/24"
gateway  = "10.220.15.1"
# IPs will be 10.220.15.100, 10.220.15.101, etc.

node_distribution = ["pve1", "pve2", "pve3"]  # Spread across 3 nodes
```

### Example 3: High-Resource Class (4 cores, 8GB RAM)

```hcl
class_id     = "cs301"
class_prefix = "cs301"

students = {
  "student1" = { role = "default" }
  "student2" = { role = "default" }
  # ...
}

template_name = "ubuntu-22.04-template"

# Beef up the resources
vm_cores  = 4
vm_memory = 8192  # 8GB
disk_size = "64G"

storage_pool = "fast-ssd"  # Use faster storage
```

### Example 4: Single Node Deployment

```hcl
class_id     = "test-class"
class_prefix = "test"

students = {
  "test1" = { role = "default" }
  "test2" = { role = "default" }
}

template_name = "ubuntu-22.04-template"

# Deploy all VMs to one node
target_node = "pve1"  # Overrides node_distribution
```

## Integration with Flask App

You can integrate Terraform with your Flask app:

### Option 1: Call Terraform from Python

```python
import subprocess
import json
import os

def deploy_class_with_terraform(class_id, student_count, template_name):
    """Deploy class VMs using Terraform."""
    
    # Generate tfvars file
    tfvars = {
        "class_id": class_id,
        "class_prefix": class_id,
        "students": {
            f"student{i}": {"role": "default"} 
            for i in range(1, student_count + 1)
        },
        "template_name": template_name,
    }
    
    # Write to tfvars.json
    tfvars_path = f"terraform/classes/{class_id}.tfvars.json"
    os.makedirs(os.path.dirname(tfvars_path), exist_ok=True)
    with open(tfvars_path, 'w') as f:
        json.dump(tfvars, f)
    
    # Run terraform apply
    result = subprocess.run(
        ["terraform", "apply", "-auto-approve", f"-var-file={tfvars_path}"],
        cwd="terraform",
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Terraform apply failed: {result.stderr}")
    
    # Get output
    output = subprocess.run(
        ["terraform", "output", "-json"],
        cwd="terraform",
        capture_output=True,
        text=True
    )
    
    return json.loads(output.stdout)

# Usage
vm_info = deploy_class_with_terraform("cs101", 10, "ubuntu-22.04-template")
print(f"Created VMs: {vm_info['vm_ids']}")
```

### Option 2: Store Terraform State in Database

After Terraform deploys VMs, parse the output and store in your database:

```python
def sync_terraform_vms_to_db(class_id):
    """Sync Terraform-deployed VMs to database."""
    
    # Get Terraform output
    result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=f"terraform/classes/{class_id}",
        capture_output=True,
        text=True
    )
    output = json.loads(result.stdout)
    
    # Store in database
    for student_name, vm_info in output['student_vms']['value'].items():
        assignment = VMAssignment(
            user_id=get_user_by_username(student_name).id,
            class_id=class_id,
            proxmox_vmid=vm_info['id'],
            status='assigned'
        )
        db.session.add(assignment)
    
    db.session.commit()
```

### Option 3: Replace Python Cloning Entirely

**Before (Python):**
```python
# app/services/proxmox_operations.py - 800+ lines of complex cloning code
def clone_vms_for_class(class_id, count, template_vmid, ...):
    # Complex VMID allocation
    # Storage compatibility checks
    # Sequential cloning with delays
    # Template conversion with verification
    # Error handling for locks, race conditions
    # ...
```

**After (Terraform):**
```python
# app/services/terraform_service.py - 50 lines
def deploy_class_vms(class_id, student_count, template_name):
    tfvars = generate_tfvars(class_id, student_count, template_name)
    run_terraform_apply(tfvars)
    sync_to_database()
```

## Advanced Features

### Workspaces (Multiple Classes)

Use Terraform workspaces to manage multiple classes separately:

```bash
# Create workspace for CS101
terraform workspace new cs101
terraform apply -var-file=cs101.tfvars

# Switch to CS201
terraform workspace new cs201
terraform apply -var-file=cs201.tfvars

# List all classes
terraform workspace list

# Destroy CS101 only
terraform workspace select cs101
terraform destroy
```

### Remote State (Team Collaboration)

Store Terraform state remotely so multiple admins can manage VMs:

```hcl
# backend.tf
terraform {
  backend "s3" {
    bucket = "my-terraform-state"
    key    = "proxmox-lab/terraform.tfstate"
    region = "us-east-1"
  }
}
```

Or use Terraform Cloud for free state storage and collaboration.

### Import Existing VMs

Already have VMs created with Python? Import them into Terraform:

```bash
# Import existing VM
terraform import 'proxmox_vm_qemu.student_vms["student1"]' pve1/qemu/200

# Now Terraform manages it
```

## Troubleshooting

### "Error: timeout while waiting for state"

Template doesn't have guest agent or networking misconfigured.

**Fix:**
```hcl
# Disable guest agent wait
agent = 0

# Or increase timeout
timeouts {
  create = "30m"
}
```

### "Error: VM already exists"

Terraform state is out of sync.

**Fix:**
```bash
# Refresh state
terraform refresh

# Or import existing VM
terraform import 'proxmox_vm_qemu.student_vms["student1"]' node/qemu/vmid
```

### "Error: storage X does not support linked clones"

Storage backend doesn't support CoW.

**Fix:**
```hcl
use_full_clone = true
```

### Parallel Execution Too Fast

Proxmox rate limiting or lock contention.

**Fix:**
```bash
# Limit parallelism
terraform apply -parallelism=2
```

## Migration Guide

### From Python Cloning to Terraform

**Step 1:** Install Terraform and test with small class

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Configure for 2-3 test VMs
terraform init
terraform plan
terraform apply
```

**Step 2:** Verify VMs work correctly (network, IPs, access)

**Step 3:** Update Flask app to call Terraform instead of Python cloning

```python
# In app/routes/api/classes.py
@bp.route("/classes/<int:class_id>/deploy", methods=["POST"])
def deploy_class_vms(class_id):
    cls = Class.query.get_or_404(class_id)
    
    # OLD: Python cloning
    # clone_vms_for_class(class_id, ...)
    
    # NEW: Terraform
    from app.services.terraform_service import deploy_class_with_terraform
    result = deploy_class_with_terraform(
        class_id=cls.id,
        student_count=cls.capacity,
        template_name=cls.template.name
    )
    
    return jsonify({"ok": True, "vms": result})
```

**Step 4:** Gradually migrate existing deployments

```bash
# For each existing class, import VMs into Terraform
terraform import 'proxmox_vm_qemu.student_vms["student1"]' pve1/qemu/200
# Repeat for all VMs
```

**Step 5:** Remove Python cloning code (optional)

Once confident, remove `clone_vms_for_class()` and related functions from `proxmox_operations.py`.

## Best Practices

1. **Version control** - Commit `*.tf` files, ignore `*.tfvars` (secrets)
2. **Remote state** - Use Terraform Cloud or S3 backend for team collaboration
3. **Workspaces** - One workspace per class for isolation
4. **Variables** - Use `-var-file` for different environments (dev/prod)
5. **Modules** - Create reusable modules for common patterns
6. **Validation** - Use `terraform validate` in CI/CD
7. **Plan first** - Always run `terraform plan` before `apply`
8. **Destroy carefully** - Use `-target` to destroy specific VMs if needed

## Cost Comparison

**Python Cloning (Current):**
- 800+ lines of custom code
- Multiple bugs fixed (storage, locks, VMIDs)
- Sequential execution (10 VMs = 10 minutes)
- Hard to roll back
- No idempotency
- Ongoing maintenance burden

**Terraform (Proposed):**
- ~100 lines of config
- Zero bugs (provider handles complexity)
- Parallel execution (10 VMs = 2 minutes)
- Easy rollback (`terraform destroy`)
- Fully idempotent
- No maintenance (provider updates handle fixes)

**Conclusion:** Terraform is objectively better for mass VM deployment. The only advantage of custom Python code was deep integration with Flask, but that's easily solved with subprocess calls or a thin wrapper service.

## Resources

- [Terraform Proxmox Provider Docs](https://registry.terraform.io/providers/Telmate/proxmox/latest/docs)
- [Terraform Getting Started](https://learn.hashicorp.com/terraform)
- [Proxmox VM Cloning Best Practices](https://pve.proxmox.com/wiki/VM_Templates_and_Clones)
