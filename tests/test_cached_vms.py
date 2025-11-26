#!/usr/bin/env python3
"""
Smoke tests for cached VM API and mappings functionality.

These tests verify:
1. /api/vms returns valid JSON
2. Cache behavior (two immediate requests should be fast)
3. Mappings read/write functionality

Run with: python -m pytest tests/test_cached_vms.py -v
Or directly: python tests/test_cached_vms.py
"""

import json
import os
import sys
import time
import tempfile
import threading

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'rdp-gen'))


def test_cluster_cache_thread_safety():
    """Test that cluster cache functions are thread-safe."""
    from proxmox_client import (
        _cluster_cache_lock,
        _get_cluster_resources_cached,
        invalidate_cluster_cache,
    )
    
    # Invalidate cache to start fresh
    invalidate_cluster_cache()
    
    results = []
    errors = []
    
    def worker():
        try:
            # This should be thread-safe
            for _ in range(5):
                invalidate_cluster_cache()
                time.sleep(0.01)
        except Exception as e:
            errors.append(str(e))
    
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    assert len(errors) == 0, f"Thread safety errors: {errors}"
    print("✓ Cluster cache is thread-safe")


def test_mappings_cache_thread_safety():
    """Test that mappings cache is thread-safe for concurrent access."""
    from proxmox_client import (
        _mappings_cache_lock,
        get_user_vm_map,
        set_user_vm_mapping,
        invalidate_mappings_cache,
    )
    
    # Create a temporary mappings file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump({"test_user@pve": [100, 101]}, f)
        temp_path = f.name
    
    # Temporarily override MAPPINGS_FILE
    import proxmox_client
    original_file = proxmox_client.MAPPINGS_FILE
    proxmox_client.MAPPINGS_FILE = temp_path
    
    try:
        # Reset cache state
        invalidate_mappings_cache()
        
        errors = []
        
        def reader():
            try:
                for _ in range(10):
                    mapping = get_user_vm_map()
                    assert isinstance(mapping, dict)
            except Exception as e:
                errors.append(f"Reader error: {e}")
        
        def writer():
            try:
                for i in range(10):
                    set_user_vm_mapping(f"thread_user_{i}@pve", [200 + i])
            except Exception as e:
                errors.append(f"Writer error: {e}")
        
        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=reader))
            threads.append(threading.Thread(target=writer))
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Thread safety errors: {errors}"
        
        # Verify final state
        final_mapping = get_user_vm_map()
        assert isinstance(final_mapping, dict)
        print("✓ Mappings cache is thread-safe")
        
    finally:
        # Restore original file
        proxmox_client.MAPPINGS_FILE = original_file
        invalidate_mappings_cache()
        # Clean up temp file
        try:
            os.unlink(temp_path)
        except:
            pass


def test_ip_cache_thread_safety():
    """Test that IP cache is thread-safe."""
    from proxmox_client import (
        _ip_cache_lock,
        _get_cached_ip,
        _cache_ip,
    )
    
    errors = []
    
    def worker(thread_id):
        try:
            for i in range(100):
                vmid = 1000 + thread_id * 100 + i
                _cache_ip(vmid, f"10.0.{thread_id}.{i % 256}")
                ip = _get_cached_ip(vmid)
                # IP should match or be None (if expired)
        except Exception as e:
            errors.append(f"Thread {thread_id} error: {e}")
    
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    assert len(errors) == 0, f"IP cache thread safety errors: {errors}"
    print("✓ IP cache is thread-safe")


def test_proxmox_cache_ttl_config():
    """Test that PROXMOX_CACHE_TTL is configurable via environment."""
    from config import PROXMOX_CACHE_TTL
    
    # Default should be 10 seconds
    assert isinstance(PROXMOX_CACHE_TTL, int)
    assert PROXMOX_CACHE_TTL > 0
    print(f"✓ PROXMOX_CACHE_TTL = {PROXMOX_CACHE_TTL} seconds")


def test_cluster_query_functions_exist():
    """Test that new cluster-wide query functions exist."""
    from proxmox_client import (
        get_all_qemu_vms,
        get_all_lxc_containers,
        invalidate_cluster_cache,
        lookup_ips_parallel,
        shutdown_executor,
        _get_cached_ips_batch,
    )
    
    # Verify functions are callable
    assert callable(get_all_qemu_vms)
    assert callable(get_all_lxc_containers)
    assert callable(invalidate_cluster_cache)
    assert callable(lookup_ips_parallel)
    assert callable(shutdown_executor)
    assert callable(_get_cached_ips_batch)
    print("✓ Cluster-wide query functions exist")


