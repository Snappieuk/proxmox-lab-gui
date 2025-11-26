#!/usr/bin/env python3
"""
Test network scanning capabilities on the server.
"""

import subprocess
import sys

def test_command(cmd_path, args):
    """Test if a command exists and works."""
    try:
        result = subprocess.run(
            [cmd_path] + args,
            capture_output=True,
            text=True,
            timeout=2
        )
        print(f"✓ {cmd_path} found - returncode: {result.returncode}")
        if result.stdout:
            print(f"  stdout: {result.stdout[:100]}")
        if result.stderr:
            print(f"  stderr: {result.stderr[:100]}")
        return True
    except FileNotFoundError:
        print(f"✗ {cmd_path} not found")
        return False
    except Exception as e:
        print(f"✗ {cmd_path} error: {e}")
        return False

print("Testing network scanning tools:\n")

# Test ping
print("1. Testing ping:")
test_command('/usr/bin/ping', ['-c', '1', '127.0.0.1'])
test_command('ping', ['-c', '1', '127.0.0.1'])

# Test broadcast ping (might require permissions)
print("\n2. Testing broadcast ping:")
test_command('/usr/bin/ping', ['-b', '-c', '1', '-W', '1', '10.220.15.255'])

# Test nmap
print("\n3. Testing nmap:")
test_command('/usr/bin/nmap', ['--version'])
test_command('nmap', ['--version'])

# Test fping
print("\n4. Testing fping:")
test_command('/usr/bin/fping', ['--version'])
test_command('/usr/sbin/fping', ['--version'])
test_command('fping', ['--version'])

# Test arp
print("\n5. Testing ARP commands:")
test_command('ip', ['neigh', 'show'])
test_command('arp', ['-a'])

# Test if running as root
print("\n6. User privileges:")
result = subprocess.run(['id'], capture_output=True, text=True)
print(f"  Current user: {result.stdout.strip()}")

print("\nDone!")
