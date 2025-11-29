#!/usr/bin/env python3
"""
Node selection logic for intelligent VM placement across Proxmox cluster.

Selects the best node based on available resources (RAM, CPU, IO wait).
"""

import logging
from typing import Optional, Dict, List, Tuple
from app.services.proxmox_service import get_proxmox_admin_for_cluster

logger = logging.getLogger(__name__)


def get_node_resources(proxmox, node_name: str) -> Optional[Dict]:
    """Get resource usage stats for a specific node.
    
    Returns dict with:
        - mem_used: Used memory in bytes
        - mem_total: Total memory in bytes
        - mem_free: Free memory in bytes
        - mem_percent: Memory usage percentage
        - cpu_usage: CPU usage percentage (0-100)
        - cpu_count: Number of CPU cores
        - iowait: IO wait percentage (0-100)
        - load_avg: 1-minute load average
    """
    try:
        status = proxmox.nodes(node_name).status.get()
        # Defensive defaults
        mem = status.get('memory') or {}
        mem_used = mem.get('used', 0)
        mem_total = mem.get('total', 0) or 1  # avoid div by zero
        mem_free = mem.get('free', max(0, mem_total - mem_used))

        cpu_usage = (status.get('cpu') or 0) * 100  # percentage
        cpuinfo = status.get('cpuinfo') or {}
        cpu_count = cpuinfo.get('cpus', 1) or 1

        iowait = (status.get('wait') or 0) * 100
        loadavg = status.get('loadavg') or [0]
        load_avg = loadavg[0] if isinstance(loadavg, (list, tuple)) and loadavg else (loadavg if isinstance(loadavg, (int, float)) else 0)

        online_status = status.get('status')
        online = True if online_status is None else (online_status == 'online')

        return {
            'node': node_name,
            'mem_used': mem_used,
            'mem_total': mem_total,
            'mem_free': mem_free,
            'mem_percent': (mem_used / mem_total * 100) if mem_total > 0 else 100,
            'cpu_usage': cpu_usage,
            'cpu_count': cpu_count,
            'iowait': iowait,
            'load_avg': load_avg,
            'online': online
        }
    except Exception as e:
        logger.error(f"Failed to get resources for node {node_name}: {e}")
        return None


def calculate_node_score(node_stats: Dict) -> float:
    """Calculate a score for node suitability (higher is better).
    
    Scoring factors:
    - Available memory (40% weight)
    - Low CPU usage (30% weight)
    - Low IO wait (20% weight)
    - Low load average relative to cores (10% weight)
    
    Returns: Score from 0-100 (higher = better node)
    """
    if not node_stats or not node_stats.get('online'):
        return 0.0
    
    # Coerce numeric fields defensively
    try:
        mem_percent = float(node_stats.get('mem_percent', 100))
    except Exception:
        mem_percent = 100.0
    try:
        cpu_usage = float(node_stats.get('cpu_usage', 100))
    except Exception:
        cpu_usage = 100.0
    try:
        iowait = float(node_stats.get('iowait', 100))
    except Exception:
        iowait = 100.0
    try:
        load_avg = float(node_stats.get('load_avg', 0))
    except Exception:
        load_avg = 0.0
    try:
        cpu_count = int(node_stats.get('cpu_count', 1)) or 1
    except Exception:
        cpu_count = 1

    # Memory score: More free memory = better (0-100)
    mem_free_percent = 100 - mem_percent
    mem_score = mem_free_percent * 0.4
    
    # CPU score: Less usage = better (0-100)
    cpu_free_percent = 100 - min(cpu_usage, 100)
    cpu_score = cpu_free_percent * 0.3
    
    # IO wait score: Less wait = better (0-100)
    io_free_percent = 100 - min(iowait, 100)
    io_score = io_free_percent * 0.2
    
    # Load average score: Lower load per core = better (0-100)
    # Normalize load avg: 0.0 = 100, 1.0 per core = 50, 2.0+ per core = 0
    load_per_core = load_avg / cpu_count
    load_score = max(0, min(100, 100 - (load_per_core * 50))) * 0.1
    
    total_score = mem_score + cpu_score + io_score + load_score
    
    logger.debug(
        f"Node {node_stats['node']} score: {total_score:.1f} "
        f"(mem: {mem_score:.1f}, cpu: {cpu_score:.1f}, io: {io_score:.1f}, load: {load_score:.1f})"
    )
    
    return total_score


