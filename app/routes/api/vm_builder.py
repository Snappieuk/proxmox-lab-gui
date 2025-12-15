"""
VM Builder API endpoints - Build VMs from scratch with full configuration
"""

import logging
import threading
from flask import Blueprint, jsonify, request, session

from app.utils.decorators import login_required
from app.services.user_manager import require_user, is_admin_user
from app.services.class_service import get_user_by_username
from app.services.vm_deployment_service import build_vm_from_scratch, list_available_isos

logger = logging.getLogger(__name__)

vm_builder_bp = Blueprint('vm_builder', __name__, url_prefix='/api/vm-builder')

# Log blueprint registration
logger.info(f"VM Builder blueprint created with prefix: /api/vm-builder")


@vm_builder_bp.before_request
def log_request():
    """Log all requests to this blueprint."""
    logger.info(f"[VM Builder Blueprint] Request: {request.method} {request.path}")
    logger.info(f"[VM Builder Blueprint] Full URL: {request.url}")


@vm_builder_bp.route("/test", methods=["GET"])
def test_endpoint():
    """Test endpoint to verify blueprint is working."""
    return jsonify({"ok": True, "message": "VM Builder blueprint is working"}), 200


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
        vm_name = data.get('vm_name')
        iso_file = data.get('iso_file')
        os_type = data.get('os_type', 'ubuntu')
        
        if not all([cluster_id, vm_name, iso_file]):
            return jsonify({
                "ok": False,
                "error": "Missing required parameters: cluster_id, vm_name, iso_file"
            }), 400
        
        # Hardware configuration
        cpu_cores = data.get('cpu_cores', 2)
        cpu_sockets = data.get('cpu_sockets', 1)
        memory_mb = data.get('memory_mb', 2048)
        disk_size_gb = data.get('disk_size_gb', 32)
        
        # Network configuration
        network_bridge = data.get('network_bridge', 'vmbr0')
        
        # Storage configuration
        storage = data.get('storage', 'local-lvm')
        
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
        
        # Optional: node hint from frontend (if ISO data includes node)
        target_node = data.get('target_node')
        
        logger.info(f"Building VM: {vm_name} on cluster {cluster_id} with ISO {iso_file}")
        if target_node:
            logger.info(f"Frontend provided target node: {target_node}")
        
        # Build the VM in a background thread to avoid gateway timeout
        # Return immediately with success message
        def build_vm_background():
            """Background thread to build VM without blocking the request."""
            try:
                from flask import current_app
                app = current_app._get_current_object()
                
                with app.app_context():
                    logger.info(f"[Background] Starting VM build: {vm_name} (VMID will be auto-assigned)")
                    
                    success, vmid, message = build_vm_from_scratch(
                        cluster_id=cluster_id,
                        vm_name=vm_name,
                        iso_file=iso_file,
                        os_type=os_type,
                        cpu_cores=cpu_cores,
                        cpu_sockets=cpu_sockets,
                        memory_mb=memory_mb,
                        disk_size_gb=disk_size_gb,
                        network_bridge=network_bridge,
                        storage=storage,
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
                        convert_to_template=convert_to_template,
                        target_node=target_node
                    )
                    
                    if success:
                        logger.info(f"[Background] VM {vmid} created successfully: {message}")
                        
                        # Trigger immediate background sync
                        try:
                            from app.services.background_sync import trigger_immediate_sync
                            trigger_immediate_sync()
                        except Exception as sync_err:
                            logger.warning(f"Failed to trigger sync: {sync_err}")
                        
                        # Create VM assignment
                        if local_user:
                            try:
                                from app.models import VMAssignment, VMInventory, db
                                
                                vm_record = VMInventory.query.filter_by(cluster_id=cluster_id, vmid=vmid).first()
                                mac_address = vm_record.mac_address if vm_record else None
                                node = vm_record.node if vm_record else None
                                
                                if not node:
                                    try:
                                        from app.services.proxmox_service import get_proxmox_admin_for_cluster
                                        proxmox = get_proxmox_admin_for_cluster(cluster_id)
                                        resources = proxmox.cluster.resources.get(type="vm")
                                        for r in resources:
                                            if int(r.get('vmid', -1)) == int(vmid):
                                                node = r.get('node')
                                                break
                                    except Exception as node_err:
                                        logger.warning(f"Failed to query node: {node_err}")
                                
                                assignment = VMAssignment(
                                    proxmox_vmid=vmid,
                                    vm_name=vm_name,
                                    assigned_user_id=local_user.id,
                                    status='available',
                                    mac_address=mac_address,
                                    node=node,
                                    class_id=None
                                )
                                db.session.add(assignment)
                                db.session.commit()
                                logger.info(f"[Background] Created VM assignment for {local_user.username} -> VM {vmid}")
                            except Exception as e:
                                logger.warning(f"Failed to create assignment: {e}")
                    else:
                        logger.error(f"[Background] VM build failed: {message}")
                        
            except Exception as e:
                logger.error(f"[Background] Exception during VM build: {e}", exc_info=True)
        
        # Start background thread
        from flask import current_app
        app = current_app._get_current_object()
        threading.Thread(target=build_vm_background, daemon=True).start()
        
        # Return immediately
        return jsonify({
            "ok": True,
            "message": f"VM '{vm_name}' creation started in background. This will take 1-2 minutes. Check the portal for the new VM.",
            "vm_name": vm_name,
            "cluster_id": cluster_id
        })
            
    except Exception as e:
        logger.error(f"Failed to build VM: {e}", exc_info=True)
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@vm_builder_bp.route("/clone-from-template", methods=["POST"])
@login_required
def api_clone_from_template():
    """Clone a VM from an existing template."""
    try:
        user = require_user()
        
        # Check if user is teacher or admin
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            return jsonify({
                "ok": False,
                "error": "Access denied. Only teachers and administrators can clone VMs."
            }), 403
        
        data = request.json
        
        # Required parameters
        cluster_id = data.get('cluster_id') or session.get('cluster_id')
        vm_name = data.get('vm_name')
        template_vmid = data.get('template_vmid')
        target_node = data.get('target_node')
        
        if not all([cluster_id, vm_name, template_vmid]):
            return jsonify({
                "ok": False,
                "error": "Missing required parameters: cluster_id, vm_name, template_vmid"
            }), 400
        
        # Storage configuration
        storage = data.get('storage', 'TRUENAS-NFS')
        
        # Template options
        convert_to_template = data.get('convert_to_template', False)
        
        logger.info(f"Cloning VM from template {template_vmid}: {vm_name} on cluster {cluster_id}")
        if target_node:
            logger.info(f"Target node: {target_node}")
        
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        from app.services.vm_utils import get_next_available_vmid
        from app.models import VMAssignment, db
        from app.services.background_sync import trigger_immediate_sync
        import time
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return jsonify({
                "ok": False,
                "error": f"Failed to connect to cluster {cluster_id}"
            }), 500
        
        # Get next available VMID
        new_vmid = get_next_available_vmid(proxmox)
        logger.info(f"Allocated VMID: {new_vmid}")
        
        # If no target node specified, find the node where template exists
        if not target_node:
            try:
                resources = proxmox.cluster.resources.get(type='vm')
                for resource in resources:
                    if int(resource.get('vmid', -1)) == int(template_vmid):
                        target_node = resource.get('node')
                        logger.info(f"Found template on node: {target_node}")
                        break
                
                if not target_node:
                    return jsonify({
                        "ok": False,
                        "error": f"Template {template_vmid} not found in cluster"
                    }), 404
            except Exception as e:
                logger.error(f"Failed to locate template: {e}")
                return jsonify({
                    "ok": False,
                    "error": f"Failed to locate template: {str(e)}"
                }), 500
        
        try:
            # Clone the template using Proxmox API
            # Full clone with target storage
            clone_params = {
                'newid': new_vmid,
                'name': vm_name,
                'full': 1,  # Full clone (not linked)
                'storage': storage,
                'target': target_node
            }
            
            logger.info(f"Cloning template {template_vmid} to VM {new_vmid} with params: {clone_params}")
            
            task_upid = proxmox.nodes(target_node).qemu(template_vmid).clone.post(**clone_params)
            logger.info(f"Clone task started: {task_upid}")
            
            # Wait for clone to complete (can take a few minutes)
            import time
            max_wait = 300  # 5 minutes
            wait_interval = 2
            elapsed = 0
            
            while elapsed < max_wait:
                try:
                    task_status = proxmox.nodes(target_node).tasks(task_upid).status.get()
                    status = task_status.get('status')
                    
                    if status == 'stopped':
                        exitstatus = task_status.get('exitstatus')
                        if exitstatus == 'OK':
                            logger.info(f"Clone completed successfully: {task_upid}")
                            break
                        else:
                            error_msg = f"Clone task failed with status: {exitstatus}"
                            logger.error(error_msg)
                            return jsonify({
                                "ok": False,
                                "error": error_msg
                            }), 500
                except Exception as e:
                    logger.warning(f"Failed to check task status: {e}")
                
                time.sleep(wait_interval)
                elapsed += wait_interval
            
            if elapsed >= max_wait:
                return jsonify({
                    "ok": False,
                    "error": "Clone operation timed out (5 minutes)"
                }), 500
            
            # Convert to template if requested
            if convert_to_template:
                logger.info(f"Converting VM {new_vmid} to template")
                proxmox.nodes(target_node).qemu(new_vmid).template.post()
                message = f"VM {new_vmid} cloned and converted to template successfully"
            else:
                message = f"VM {new_vmid} cloned successfully"
            
            # Get MAC address for inventory
            mac_address = None
            try:
                vm_config = proxmox.nodes(target_node).qemu(new_vmid).config.get()
                net0 = vm_config.get('net0', '')
                if net0:
                    for part in net0.split(','):
                        if '=' in part:
                            key, value = part.split('=', 1)
                            if key.strip().lower() in ['macaddr', 'hwaddr']:
                                mac_address = value.strip()
                                break
            except Exception as e:
                logger.warning(f"Failed to get MAC address: {e}")
            
            # Trigger immediate background sync
            try:
                logger.info("Triggering immediate background sync for VMInventory update")
                trigger_immediate_sync()
                time.sleep(1)  # Give sync a moment to process
            except Exception as sync_err:
                logger.warning(f"Failed to trigger sync: {sync_err}")
            
            # Create VM assignment for builder VM
            if local_user:
                try:
                    # Query VMInventory for node if not yet populated
                    node = target_node
                    if not node:
                        try:
                            resources = proxmox.cluster.resources.get(type='vm')
                            for r in resources:
                                if int(r.get('vmid', -1)) == int(new_vmid):
                                    node = r.get('node')
                                    logger.info(f"Found node {node} for VM {new_vmid} via cluster resources")
                                    break
                        except Exception as node_err:
                            logger.warning(f"Failed to query node for VM {new_vmid}: {node_err}")
                    
                    assignment = VMAssignment(
                        proxmox_vmid=new_vmid,
                        vm_name=vm_name,
                        assigned_user_id=local_user.id,
                        status='available',
                        mac_address=mac_address,
                        node=node,
                        class_id=None  # Direct assignment = builder VM
                    )
                    db.session.add(assignment)
                    db.session.commit()
                    logger.info(f"Created VM assignment for user {local_user.username} -> VM {new_vmid}")
                except Exception as e:
                    logger.warning(f"Failed to create VM assignment: {e}")
            
            return jsonify({
                "ok": True,
                "vmid": new_vmid,
                "message": message
            })
            
        except Exception as clone_error:
            logger.error(f"Clone operation failed: {clone_error}", exc_info=True)
            return jsonify({
                "ok": False,
                "error": f"Clone failed: {str(clone_error)}"
            }), 500
            
    except Exception as e:
        logger.error(f"Failed to clone from template: {e}", exc_info=True)
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@vm_builder_bp.route("/isos", methods=["GET"])
@login_required
def api_list_isos():
    """List available ISO images across all nodes."""
    try:
        user = require_user()
        logger.info(f"ISO list request from user: {user}")
        
        # Check if user is teacher or admin
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            logger.warning(f"Access denied for user {user} - not teacher/admin")
            return jsonify({
                "ok": False,
                "error": "Access denied"
            }), 403
        
        cluster_id = request.args.get('cluster_id') or session.get('cluster_id')
        logger.info(f"Listing ISOs for cluster_id: {cluster_id}")
        
        if not cluster_id:
            logger.error("No cluster_id provided in request or session - user may not have configured a cluster yet")
            return jsonify({
                "ok": False,
                "error": "No cluster configured. Please configure a cluster via /setup or /admin/settings first."
            }), 400
        
        logger.info(f"Calling list_available_isos for cluster {cluster_id}")
        success, isos, message = list_available_isos(cluster_id)
        logger.info(f"list_available_isos returned: success={success}, iso_count={len(isos)}, message={message}")
        
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


