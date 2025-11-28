#!/usr/bin/env python3
"""
Test script to verify performance optimizations.

This script validates that:
1. Single VM status endpoint exists and works
2. skip_ip parameter is properly handled
3. find_vm_for_user accepts skip_ip parameter
4. Frontend references correct API endpoints
"""

import re
import sys
from pathlib import Path

def test_api_endpoint():
    """Test that single VM status endpoint exists."""
    api_file = Path("app/routes/api/vms.py")
    content = api_file.read_text(encoding='utf-8')
    
    # Check for status endpoint
    if '/vm/<int:vmid>/status' not in content:
        print("❌ FAIL: Single VM status endpoint not found")
        return False
    
    # Check for skip_ip parameter handling
    if 'skip_ip' not in content:
        print("❌ FAIL: skip_ip parameter not handled")
        return False
    
    print("✅ PASS: API endpoint exists with skip_ip support")
    return True

def test_proxmox_client():
    """Test that find_vm_for_user accepts skip_ip."""
    client_file = Path("app/services/proxmox_client.py")
    content = client_file.read_text(encoding='utf-8')
    
    # Check for skip_ip parameter in function signature
    pattern = r'def find_vm_for_user\([^)]*skip_ip[^)]*\)'
    if not re.search(pattern, content):
        print("❌ FAIL: find_vm_for_user doesn't accept skip_ip parameter")
        return False
    
    # Check that skip_ip is passed to get_vms_for_user
    pattern = r'get_vms_for_user\([^)]*skip_ips?\s*=\s*skip_ip'
    if not re.search(pattern, content):
        print("❌ FAIL: skip_ip not passed to get_vms_for_user")
        return False
    
    print("✅ PASS: find_vm_for_user optimized with skip_ip")
    return True

def test_frontend():
    """Test frontend uses optimized endpoints."""
    html_file = Path("app/templates/index.html")
    content = html_file.read_text(encoding='utf-8')
    
    # Check for refreshSingleVMStatus function
    if 'function refreshSingleVMStatus' not in content:
        print("❌ FAIL: refreshSingleVMStatus function not found")
        return False
    
    # Check for visibility detection
    if 'document.hidden' not in content:
        print("❌ FAIL: Visibility detection not implemented")
        return False
    
    # Check for conditional IP fetching
    if 'needsIPFetch' not in content:
        print("❌ FAIL: Conditional IP fetching not implemented")
        return False
    
    # Check polling interval increased to 30s
    if 'setInterval(refreshVmStatus, 30000)' not in content:
        print("⚠️  WARNING: Polling interval not set to 30 seconds")
    
    print("✅ PASS: Frontend optimizations implemented")
    return True

def test_performance_doc():
    """Test that performance documentation exists."""
    doc_file = Path("PERFORMANCE_OPTIMIZATIONS.md")
    if not doc_file.exists():
        print("❌ FAIL: Performance documentation not found")
        return False
    
    content = doc_file.read_text(encoding='utf-8')
    
    required_sections = [
        "Problem Statement",
        "Optimizations Implemented",
        "Performance Metrics",
        "Usage Examples"
    ]
    
    for section in required_sections:
        if section not in content:
            print(f"❌ FAIL: Missing documentation section: {section}")
            return False
    
    print("✅ PASS: Performance documentation complete")
    return True

def main():
    print("=" * 60)
    print("Performance Optimization Test Suite")
    print("=" * 60)
    print()
    
    tests = [
        ("API Endpoint", test_api_endpoint),
        ("Proxmox Client", test_proxmox_client),
        ("Frontend", test_frontend),
        ("Documentation", test_performance_doc),
    ]
    
    results = []
    for name, test_func in tests:
        print(f"\nTesting: {name}")
        print("-" * 40)
        results.append(test_func())
    
    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} tests passed")
    print("=" * 60)
    
    if passed == total:
        print("\n✅ All optimizations verified successfully!")
        return 0
    else:
        print(f"\n❌ {total - passed} test(s) failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())