def select_best_node(cluster_id: str, exclude_nodes: List[str] = None) -> Optional[str]:
    """Select the best node in a cluster based on available resources.
    
    Args:
        cluster_id: Cluster ID to query
        exclude_nodes: List of node names to exclude from selection
    
    Returns:
        Name of the best node, or None if no suitable nodes found
    """
    exclude_nodes = exclude_nodes or []
    
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        
        # Get all nodes in cluster
        nodes = proxmox.nodes.get()
        
        if not nodes:
            logger.warning(f"No nodes found in cluster {cluster_id}")
            return None
        
        # Get resources for each node
        node_scores = []
        for node_info in nodes:
            node_name = node_info.get('node')
            
            if node_name in exclude_nodes:
                logger.debug(f"Skipping excluded node: {node_name}")
                continue
            
            stats = get_node_resources(proxmox, node_name)
            if not stats:
                continue
            # Log basic stats regardless of online flag
            logger.debug(
                f"Node {node_name} raw stats: online={stats.get('online')}, "
                f"mem%={stats['mem_percent']:.1f}, cpu%={stats['cpu_usage']:.1f}, "
                f"iowait%={stats['iowait']:.1f}, load={stats['load_avg']:.2f}/{stats['cpu_count']}"
            )

            score = calculate_node_score(stats)
            node_scores.append((node_name, score, stats))
            
            logger.info(
                f"Node {node_name}: Score={score:.1f}, "
                f"RAM={stats['mem_percent']:.1f}%, "
                f"CPU={stats['cpu_usage']:.1f}%, "
                f"IOWait={stats['iowait']:.1f}%, "
                f"Load={stats['load_avg']:.2f}/{stats['cpu_count']}"
            )
        
        if not node_scores:
            logger.warning("No suitable nodes found in cluster")
            return None
        
        # Sort by score (highest first)
        node_scores.sort(key=lambda x: x[1], reverse=True)
        
        best_node = node_scores[0][0]
        best_score = node_scores[0][1]
        
        logger.info(f"Selected node {best_node} with score {best_score:.1f}")
        
        return best_node
        
    except Exception as e:
        logger.exception(f"Failed to select best node: {e}")
        return None


def distribute_vms_across_nodes(cluster_id: str, vm_count: int, estimated_ram_mb: int = 2048, 
                                estimated_cpu_cores: float = 1.0) -> List[str]:
    """Distribute VMs across multiple nodes for load balancing with dynamic re-scoring.
    
    Args:
        cluster_id: Cluster ID
        vm_count: Number of VMs to distribute
        estimated_ram_mb: Estimated RAM per VM in MB (default: 2048)
        estimated_cpu_cores: Estimated CPU cores per VM (default: 1.0)
    
    Returns:
        List of node names (one per VM), distributed for optimal balance with dynamic updates
    """
    try:
        proxmox = get_proxmox_admin_for_cluster(cluster_id)
        nodes = proxmox.nodes.get()
        
        if not nodes:
            return []
        
        # Get initial resources for all nodes (do not filter by 'online' to avoid empty sets)
        node_stats = {}
        for node_info in nodes:
            node_name = node_info.get('node')
            stats = get_node_resources(proxmox, node_name)
            if stats:
                node_stats[node_name] = stats
            else:
                logger.warning(f"Skipping node {node_name}: unable to get stats")
        
        if not node_stats:
            return []
        
        # Track VM assignments with dynamic re-scoring
        node_assignments = []
        estimated_ram_bytes = estimated_ram_mb * 1024 * 1024
        
        for i in range(vm_count):
            # Calculate current scores for all nodes
            node_scores = []
            for node_name, stats in node_stats.items():
                score = calculate_node_score(stats)
                node_scores.append((node_name, score, stats))
            
            # Sort by score (best first)
            node_scores.sort(key=lambda x: x[1], reverse=True)
            
            # Select best node
            # If all scores are zero (e.g., missing metrics), fall back to first available node
            best_node = node_scores[0][0] if node_scores else next(iter(node_stats.keys()))
            node_assignments.append(best_node)
            
            logger.debug(
                f"VM {i+1}/{vm_count}: Assigned to {best_node} "
                f"(Score: {node_scores[0][1]:.1f}, "
                f"RAM: {node_stats[best_node]['mem_percent']:.1f}%, "
                f"CPU: {node_stats[best_node]['cpu_usage']:.1f}%)"
            )
            
            # Update node stats to reflect the new VM placement
            current_stats = node_stats[best_node]
            
            # Adjust memory usage
            current_stats['mem_used'] += estimated_ram_bytes
            current_stats['mem_free'] = max(0, current_stats['mem_free'] - estimated_ram_bytes)
            current_stats['mem_percent'] = (current_stats['mem_used'] / current_stats['mem_total'] * 100) if current_stats['mem_total'] > 0 else 100
            
            # Adjust CPU usage (estimated increase based on cores allocated)
            # Assume each VM core adds proportional CPU load
            cpu_increase = (estimated_cpu_cores / current_stats['cpu_count']) * 100
            current_stats['cpu_usage'] = min(100, current_stats['cpu_usage'] + cpu_increase)
            
            # Adjust load average (rough estimate: +1 per VM)
            try:
                current_stats['load_avg'] = float(current_stats.get('load_avg', 0)) + 1.0
            except Exception:
                current_stats['load_avg'] = 1.0
            
            # Update the stats in the dictionary
            node_stats[best_node] = current_stats
        
        # Log final distribution summary
        distribution_summary = {}
        for node_name in node_assignments:
            distribution_summary[node_name] = distribution_summary.get(node_name, 0) + 1
        
        logger.info(
            f"Distributed {vm_count} VMs with dynamic re-scoring: " +
            ", ".join([f"{node}={count}" for node, count in distribution_summary.items()])
        )
        
        return node_assignments
        
    except Exception as e:
        logger.exception(f"Failed to distribute VMs: {e}")
        return []
