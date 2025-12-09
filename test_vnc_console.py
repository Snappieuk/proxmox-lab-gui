#!/usr/bin/env python3
"""
Test script to verify VNC console implementation
Tests local noVNC assets, proxmox_api service, and Flask routes
"""

import os
import sys

# Add app directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_novnc_assets():
    """Test that noVNC assets are in place"""
    print("=" * 60)
    print("TEST 1: Verify noVNC Assets")
    print("=" * 60)
    
    required_files = [
        'app/static/novnc/core/rfb.js',
        'app/static/novnc/core/websock.js',
        'app/static/novnc/core/display.js',
        'app/static/novnc/app/styles/base.css',
    ]
    
    all_exist = True
    for file_path in required_files:
        exists = os.path.exists(file_path)
        status = "✓" if exists else "✗"
        print(f"{status} {file_path}")
        all_exist = all_exist and exists
    
    if all_exist:
        # Check total size
        total_size = 0
        for root, dirs, files in os.walk('app/static/novnc'):
            for file in files:
                total_size += os.path.getsize(os.path.join(root, file))
        
        size_mb = total_size / 1024 / 1024
        print(f"\n✓ All noVNC assets present ({size_mb:.2f} MB)")
        return True
    else:
        print("\n✗ Some noVNC assets are missing")
        return False


def test_proxmox_api_module():
    """Test that proxmox_api.py module can be imported"""
    print("\n" + "=" * 60)
    print("TEST 2: Verify proxmox_api Service")
    print("=" * 60)
    
    try:
        from app.services import proxmox_api
        
        # Check required functions exist
        required_functions = ['get_vnc_ticket', 'get_auth_ticket', 'find_vm_location']
        
        all_exist = True
        for func_name in required_functions:
            has_func = hasattr(proxmox_api, func_name)
            status = "✓" if has_func else "✗"
            print(f"{status} {func_name}()")
            all_exist = all_exist and has_func
        
        if all_exist:
            print("\n✓ proxmox_api module loaded successfully")
            return True
        else:
            print("\n✗ Some functions missing from proxmox_api")
            return False
            
    except ImportError as e:
        print(f"✗ Failed to import proxmox_api: {e}")
        return False


def test_console_routes():
    """Test that console routes are registered"""
    print("\n" + "=" * 60)
    print("TEST 3: Verify Console Routes")
    print("=" * 60)
    
    try:
        from app import create_app
        
        app = create_app()
        
        # Check for console routes
        console_routes = [
            rule.rule for rule in app.url_map.iter_rules() 
            if 'console' in rule.rule
        ]
        
        expected_routes = [
            '/api/console/<int:vmid>/vnc',
            '/api/console/<int:vmid>/view2',
        ]
        
        all_exist = True
        for route in expected_routes:
            # Check if route pattern exists (normalize int: parameter)
            found = any(
                route.replace('<int:vmid>', '<vmid>') in r or route in r 
                for r in console_routes
            )
            status = "✓" if found else "✗"
            print(f"{status} {route}")
            all_exist = all_exist and found
        
        print(f"\nAll console routes: {console_routes}")
        
        if all_exist:
            print("\n✓ Console routes registered successfully")
            return True
        else:
            print("\n✗ Some console routes not found")
            return False
            
    except Exception as e:
        print(f"✗ Failed to create Flask app: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_console_template():
    """Test that console.html uses local assets"""
    print("\n" + "=" * 60)
    print("TEST 4: Verify Console Template")
    print("=" * 60)
    
    template_path = 'app/templates/console.html'
    
    if not os.path.exists(template_path):
        print(f"✗ Template not found: {template_path}")
        return False
    
    with open(template_path, 'r') as f:
        content = f.read()
    
    # Check for local asset references
    checks = {
        'Local CSS': "url_for('static', filename='novnc/app/styles/base.css')" in content,
        'Local JS': "url_for('static', filename='novnc/core/rfb.js')" in content,
        'No CDN CSS': 'cdn.jsdelivr.net' not in content,
        'WebSocket Proxy': '/api/console/ws/console/' in content,
    }
    
    all_pass = True
    for check_name, passed in checks.items():
        status = "✓" if passed else "✗"
        print(f"{status} {check_name}")
        all_pass = all_pass and passed
    
    if all_pass:
        print("\n✓ Console template configured correctly")
        return True
    else:
        print("\n✗ Console template has configuration issues")
        return False


def test_dependencies():
    """Test that required Python packages are installed"""
    print("\n" + "=" * 60)
    print("TEST 5: Verify Python Dependencies")
    print("=" * 60)
    
    required_packages = {
        'flask': 'Flask',
        'flask_sock': 'flask-sock',
        'websocket': 'websocket-client',
        'proxmoxer': 'proxmoxer',
    }
    
    all_installed = True
    for module_name, package_name in required_packages.items():
        try:
            __import__(module_name)
            print(f"✓ {package_name}")
        except ImportError:
            print(f"✗ {package_name} - Not installed")
            all_installed = False
    
    if all_installed:
        print("\n✓ All dependencies installed")
        return True
    else:
        print("\n✗ Some dependencies missing")
        print("\nInstall with: pip install -r requirements.txt")
        return False


def main():
    """Run all tests"""
    print("\n" + "=" * 60)
    print("VNC Console Implementation Test Suite")
    print("=" * 60)
    
    results = {
        'noVNC Assets': test_novnc_assets(),
        'proxmox_api Service': test_proxmox_api_module(),
        'Console Routes': test_console_routes(),
        'Console Template': test_console_template(),
        'Dependencies': test_dependencies(),
    }
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    passed = sum(results.values())
    total = len(results)
    
    for test_name, result in results.items():
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {test_name}")
    
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print("\n✅ All tests passed! VNC console implementation is ready.")
        print("\nNext steps:")
        print("1. Deploy to Proxmox server")
        print("2. Restart Flask application")
        print("3. Test console access in browser")
        print("4. Check logs: journalctl -u proxmox-gui -f | grep -i vnc")
        return 0
    else:
        print("\n❌ Some tests failed. Please fix the issues above.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
