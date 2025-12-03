"""API routes for querying template information from the database."""

import logging
from datetime import datetime
from flask import Blueprint, jsonify, request, session
from app.models import Template, db
from app.utils.decorators import login_required
from app.config import CLUSTERS

logger = logging.getLogger(__name__)

bp = Blueprint("api_templates", __name__, url_prefix="/api/templates")


@bp.route("", methods=["GET"])
@login_required
def list_templates():
    """Get all templates with optional filtering.
    
    Query params:
    - cluster_ip: Filter by cluster IP
    - cluster_id: Filter by cluster ID (converted to IP)
    - node: Filter by node name
    - vmid: Filter by specific VMID
    - is_replica: Filter replicas (true/false)
    - is_class_template: Filter class templates (true/false)
    - class_id: Filter by class ID
    """
    query = Template.query
    
    # Filter by cluster
    cluster_ip = request.args.get("cluster_ip")
    cluster_id = request.args.get("cluster_id")
    if cluster_id:
        # Convert cluster_id to cluster_ip
        for cluster in CLUSTERS:
            if cluster["id"] == cluster_id:
                cluster_ip = cluster["host"]
                break
    if cluster_ip:
        query = query.filter_by(cluster_ip=cluster_ip)
    
    # Filter by node
    node = request.args.get("node")
    if node:
        query = query.filter_by(node=node)
    
    # Filter by VMID
    vmid = request.args.get("vmid")
    if vmid:
        try:
            query = query.filter_by(proxmox_vmid=int(vmid))
        except ValueError:
            pass
    
    # Filter by replica status
    is_replica = request.args.get("is_replica")
    if is_replica is not None:
        query = query.filter_by(is_replica=is_replica.lower() == "true")
    
    # Filter by class template status
    is_class_template = request.args.get("is_class_template")
    if is_class_template is not None:
        query = query.filter_by(is_class_template=is_class_template.lower() == "true")
    
    # Filter by class ID
    class_id = request.args.get("class_id")
    if class_id:
        try:
            query = query.filter_by(class_id=int(class_id))
        except ValueError:
            pass
    
    templates = query.all()
    return jsonify([t.to_dict() for t in templates])


@bp.route("/by-name/<template_name>", methods=["GET"])
@login_required
def get_template_by_name(template_name):
    """Get all instances of a template across nodes.
    
    Returns list of nodes where this template exists.
    """
    cluster_ip = request.args.get("cluster_ip")
    query = Template.query.filter_by(name=template_name)
    
    if cluster_ip:
        query = query.filter_by(cluster_ip=cluster_ip)
    
    templates = query.all()
    return jsonify([t.to_dict() for t in templates])


@bp.route("/nodes", methods=["GET"])
@login_required
def get_template_nodes():
    """Get all nodes that have templates for a specific template VMID.
    
    Query params:
    - vmid: Original template VMID (required)
    - cluster_ip: Cluster IP (optional)
    """
    vmid = request.args.get("vmid")
    if not vmid:
        return jsonify({"error": "vmid parameter required"}), 400
    
    try:
        vmid = int(vmid)
    except ValueError:
        return jsonify({"error": "vmid must be an integer"}), 400
    
    cluster_ip = request.args.get("cluster_ip")
    
    # Find the original template
    query = Template.query.filter_by(proxmox_vmid=vmid, is_replica=False)
    if cluster_ip:
        query = query.filter_by(cluster_ip=cluster_ip)
    
    original = query.first()
    if not original:
        return jsonify({"error": "Original template not found"}), 404
    
    # Find all replicas
    replicas = Template.query.filter_by(
        source_vmid=vmid,
        is_replica=True,
        cluster_ip=original.cluster_ip
    ).all()
    
    # Build node map
    nodes = {
        original.node: {
            "vmid": original.proxmox_vmid,
            "name": original.name,
            "is_original": True,
            "last_verified_at": original.last_verified_at.isoformat() if original.last_verified_at else None
        }
    }
    
    for replica in replicas:
        nodes[replica.node] = {
            "vmid": replica.proxmox_vmid,
            "name": replica.name,
            "is_original": False,
            "last_verified_at": replica.last_verified_at.isoformat() if replica.last_verified_at else None
        }
    
    return jsonify({
        "template_vmid": vmid,
        "template_name": original.name,
        "cluster_ip": original.cluster_ip,
        "nodes": nodes
    })


@bp.route("/stats", methods=["GET"])
@login_required
def get_template_stats():
    """Get statistics about templates in the database.
    
    Query params:
    - cluster_ip: Filter by cluster (optional)
    """
    cluster_ip = request.args.get("cluster_ip")
    query = Template.query
    
    if cluster_ip:
        query = query.filter_by(cluster_ip=cluster_ip)
    
    total = query.count()
    replicas = query.filter_by(is_replica=True).count()
    class_templates = query.filter_by(is_class_template=True).count()
    originals = query.filter_by(is_replica=False, is_class_template=False).count()
    
    # Get templates by node
    by_node = {}
    templates = query.all()
    for tpl in templates:
        node = tpl.node
        if node not in by_node:
            by_node[node] = 0
        by_node[node] += 1
    
    return jsonify({
        "total_templates": total,
        "original_templates": originals,
        "replica_templates": replicas,
        "class_templates": class_templates,
        "templates_by_node": by_node
    })


