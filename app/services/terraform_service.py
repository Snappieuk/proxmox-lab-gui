#!/usr/bin/env python3
"""
Terraform Integration Service

Provides Python interface to Terraform for VM deployment.
Much simpler and more reliable than custom Python cloning code.
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Terraform working directory
TERRAFORM_DIR = Path(__file__).parent.parent.parent / "terraform"
TERRAFORM_CLASSES_DIR = TERRAFORM_DIR / "classes"


class TerraformService:
    """Service for managing VM deployments via Terraform."""
    
    def __init__(self):
        """Initialize Terraform service."""
        self.terraform_dir = TERRAFORM_DIR
        self.classes_dir = TERRAFORM_CLASSES_DIR
        self.classes_dir.mkdir(exist_ok=True)
        
    def deploy_class_vms(
        self,
        class_id: str,
        class_prefix: str,
        students: List[str],
        template_name: str,
        use_full_clone: bool = False,
        create_teacher_vm: bool = True,
        create_teacher_template: bool = True,
        node_distribution: Optional[List[str]] = None,
        target_node: Optional[str] = None,
        auto_select_best_node: bool = True,
    ) -> Dict:
        """Deploy VMs for a class using Terraform.
        
        Args:
            class_id: Unique class identifier
            class_prefix: Prefix for VM names (e.g., 'cs101')
            students: List of student usernames
            template_name: Proxmox template name to clone (uses template's CPU/RAM/disk)
            use_full_clone: Use full clone instead of linked
            create_teacher_vm: Create separate teacher VM (editable)
            create_teacher_template: Create teacher template VM (converted to template for students)
            node_distribution: List of nodes to distribute across
            target_node: Deploy all to this node (overrides distribution)
            auto_select_best_node: Query Proxmox for least loaded nodes
            
        Returns:
            Dict with VM information (IDs, IPs, etc.)
            
        Raises:
            RuntimeError: If Terraform apply fails
        """
        logger.info(f"Deploying class {class_id} with {len(students)} students via Terraform")
        
        # Auto-select best nodes if requested and no specific node set
        if auto_select_best_node and not target_node and not node_distribution:
            node_distribution = self._get_best_nodes()
            logger.info(f"Auto-selected nodes based on load: {node_distribution}")
        
        # Create class workspace directory
        class_dir = self.classes_dir / class_id
        class_dir.mkdir(exist_ok=True)
        
        # Copy Terraform files to class directory
        self._copy_terraform_files(class_dir)
        
        # Generate tfvars
        tfvars = self._generate_tfvars(
            class_id=class_id,
            class_prefix=class_prefix,
            students=students,
            template_name=template_name,
            use_full_clone=use_full_clone,
            create_teacher_vm=create_teacher_vm,
            create_teacher_template=create_teacher_template,
            node_distribution=node_distribution,
            target_node=target_node,
        )
        
        # Write tfvars to file
        tfvars_path = class_dir / "terraform.tfvars.json"
        with open(tfvars_path, 'w') as f:
            json.dump(tfvars, f, indent=2)
        
        logger.info(f"Generated Terraform config in {class_dir}")
        
        # Initialize Terraform (if not already initialized)
        self._terraform_init(class_dir)
        
        # Apply configuration
        self._terraform_apply(class_dir)
        
        # Get outputs
        outputs = self._terraform_output(class_dir)
        
        # Convert teacher template VM to Proxmox template
        if create_teacher_template and outputs.get('teacher_template_vm'):
            teacher_template = outputs['teacher_template_vm']
            logger.info(f"Converting teacher template VM {teacher_template['id']} to Proxmox template")
            self._convert_vm_to_template(
                vmid=teacher_template['id'],
                node=teacher_template['node']
            )
            logger.info(f"Teacher template VM {teacher_template['id']} converted to template")
        
        logger.info(f"Successfully deployed {len(students)} VMs for class {class_id}")
        return outputs
    
    def destroy_class_vms(self, class_id: str) -> None:
        """Destroy all VMs for a class.
        
        Args:
            class_id: Class identifier
            
        Raises:
            RuntimeError: If Terraform destroy fails
        """
        logger.info(f"Destroying VMs for class {class_id}")
        
        class_dir = self.classes_dir / class_id
        if not class_dir.exists():
            raise ValueError(f"Class {class_id} Terraform config not found")
        
        self._terraform_destroy(class_dir)
        logger.info(f"Successfully destroyed VMs for class {class_id}")
    
    def get_class_vms(self, class_id: str) -> Dict:
        """Get VM information for a class.
        
        Args:
            class_id: Class identifier
            
        Returns:
            Dict with VM information
        """
        class_dir = self.classes_dir / class_id
        if not class_dir.exists():
            return {}
        
        return self._terraform_output(class_dir)
    
    def _get_best_nodes(self) -> List[str]:
        """Query Proxmox to find nodes with most available resources.
        
        Returns:
            List of node names sorted by available resources (best first)
        """
        try:
            from app.services.proxmox_service import get_proxmox_admin
            
            proxmox = get_proxmox_admin()
            nodes_data = proxmox.nodes.get()
            
            # Calculate resource score for each node (lower is better load)
            node_scores = []
            for node in nodes_data:
                name = node.get('node')
                status = node.get('status')
                
                if status != 'online':
                    continue
                
                # Get resource usage percentages
                cpu_usage = node.get('cpu', 0) * 100  # Convert to percentage
                mem_total = node.get('maxmem', 1)
                mem_used = node.get('mem', 0)
                mem_usage = (mem_used / mem_total * 100) if mem_total > 0 else 100
                
                # Calculate availability score (higher = more available)
                cpu_available = 100 - cpu_usage
                mem_available = 100 - mem_usage
                
                # Weighted score: 60% CPU, 40% memory
                score = (cpu_available * 0.6) + (mem_available * 0.4)
                
                node_scores.append((name, score))
                logger.debug(f"Node {name}: CPU {cpu_usage:.1f}%, RAM {mem_usage:.1f}%, Score {score:.1f}")
            
            # Sort by score (descending - best first)
            node_scores.sort(key=lambda x: x[1], reverse=True)
            
            # Return node names
            return [name for name, score in node_scores]
            
        except Exception as e:
            logger.warning(f"Failed to query node resources: {e}, using default nodes")
            from app.config import CLUSTERS
            # Fallback to configured nodes
            if CLUSTERS:
                # Try to get nodes from first cluster
                try:
                    proxmox = get_proxmox_admin()
                    nodes = proxmox.nodes.get()
                    return [n['node'] for n in nodes if n.get('status') == 'online']
                except:
                    pass
            return ["pve1", "pve2"]  # Hard-coded fallback
    
    def _copy_terraform_files(self, dest_dir: Path) -> None:
        """Copy main Terraform files to class directory."""
        import shutil
        
        files = ["main.tf", "variables.tf"]
        for file in files:
            src = self.terraform_dir / file
            dst = dest_dir / file
            if src.exists():
                shutil.copy2(src, dst)
    
    def _generate_tfvars(
        self,
        class_id: str,
        class_prefix: str,
        students: List[str],
        template_name: str,
        use_full_clone: bool,
        create_teacher_vm: bool,
        create_teacher_template: bool,
        node_distribution: Optional[List[str]],
        target_node: Optional[str],
    ) -> Dict:
        """Generate Terraform variables."""
        from app.config import CLUSTERS, PVE_ADMIN_USER, PVE_ADMIN_PASS, PVE_HOST
        
        # Get Proxmox credentials from config
        proxmox_url = f"https://{PVE_HOST}:8006/api2/json"
        
        # Build students map
        students_map = {
            username: {"role": "default"}
            for username in students
        }
        
        tfvars = {
            # Proxmox connection
            "proxmox_api_url": proxmox_url,
            "proxmox_user": PVE_ADMIN_USER,
            "proxmox_password": PVE_ADMIN_PASS,
            "proxmox_tls_insecure": True,
            
            # Class config
            "class_id": str(class_id),
            "class_prefix": class_prefix,
            "students": students_map,
            
            # Template (VMs inherit all specs from template)
            "template_name": template_name,
            "use_full_clone": use_full_clone,
            
            # Teacher VMs
            "create_teacher_vm": create_teacher_vm,
            "create_teacher_template": create_teacher_template,
        }
        
        # Node distribution
        if target_node:
            tfvars["target_node"] = target_node
        elif node_distribution:
            tfvars["node_distribution"] = node_distribution
        
        return tfvars
    
    def _terraform_init(self, working_dir: Path) -> None:
        """Initialize Terraform in directory."""
        logger.info(f"Running terraform init in {working_dir}")
        
        result = subprocess.run(
            ["terraform", "init"],
            cwd=working_dir,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            logger.error(f"Terraform init failed: {result.stderr}")
            raise RuntimeError(f"Terraform init failed: {result.stderr}")
    
    def _terraform_apply(self, working_dir: Path) -> None:
        """Apply Terraform configuration."""
        logger.info(f"Running terraform apply in {working_dir}")
        
        result = subprocess.run(
            ["terraform", "apply", "-auto-approve"],
            cwd=working_dir,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            logger.error(f"Terraform apply failed: {result.stderr}")
            raise RuntimeError(f"Terraform apply failed: {result.stderr}")
        
        logger.info(f"Terraform apply output: {result.stdout}")
    
    def _terraform_destroy(self, working_dir: Path) -> None:
        """Destroy Terraform-managed resources."""
        logger.info(f"Running terraform destroy in {working_dir}")
        
        result = subprocess.run(
            ["terraform", "destroy", "-auto-approve"],
            cwd=working_dir,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            logger.error(f"Terraform destroy failed: {result.stderr}")
            raise RuntimeError(f"Terraform destroy failed: {result.stderr}")
    
    def _terraform_output(self, working_dir: Path) -> Dict:
        """Get Terraform outputs."""
        result = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=working_dir,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            logger.warning(f"Terraform output failed: {result.stderr}")
            return {}
        
        try:
            outputs = json.loads(result.stdout)
            # Extract values from Terraform output format
            return {
                key: val.get("value") 
                for key, val in outputs.items()
            }
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse Terraform output: {result.stdout}")
            return {}
    
    def _convert_vm_to_template(self, vmid: int, node: str) -> None:
        """Convert a VM to a Proxmox template.
        
        Args:
            vmid: VM ID to convert
            node: Node where VM is located
        """
        from app.services.proxmox_service import get_proxmox_admin
        
        try:
            proxmox = get_proxmox_admin()
            
            # Stop VM if running
            try:
                status = proxmox.nodes(node).qemu(vmid).status.current.get()
                if status.get('status') == 'running':
                    logger.info(f"Stopping VM {vmid} before template conversion")
                    proxmox.nodes(node).qemu(vmid).status.stop.post()
                    import time
                    time.sleep(5)  # Wait for VM to stop
            except Exception as e:
                logger.warning(f"Failed to check/stop VM {vmid}: {e}")
            
            # Convert to template
            proxmox.nodes(node).qemu(vmid).template.post()
            logger.info(f"VM {vmid} converted to template")
            
            # Verify conversion
            import time
            time.sleep(3)
            config = proxmox.nodes(node).qemu(vmid).config.get()
            if config.get('template') == 1:
                logger.info(f"Verified VM {vmid} is now a template")
            else:
                logger.warning(f"VM {vmid} template flag not set after conversion")
                
        except Exception as e:
            logger.error(f"Failed to convert VM {vmid} to template: {e}")
            raise


# Singleton instance
_terraform_service = None


def get_terraform_service() -> TerraformService:
    """Get singleton Terraform service instance."""
    global _terraform_service
    if _terraform_service is None:
        _terraform_service = TerraformService()
    return _terraform_service


# Convenience functions
def deploy_class_with_terraform(
    class_id: str,
    class_prefix: str,
    students: List[str],
    template_name: str,
    **kwargs
) -> Dict:
    """Deploy class VMs using Terraform (convenience function)."""
    service = get_terraform_service()
    return service.deploy_class_vms(
        class_id=class_id,
        class_prefix=class_prefix,
        students=students,
        template_name=template_name,
        **kwargs
    )


def destroy_class_vms(class_id: str) -> None:
    """Destroy class VMs using Terraform (convenience function)."""
    service = get_terraform_service()
    return service.destroy_class_vms(class_id)
