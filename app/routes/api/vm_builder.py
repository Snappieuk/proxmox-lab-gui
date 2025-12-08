"""
VM Builder API endpoints - Build VMs from scratch with full configuration
"""

import logging
from flask import Blueprint, jsonify, request, session

from app.utils.decorators import login_required
from app.services.user_manager import require_user, is_admin_user
from app.services.class_service import get_user_by_username
from app.services.vm_deployment_service import build_vm_from_scratch, list_available_isos

logger = logging.getLogger(__name__)

vm_builder_bp = Blueprint('vm_builder', __name__, url_prefix='/api/vm-builder')


@vm_builder_bp.route("/build", methods=["POST"])
@login_required
def api_build_vm():
    """Build a VM from scratch with full configuration."""
    try:
        user = require_user()
        
        # Check if user is teacher or admin
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            return jsonify({
                "ok": False,
                "error": "Access denied. Only teachers and administrators can build VMs."
            }), 403
        
        data = request.json
        
        # Required parameters
        cluster_id = data.get('cluster_id') or session.get('cluster_id')
        node = data.get('node')
        vm_name = data.get('vm_name')
        os_type = data.get('os_type', 'ubuntu')
        
        if not all([cluster_id, node, vm_name]):
            return jsonify({
                "ok": False,
                "error": "Missing required parameters: cluster_id, node, vm_name"
            }), 400
        
        # Hardware configuration
        cpu_cores = data.get('cpu_cores', 2)
        cpu_sockets = data.get('cpu_sockets', 1)
        memory_mb = data.get('memory_mb', 2048)
        disk_size_gb = data.get('disk_size_gb', 32)
        
        # Network configuration
        network_bridge = data.get('network_bridge', 'vmbr0')
        use_dhcp = data.get('use_dhcp', True)
        ip_address = data.get('ip_address')
        netmask = data.get('netmask')
        gateway = data.get('gateway')
        dns_servers = data.get('dns_servers')
        
        # Cloud-init configuration
        username_ci = data.get('username')
        password = data.get('password')
        ssh_key = data.get('ssh_key')
        
        # Storage configuration
        storage = data.get('storage', 'local-lvm')
        iso_storage = data.get('iso_storage', 'local')
        iso_file = data.get('iso_file')
        
        # Advanced hardware options
        cpu_type = data.get('cpu_type', 'host')
        cpu_flags = data.get('cpu_flags')
        cpu_limit = data.get('cpu_limit')
        numa_enabled = data.get('numa_enabled', False)
        bios_type = data.get('bios_type', 'seabios')
        machine_type = data.get('machine_type', 'pc')
        vga_type = data.get('vga_type', 'std')
        boot_order = data.get('boot_order', 'cdn')
        scsihw = data.get('scsihw', 'virtio-scsi-pci')
        tablet = data.get('tablet', True)
        agent_enabled = data.get('agent_enabled', True)
        onboot = data.get('onboot', False)
        
        # Template options
        convert_to_template = data.get('convert_to_template', False)
        
        logger.info(f"Building VM: {vm_name} on cluster {cluster_id}, node {node}")
        
        # Build the VM
        success, vmid, message = build_vm_from_scratch(
            cluster_id=cluster_id,
            node=node,
            vm_name=vm_name,
            os_type=os_type,
            cpu_cores=cpu_cores,
            cpu_sockets=cpu_sockets,
            memory_mb=memory_mb,
            disk_size_gb=disk_size_gb,
            network_bridge=network_bridge,
            use_dhcp=use_dhcp,
            ip_address=ip_address,
            netmask=netmask,
            gateway=gateway,
            dns_servers=dns_servers,
            username=username_ci,
            password=password,
            ssh_key=ssh_key,
            storage=storage,
            iso_storage=iso_storage,
            iso_file=iso_file,
            cpu_type=cpu_type,
            cpu_flags=cpu_flags,
            cpu_limit=cpu_limit,
            numa_enabled=numa_enabled,
            bios_type=bios_type,
            machine_type=machine_type,
            vga_type=vga_type,
            boot_order=boot_order,
            scsihw=scsihw,
            tablet=tablet,
            agent_enabled=agent_enabled,
            onboot=onboot,
            convert_to_template=convert_to_template
        )
        
        if success:
            return jsonify({
                "ok": True,
                "vmid": vmid,
                "message": message
            })
        else:
            return jsonify({
                "ok": False,
                "error": message,
                "vmid": vmid
            }), 500
            
    except Exception as e:
        logger.error(f"Failed to build VM: {e}", exc_info=True)
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@vm_builder_bp.route("/isos", methods=["GET"])
@login_required
def api_list_isos():
    """List available ISO images."""
    try:
        user = require_user()
        
        # Check if user is teacher or admin
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            return jsonify({
                "ok": False,
                "error": "Access denied"
            }), 403
        
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        node = request.args.get('node')
        storage = request.args.get('storage', 'local')
        
        if not cluster_id or not node:
            return jsonify({
                "ok": False,
                "error": "Missing required parameters: cluster_id, node"
            }), 400
        
        success, isos, message = list_available_isos(cluster_id, node, storage)
        
        if success:
            return jsonify({
                "ok": True,
                "isos": isos
            })
        else:
            return jsonify({
                "ok": False,
                "error": message
            }), 500
            
    except Exception as e:
        logger.error(f"Failed to list ISOs: {e}")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@vm_builder_bp.route("/nodes", methods=["GET"])
@login_required
def api_list_nodes():
    """List available nodes in cluster."""
    try:
        user = require_user()
        
        # Check if user is teacher or admin
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            return jsonify({
                "ok": False,
                "error": "Access denied"
            }), 403
        
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        
        if not cluster_id:
            return jsonify({
                "ok": False,
                "error": "Missing cluster_id"
            }), 400
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return jsonify({
                "ok": False,
                "error": f"Failed to connect to cluster {cluster_id}"
            }), 500
        
        nodes = proxmox.nodes.get()
        
        node_list = []
        for node in nodes:
            node_list.append({
                'node': node['node'],
                'status': node.get('status'),
                'cpu': node.get('cpu'),
                'mem': node.get('mem'),
                'maxmem': node.get('maxmem'),
                'disk': node.get('disk'),
                'maxdisk': node.get('maxdisk')
            })
        
        return jsonify({
            "ok": True,
            "nodes": node_list
        })
        
    except Exception as e:
        logger.error(f"Failed to list nodes: {e}")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@vm_builder_bp.route("/storages", methods=["GET"])
@login_required
def api_list_storages():
    """List available storage pools."""
    try:
        user = require_user()
        
        # Check if user is teacher or admin
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            return jsonify({
                "ok": False,
                "error": "Access denied"
            }), 403
        
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        node = request.args.get('node')
        
        if not cluster_id or not node:
            return jsonify({
                "ok": False,
                "error": "Missing required parameters"
            }), 400
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return jsonify({
                "ok": False,
                "error": f"Failed to connect to cluster"
            }), 500
        
        storages = proxmox.nodes(node).storage.get()
        
        storage_list = []
        for storage in storages:
            if storage.get('enabled', True):
                storage_list.append({
                    'storage': storage['storage'],
                    'type': storage.get('type'),
                    'content': storage.get('content', '').split(','),
                    'avail': storage.get('avail'),
                    'total': storage.get('total'),
                    'used': storage.get('used')
                })
        
        return jsonify({
            "ok": True,
            "storages": storage_list
        })
        
    except Exception as e:
        logger.error(f"Failed to list storages: {e}")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500