@bp.route("/sync", methods=["POST"])
@login_required
def sync_templates_from_proxmox():
    """Scan Proxmox cluster(s) and sync template list with specs to database.
    
    This fetches all templates from Proxmox and caches their specs (CPU, RAM, disk, etc.)
    in the database. Should be called periodically to keep template list fresh.
    
    Query params:
    - cluster_ip: Sync specific cluster (optional, defaults to all clusters)
    - force: Force re-sync even if recently synced (optional)
    """
    from app.services.proxmox_service import get_proxmox_admin_for_cluster
    
    cluster_filter = request.args.get("cluster_ip")
    force = request.args.get("force", "false").lower() == "true"
    
    synced_count = 0
    updated_count = 0
    errors = []
    
    # Determine which clusters to sync
    clusters_to_sync = []
    if cluster_filter:
        for cluster in CLUSTERS:
            if cluster["host"] == cluster_filter:
                clusters_to_sync.append(cluster)
                break
        if not clusters_to_sync:
            return jsonify({"ok": False, "error": f"Cluster {cluster_filter} not found"}), 404
    else:
        clusters_to_sync = CLUSTERS
    
    for cluster in clusters_to_sync:
        cluster_id = cluster["id"]
        cluster_ip = cluster["host"]
        
        try:
            proxmox = get_proxmox_admin_for_cluster(cluster_id)
            
            # Get all VMs/templates from cluster
            resources = proxmox.cluster.resources.get(type="vm")
            
            for resource in resources:
                # Only process templates
                if not resource.get("template"):
                    continue
                
                vmid = resource["vmid"]
                node = resource["node"]
                name = resource.get("name", f"template-{vmid}")
                
                # Check if template exists in database
                existing = Template.query.filter_by(
                    cluster_ip=cluster_ip,
                    node=node,
                    proxmox_vmid=vmid
                ).first()
                
                # Fetch detailed specs from Proxmox
                try:
                    vm_config = proxmox.nodes(node).qemu(vmid).config.get()
                    
                    # Extract specs
                    cpu_cores = vm_config.get("cores")
                    cpu_sockets = vm_config.get("sockets", 1)
                    memory_mb = vm_config.get("memory")
                    os_type = vm_config.get("ostype")
                    
                    # Extract disk info (check scsi0, virtio0, sata0, ide0)
                    disk_path = None
                    disk_storage = None
                    disk_size_gb = None
                    disk_format = None
                    
                    for disk_key in ["scsi0", "virtio0", "sata0", "ide0"]:
                        if disk_key in vm_config:
                            disk_config = vm_config[disk_key]
                            disk_path = disk_config
                            
                            # Parse disk path: "storage:vm-100-disk-0" or "storage:vm-100-disk-0,size=20G"
                            if ":" in disk_config:
                                disk_storage = disk_config.split(":")[0]
                            
                            # Extract size if present
                            if "size=" in disk_config:
                                size_part = disk_config.split("size=")[1].split(",")[0]
                                if "G" in size_part:
                                    disk_size_gb = float(size_part.replace("G", ""))
                                elif "M" in size_part:
                                    disk_size_gb = float(size_part.replace("M", "")) / 1024
                            
                            # Try to determine format
                            if ".qcow2" in disk_config or "qcow2" in disk_storage:
                                disk_format = "qcow2"
                            elif ".raw" in disk_config or "raw" in disk_storage:
                                disk_format = "raw"
                            
                            break  # Use first disk found
                    
                    # Extract network bridge
                    network_bridge = None
                    for net_key in ["net0", "net1", "net2"]:
                        if net_key in vm_config:
                            net_config = vm_config[net_key]
                            # Parse "virtio=XX:XX:XX:XX:XX:XX,bridge=vmbr0"
                            if "bridge=" in net_config:
                                network_bridge = net_config.split("bridge=")[1].split(",")[0]
                                break
                    
                    if existing:
                        # Update existing template
                        existing.name = name
                        existing.last_verified_at = datetime.utcnow()
                        existing.cpu_cores = cpu_cores
                        existing.cpu_sockets = cpu_sockets
                        existing.memory_mb = memory_mb
                        existing.disk_size_gb = disk_size_gb
                        existing.disk_storage = disk_storage
                        existing.disk_path = disk_path
                        existing.disk_format = disk_format
                        existing.network_bridge = network_bridge
                        existing.os_type = os_type
                        existing.specs_cached_at = datetime.utcnow()
                        updated_count += 1
                    else:
                        # Create new template
                        new_template = Template(
                            name=name,
                            proxmox_vmid=vmid,
                            cluster_ip=cluster_ip,
                            node=node,
                            is_replica=False,
                            is_class_template=False,
                            last_verified_at=datetime.utcnow(),
                            cpu_cores=cpu_cores,
                            cpu_sockets=cpu_sockets,
                            memory_mb=memory_mb,
                            disk_size_gb=disk_size_gb,
                            disk_storage=disk_storage,
                            disk_path=disk_path,
                            disk_format=disk_format,
                            network_bridge=network_bridge,
                            os_type=os_type,
                            specs_cached_at=datetime.utcnow()
                        )
                        db.session.add(new_template)
                        synced_count += 1
                    
                except Exception as e:
                    logger.error(f"Failed to fetch specs for template {vmid} on {node}: {e}")
                    errors.append(f"{node}/{vmid}: {str(e)}")
            
            db.session.commit()
            logger.info(f"Synced templates from cluster {cluster_ip}: {synced_count} new, {updated_count} updated")
            
        except Exception as e:
            logger.exception(f"Failed to sync templates from cluster {cluster_ip}: {e}")
            errors.append(f"Cluster {cluster_ip}: {str(e)}")
    
    return jsonify({
        "ok": True,
        "synced": synced_count,
        "updated": updated_count,
        "errors": errors if errors else None
    })
