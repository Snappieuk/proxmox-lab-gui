#!/usr/bin/env python3
"""
Tests for arp_scanner module.

Run with: python -m pytest tests/test_arp_scanner.py -v
Or directly: python tests/test_arp_scanner.py
"""

import os
import sys
import time

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'rdp-gen'))


def test_normalize_mac_valid_formats():
    """Test normalize_mac with various valid MAC formats."""
    from arp_scanner import normalize_mac
    
    # With colons
    assert normalize_mac('AA:BB:CC:DD:EE:FF') == 'aabbccddeeff'
    assert normalize_mac('aa:bb:cc:dd:ee:ff') == 'aabbccddeeff'
    
    # With dashes
    assert normalize_mac('AA-BB-CC-DD-EE-FF') == 'aabbccddeeff'
    assert normalize_mac('aa-bb-cc-dd-ee-ff') == 'aabbccddeeff'
    
    # Already normalized
    assert normalize_mac('aabbccddeeff') == 'aabbccddeeff'
    assert normalize_mac('AABBCCDDEEFF') == 'aabbccddeeff'
    
    # Mixed separators
    assert normalize_mac('AA:BB-CC:DD-EE:FF') == 'aabbccddeeff'
    
    print("✓ normalize_mac handles valid formats correctly")


def test_normalize_mac_invalid_formats():
    """Test normalize_mac with invalid inputs."""
    from arp_scanner import normalize_mac
    
    # None/empty
    assert normalize_mac(None) is None
    assert normalize_mac('') is None
    
    # Too short/long
    assert normalize_mac('aabbcc') is None
    assert normalize_mac('aabbccddeeffgg') is None
    
    # Invalid characters
    assert normalize_mac('gghhiijjkkll') is None
    assert normalize_mac('zz:bb:cc:dd:ee:ff') is None
    
    print("✓ normalize_mac rejects invalid formats correctly")


def test_get_scan_status_returns_none_for_unknown():
    """Test get_scan_status returns None for unknown VMIDs."""
    from arp_scanner import get_scan_status
    
    # Unknown VMID should return None
    assert get_scan_status(99999) is None
    assert get_scan_status(-1) is None
    assert get_scan_status(0) is None
    
    print("✓ get_scan_status returns None for unknown VMIDs")


def test_discover_ips_via_arp_empty_map():
    """Test discover_ips_via_arp with empty vm_mac_map."""
    from arp_scanner import discover_ips_via_arp
    
    # Empty map should return empty dict
    result = discover_ips_via_arp({}, background=False)
    assert result == {}
    
    result = discover_ips_via_arp({}, background=True)
    assert result == {}
    
    print("✓ discover_ips_via_arp handles empty map correctly")


def test_is_root_returns_bool():
    """Test _is_root returns a boolean."""
    from arp_scanner import _is_root
    
    result = _is_root()
    assert isinstance(result, bool)
    
    # In test environment, we're typically not root
    # but either value is acceptable
    print(f"✓ _is_root returns boolean (value: {result})")


def test_get_arp_table_returns_dict():
    """Test get_arp_table returns a dict."""
    from arp_scanner import get_arp_table
    
    result = get_arp_table()
    assert isinstance(result, dict)
    
    # All keys should be normalized MACs (12 hex chars, lowercase)
    for mac in result.keys():
        assert len(mac) == 12
        assert all(c in '0123456789abcdef' for c in mac)
    
    # All values should be IP addresses (basic check)
    for ip in result.values():
        assert isinstance(ip, str)
        # Basic IP format check
        parts = ip.split('.')
        if len(parts) == 4:  # IPv4
            assert all(p.isdigit() for p in parts)
    
    print(f"✓ get_arp_table returns valid dict ({len(result)} entries)")


def test_module_level_cache_exists():
    """Test that module-level cache variables exist."""
    import arp_scanner
    
    assert hasattr(arp_scanner, '_arp_cache')
    assert hasattr(arp_scanner, '_arp_cache_time')
    assert hasattr(arp_scanner, '_arp_cache_ttl')
    assert hasattr(arp_scanner, '_scan_status')
    assert hasattr(arp_scanner, '_scan_in_progress')
    assert hasattr(arp_scanner, '_scan_lock')
    
    # Check default TTL is 3600 seconds (1 hour)
    assert arp_scanner._arp_cache_ttl == 3600
    
    print("✓ Module-level cache variables exist with correct defaults")


def test_scan_lock_is_threading_lock():
    """Test that _scan_lock is a threading.Lock."""
    import arp_scanner
    
    # Should be a lock that can be acquired and released
    acquired = arp_scanner._scan_lock.acquire(blocking=False)
    if acquired:
        arp_scanner._scan_lock.release()
    
    print("✓ _scan_lock is a valid threading lock")


def test_background_scan_does_not_block():
    """Test that background=True returns immediately."""
    from arp_scanner import discover_ips_via_arp
    
    # Create a fake vm_mac_map
    vm_mac_map = {9999: 'aabbccddeeff'}
    
    start = time.time()
    result = discover_ips_via_arp(vm_mac_map, background=True)
    elapsed = time.time() - start
    
    # Background mode should return very quickly (< 1 second)
    # It may take slightly longer due to ARP table lookup
    assert elapsed < 2.0, f"Background mode took {elapsed}s, should be < 2s"
    
    # Result should be a dict (possibly empty if MAC not in ARP table)
    assert isinstance(result, dict)
    
    print(f"✓ Background mode returns in {elapsed:.3f}s (< 2s)")


def test_exports_match_requirements():
    """Test that all required functions are exported."""
    from arp_scanner import (
        normalize_mac,
        discover_ips_via_arp,
        get_scan_status,
    )
    
    # All required functions should be callable
    assert callable(normalize_mac)
    assert callable(discover_ips_via_arp)
    assert callable(get_scan_status)
    
    print("✓ All required functions are exported")


def test_proxmox_client_can_import():
    """Test that proxmox_client can import arp_scanner functions."""
    import proxmox_client
    
    assert proxmox_client.ARP_SCANNER_AVAILABLE is True
    
    print("✓ proxmox_client successfully imports arp_scanner")


def run_all_tests():
    """Run all arp_scanner tests."""
    print("\n=== Running ARP Scanner Tests ===\n")
    
    tests = [
        test_normalize_mac_valid_formats,
        test_normalize_mac_invalid_formats,
        test_get_scan_status_returns_none_for_unknown,
        test_discover_ips_via_arp_empty_map,
        test_is_root_returns_bool,
        test_get_arp_table_returns_dict,
        test_module_level_cache_exists,
        test_scan_lock_is_threading_lock,
        test_background_scan_does_not_block,
        test_exports_match_requirements,
        test_proxmox_client_can_import,
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
