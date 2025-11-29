"""API routes for querying template information from the database."""

from flask import Blueprint, jsonify, request, session
from app.models import Template, db
from app.utils.decorators import login_required
from app.config import CLUSTERS

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
