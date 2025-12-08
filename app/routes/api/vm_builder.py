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
        
        logger.info(f"Building VM: {vm_name} on cluster {cluster_id} with ISO {iso_file}")
        
        # Build the VM (node will be auto-detected from ISO location)
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
    """List available ISO images across all nodes."""
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
        
        if not cluster_id:
            return jsonify({
                "ok": False,
                "error": "Missing required parameter: cluster_id"
            }), 400
        
        success, isos, message = list_available_isos(cluster_id)
        
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
    try:
        user = require_user()
        
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
        
        logger.info(f"Uploading ISO {file.filename} to {node}:{storage}")
        
        # Get file size for logging
        file.stream.seek(0, 2)  # Seek to end
        file_size = file.stream.tell()
        file.stream.seek(0)  # Reset to beginning
        logger.info(f"ISO file size: {file_size / (1024*1024):.2f} MB")
        
        # Upload file to Proxmox storage
        # Note: The ProxmoxAPI library's upload method can be unreliable for large files
        # We need to use the storage content upload endpoint properly
        try:
            # Proxmox expects the file upload via the storage content endpoint
            # Format: POST /nodes/{node}/storage/{storage}/upload
            # with multipart form data containing 'content' and 'filename'
            
            import requests
            from requests.adapters import HTTPAdapter
            from requests.packages.urllib3.util.retry import Retry
            from app.models import Cluster
            
            # Find cluster config from database
            cluster = Cluster.query.filter_by(id=cluster_id).first()
            
            if not cluster:
                raise Exception("Cluster configuration not found")
            
            # Build Proxmox API URL
            api_url = f"https://{cluster.host}:{cluster.port}/api2/json/nodes/{node}/storage/{storage}/upload"
            
            # Create a session with retry logic
            session_obj = requests.Session()
            retry_strategy = Retry(
                total=3,
                status_forcelist=[429, 500, 502, 503, 504],
                method_whitelist=["POST"],
                backoff_factor=1
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session_obj.mount("https://", adapter)
            session_obj.mount("http://", adapter)
            
            # Prepare the file for upload (don't read entire file into memory)
            file.stream.seek(0)
            files = {
                'content': (file.filename, file.stream, 'application/octet-stream')
            }
            
            # Prepare form data
            data = {
                'filename': file.filename,
                'content': 'iso'
            }
            
            # Make request with authentication and extended timeout
            auth = (cluster.user, cluster.password)
            verify_ssl = cluster.verify_ssl
            
            logger.info(f"Starting upload to {api_url}")
            response = session_obj.post(
                api_url,
                auth=auth,
                files=files,
                data=data,
                verify=verify_ssl,
                timeout=(30, 1800),  # (connect timeout, read timeout) - 30 min read timeout
                stream=False
            )
            
            if response.status_code != 200:
                error_text = response.text[:500]  # Limit error text
                logger.error(f"Proxmox API error {response.status_code}: {error_text}")
                raise Exception(f"Proxmox API returned status {response.status_code}: {error_text}")
            
            result = response.json()
            logger.info(f"ISO uploaded successfully: {result}")
            
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
                    logger.info(f"Updated existing ISO cache entry: {volid}")
                else:
                    new_iso = ISOImage(
                        volid=volid,
                        name=file.filename,
                        size=0,  # Size not returned by upload API
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
            logger.error(f"Proxmox upload failed: {upload_error}")
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