@vm_builder_bp.route("/upload-iso", methods=["POST"])
@login_required
def api_upload_iso():
    """Upload an ISO file to Proxmox storage."""
    logger.info(f"=== ISO UPLOAD ENDPOINT HIT === Method: {request.method}")
    
    try:
        user = require_user()
        logger.info(f"ISO upload request from user: {user}")
        
        # Check if user is teacher or admin
        is_admin = is_admin_user(user)
        username = user.split('@')[0] if '@' in user else user
        local_user = get_user_by_username(username)
        
        if not is_admin and (not local_user or local_user.role not in ['teacher', 'adminer']):
            return jsonify({
                "ok": False,
                "error": "Access denied. Only teachers and administrators can upload ISOs."
            }), 403
        
        # Get parameters
        node = request.form.get('node')
        storage = request.form.get('storage', 'local')
        
        if not node:
            return jsonify({
                "ok": False,
                "error": "Missing node parameter"
            }), 400
        
        # Get uploaded file
        if 'content' not in request.files:
            return jsonify({
                "ok": False,
                "error": "No file uploaded"
            }), 400
        
        file = request.files['content']
        
        if file.filename == '':
            return jsonify({
                "ok": False,
                "error": "No file selected"
            }), 400
        
        if not file.filename.lower().endswith('.iso'):
            return jsonify({
                "ok": False,
                "error": "File must be an ISO image (.iso extension)"
            }), 400
        
        from app.services.proxmox_service import get_proxmox_admin_for_cluster
        
        cluster_id = request.form.get('cluster_id') or session.get('cluster_id')
        
        if not cluster_id:
            return jsonify({
                "ok": False,
                "error": "No cluster selected"
            }), 400
        
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        if not proxmox:
            return jsonify({
                "ok": False,
                "error": "Failed to connect to cluster"
            }), 500
        
        # Verify storage exists and is available on the node
        try:
            storages = proxmox.nodes(node).storage.get()
            storage_found = False
            for s in storages:
                # Check storage name and that it's not explicitly unavailable
                if s['storage'] == storage:
                    status = s.get('status')
                    if status and status == 'unavailable':
                        continue  # Skip if explicitly unavailable
                    
                    storage_found = True
                    # Check if storage supports ISO content
                    if 'iso' not in s.get('content', '').split(','):
                        return jsonify({
                            "ok": False,
                            "error": f"Storage '{storage}' does not support ISO content"
                        }), 400
                    break
            
            if not storage_found:
                return jsonify({
                    "ok": False,
                    "error": f"Storage '{storage}' is not available on node '{node}'. Please check Proxmox storage configuration."
                }), 400
        except Exception as e:
            logger.error(f"Failed to verify storage: {e}")
            return jsonify({
                "ok": False,
                "error": f"Failed to verify storage availability: {str(e)}"
            }), 500
        
        logger.info(f"Uploading ISO {file.filename} to {node}:{storage}")
        
        # Use streaming upload to avoid loading entire file into memory
        try:
            from app.models import Cluster
            import requests
            
            # Get cluster config for direct Proxmox upload
            cluster = Cluster.query.filter_by(id=cluster_id).first()
            if not cluster:
                raise Exception("Cluster configuration not found")
            
            # Build Proxmox upload URL
            upload_url = f"https://{cluster.host}:{cluster.port}/api2/json/nodes/{node}/storage/{storage}/upload"
            logger.info(f"Proxying upload directly to Proxmox: {upload_url}")
            
            # Stream the file directly to Proxmox without loading into memory
            files = {'filename': (file.filename, file.stream, 'application/octet-stream')}
            data = {'content': 'iso'}
            
            response = requests.post(
                upload_url,
                auth=(cluster.user, cluster.password),
                files=files,
                data=data,
                verify=cluster.verify_ssl,
                timeout=(30, 3600),  # 1 hour timeout for large ISOs
                stream=False
            )
            
            if response.status_code != 200:
                error_text = response.text[:500]
                logger.error(f"Proxmox upload failed: {response.status_code} - {error_text}")
                raise Exception(f"Proxmox API error {response.status_code}: {error_text}")
            
            result = response.json()
            logger.info(f"Upload successful: {result}")
            file_size = file.stream.tell()  # Get uploaded size
            
            # Cache the uploaded ISO in database immediately
            try:
                from app.models import ISOImage, db
                from datetime import datetime
                
                # Build volid (format: storage:iso/filename.iso)
                volid = f"{storage}:iso/{file.filename}"
                
                # Check if already exists
                existing = ISOImage.query.filter_by(volid=volid).first()
                if existing:
                    existing.last_seen = datetime.utcnow()
                    existing.node = node
                    existing.storage = storage
                    existing.size = file_size
                    logger.info(f"Updated existing ISO cache entry: {volid}")
                else:
                    new_iso = ISOImage(
                        volid=volid,
                        name=file.filename,
                        size=file_size,
                        node=node,
                        storage=storage,
                        cluster_id=cluster_id
                    )
                    db.session.add(new_iso)
                    logger.info(f"Created new ISO cache entry: {volid}")
                
                db.session.commit()
            except Exception as db_error:
                logger.warning(f"Failed to cache uploaded ISO: {db_error}")
                # Don't fail the upload if caching fails
                db.session.rollback()
            
            return jsonify({
                "ok": True,
                "message": f"ISO {file.filename} uploaded successfully to {node}:{storage}",
                "filename": file.filename
            })
            
        except Exception as upload_error:
            logger.error(f"Proxmox upload failed: {upload_error}", exc_info=True)
            return jsonify({
                "ok": False,
                "error": f"Upload to Proxmox failed: {str(upload_error)}"
            }), 500
        
    except Exception as e:
        logger.error(f"Failed to upload ISO: {e}", exc_info=True)
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500