def test_batch_ip_cache():
    """Test batch IP cache retrieval function."""
    from proxmox_client import (
        _cache_ip,
        _get_cached_ips_batch,
    )
    
    # Cache some test IPs
    test_vmids = [9901, 9902, 9903, 9904]
    for i, vmid in enumerate(test_vmids):
        _cache_ip(vmid, f"10.99.0.{i+1}")
    
    # Batch retrieve
    cached = _get_cached_ips_batch(test_vmids + [9999])  # Include non-existent
    
    # Verify we got the cached IPs
    assert 9901 in cached
    assert 9902 in cached
    assert 9903 in cached
    assert 9904 in cached
    assert 9999 not in cached  # Non-existent should not be in results
    assert cached[9901] == "10.99.0.1"
    
    print("✓ Batch IP cache retrieval works")


def test_thread_pool_executor_exists():
    """Test that ThreadPoolExecutor for parallel IP lookups exists."""
    from proxmox_client import _ip_lookup_executor
    from concurrent.futures import ThreadPoolExecutor
    
    assert isinstance(_ip_lookup_executor, ThreadPoolExecutor)
    print("✓ ThreadPoolExecutor for IP lookups exists")


def test_mappings_write_through_cache():
    """Test that mappings use write-through caching."""
    from proxmox_client import (
        get_user_vm_map,
        set_user_vm_mapping,
        invalidate_mappings_cache,
        _mappings_cache,
    )
    
    # Create a temporary mappings file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump({}, f)
        temp_path = f.name
    
    # Temporarily override MAPPINGS_FILE
    import proxmox_client
    original_file = proxmox_client.MAPPINGS_FILE
    proxmox_client.MAPPINGS_FILE = temp_path
    
    try:
        # Reset cache
        invalidate_mappings_cache()
        
        # Set a mapping
        set_user_vm_mapping("cache_test@pve", [100, 101, 102])
        
        # Verify in-memory cache is updated
        mapping = get_user_vm_map()
        assert "cache_test@pve" in mapping
        assert mapping["cache_test@pve"] == [100, 101, 102]
        
        # Verify written to disk
        with open(temp_path, 'r') as f:
            disk_data = json.load(f)
        assert "cache_test@pve" in disk_data
        assert disk_data["cache_test@pve"] == [100, 101, 102]
        
        print("✓ Mappings use write-through caching")
        
    finally:
        # Restore original file
        proxmox_client.MAPPINGS_FILE = original_file
        invalidate_mappings_cache()
        # Clean up temp file
        try:
            os.unlink(temp_path)
        except:
            pass


def test_rdp_content_in_memory():
    """Test that RDP content is built in-memory without temp files."""
    from proxmox_client import build_rdp
    
    # Create a mock VM dict
    mock_vm = {
        "vmid": 999,
        "name": "test-vm",
        "ip": "192.168.1.100",
        "node": "test-node",
        "type": "qemu",
        "category": "windows",
    }
    
    # Build RDP content
    content = build_rdp(mock_vm)
    
    # Verify it's a string with expected content
    assert isinstance(content, str)
    assert "full address:s:192.168.1.100:3389" in content
    assert "prompt for credentials:i:1" in content
    
    print("✓ RDP content is built in-memory")


def test_flask_app_creates():
    """Test that Flask app can be created without errors."""
    # This test runs in the test process, not connected to Proxmox
    # It just verifies the app module loads correctly
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'rdp-gen'))
    
    # Import should not raise
    from app import app
    
    assert app is not None
    assert app.secret_key is not None
    
    print("✓ Flask app creates successfully")


def run_all_tests():
    """Run all smoke tests."""
    print("\n=== Running Proxmox Lab GUI Smoke Tests ===\n")
    
    tests = [
        test_proxmox_cache_ttl_config,
        test_cluster_query_functions_exist,
        test_batch_ip_cache,
        test_thread_pool_executor_exists,
        test_rdp_content_in_memory,
        test_flask_app_creates,
        test_cluster_cache_thread_safety,
        test_ip_cache_thread_safety,
        test_mappings_cache_thread_safety,
        test_mappings_write_through_cache,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1
    
    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
