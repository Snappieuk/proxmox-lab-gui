#!/usr/bin/env python3
"""
Tests for QCOW2 Bulk Cloner Service.

Run with: python -m pytest tests/test_qcow2_bulk_cloner.py -v
Or directly: python tests/test_qcow2_bulk_cloner.py
"""

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestValidation:
    """Test input validation functions."""
    
    def test_validate_template_disk_empty_path(self):
        """Test validation with empty path."""
        from app.services.qcow2_bulk_cloner import _validate_template_disk
        
        valid, error = _validate_template_disk("")
        assert valid is False
        assert "empty" in error.lower()
    
    def test_validate_template_disk_nonexistent(self):
        """Test validation with non-existent path."""
        from app.services.qcow2_bulk_cloner import _validate_template_disk
        
        valid, error = _validate_template_disk("/nonexistent/path/disk.qcow2")
        assert valid is False
        assert "not found" in error.lower()
    
    def test_validate_template_disk_valid_file(self):
        """Test validation with valid file."""
        from app.services.qcow2_bulk_cloner import _validate_template_disk
        
        # Create a temp file to validate
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test content")
            temp_path = f.name
        
        try:
            valid, error = _validate_template_disk(temp_path)
            assert valid is True
            assert error == ""
        finally:
            os.unlink(temp_path)
    
    def test_validate_template_disk_directory(self):
        """Test validation with directory path."""
        from app.services.qcow2_bulk_cloner import _validate_template_disk
        
        with tempfile.TemporaryDirectory() as tmpdir:
            valid, error = _validate_template_disk(tmpdir)
            assert valid is False
            assert "not a file" in error.lower()
    
    def test_validate_storage_empty_id(self):
        """Test storage validation with empty ID."""
        from app.services.qcow2_bulk_cloner import _validate_storage
        
        valid, error = _validate_storage("")
        assert valid is False
        assert "empty" in error.lower()
    
    def test_validate_storage_invalid_format(self):
        """Test storage validation with invalid format."""
        from app.services.qcow2_bulk_cloner import _validate_storage
        
        valid, error = _validate_storage("123-invalid")
        assert valid is False
        assert "invalid" in error.lower()
    
    def test_validate_storage_valid_id(self):
        """Test storage validation with valid ID."""
        from app.services.qcow2_bulk_cloner import _validate_storage
        
        valid, error = _validate_storage("local-lvm")
        assert valid is True
        assert error == ""
        
        valid, error = _validate_storage("TRUENAS_NFS")
        assert valid is True
        assert error == ""
    
    def test_validate_storage_with_path(self):
        """Test storage validation with explicit path."""
        from app.services.qcow2_bulk_cloner import _validate_storage
        
        with tempfile.TemporaryDirectory() as tmpdir:
            valid, error = _validate_storage("local", storage_path=tmpdir)
            assert valid is True
            assert error == ""
    
    def test_validate_storage_nonexistent_path(self):
        """Test storage validation with non-existent path."""
        from app.services.qcow2_bulk_cloner import _validate_storage
        
        valid, error = _validate_storage("local", storage_path="/nonexistent/path")
        assert valid is False
        assert "not found" in error.lower()


class TestCloudInitOptions:
    """Test CloudInitOptions dataclass."""
    
    def test_cloud_init_defaults(self):
        """Test CloudInitOptions default values."""
        from app.services.qcow2_bulk_cloner import CloudInitOptions
        
        opts = CloudInitOptions()
        assert opts.username == "user"
        assert opts.password is None
        assert opts.ssh_keys is None
        assert opts.ip_config == "dhcp"
        assert opts.dns_servers is None
        assert opts.upgrade is False
    
    def test_cloud_init_custom_values(self):
        """Test CloudInitOptions with custom values."""
        from app.services.qcow2_bulk_cloner import CloudInitOptions
        
        opts = CloudInitOptions(
            username="student",
            password="pass123",
            ssh_keys=["ssh-rsa AAAA..."],
            ip_config="ip=192.168.1.100/24,gw=192.168.1.1",
            dns_servers=["8.8.8.8", "8.8.4.4"],
        )
        assert opts.username == "student"
        assert opts.password == "pass123"
        assert len(opts.ssh_keys) == 1
        assert "192.168.1.100" in opts.ip_config


class TestCloneResult:
    """Test CloneResult dataclass."""
    
    def test_clone_result_success(self):
        """Test CloneResult for successful clone."""
        from app.services.qcow2_bulk_cloner import CloneResult
        
        result = CloneResult(
            vmid=100,
            name="test-vm-1",
            success=True,
            node="prox1",
        )
        assert result.vmid == 100
        assert result.name == "test-vm-1"
        assert result.success is True
        assert result.error is None
        assert result.started is False
    
    def test_clone_result_failure(self):
        """Test CloneResult for failed clone."""
        from app.services.qcow2_bulk_cloner import CloneResult
        
        result = CloneResult(
            vmid=101,
            name="test-vm-2",
            success=False,
            error="VMID already in use",
        )
        assert result.success is False
        assert "VMID" in result.error


class TestBulkCloneResult:
    """Test BulkCloneResult dataclass."""
    
    def test_bulk_clone_result_init(self):
        """Test BulkCloneResult initialization."""
        from app.services.qcow2_bulk_cloner import BulkCloneResult
        
        result = BulkCloneResult(
            total_requested=10,
            successful=8,
            failed=2,
        )
        assert result.total_requested == 10
        assert result.successful == 8
        assert result.failed == 2
        assert result.results == []
        assert result.base_qcow2_path is None
        assert result.error is None


class TestDryRun:
    """Test dry run functionality."""
    
    def test_bulk_clone_dry_run_invalid_template(self):
        """Test dry run with invalid template path."""
        from app.services.qcow2_bulk_cloner import bulk_clone_from_template
        
        result = bulk_clone_from_template(
            template_disk_path="/nonexistent/template.qcow2",
            base_storage_id="local-lvm",
            vmid_start=200,
            count=5,
            dry_run=True,
        )
        
        # Should fail validation even in dry run
        assert result.error is not None
        assert "not found" in result.error.lower()
        assert result.successful == 0
    
    def test_bulk_clone_dry_run_valid(self):
        """Test dry run with valid inputs."""
        from app.services.qcow2_bulk_cloner import bulk_clone_from_template
        
        # Create a temp file as template
        with tempfile.NamedTemporaryFile(suffix=".qcow2", delete=False) as f:
            f.write(b"fake qcow2 content")
            temp_path = f.name
        
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                result = bulk_clone_from_template(
                    template_disk_path=temp_path,
                    base_storage_id="local-lvm",
                    vmid_start=200,
                    count=3,
                    name_prefix="test-vm",
                    storage_path=tmpdir,
                    dry_run=True,
                )
                
                # Dry run should succeed but not actually create VMs
                assert result.error is None or result.successful > 0
                assert result.total_requested == 3
            finally:
                os.unlink(temp_path)


class TestVMIDAllocation:
    """Test VMID allocation logic."""
    
    @patch('app.services.qcow2_bulk_cloner._run_command')
    def test_get_next_available_vmid_dry_run(self, mock_run):
        """Test VMID allocation in dry run mode."""
        from app.services.qcow2_bulk_cloner import _get_next_available_vmid
        
        vmids = _get_next_available_vmid(start_vmid=100, count=5, dry_run=True)
        
        assert len(vmids) == 5
        assert vmids == [100, 101, 102, 103, 104]
        # No qm commands should be run in dry run
        mock_run.assert_not_called()
    
    @patch('app.services.qcow2_bulk_cloner._run_command')
    def test_get_next_available_vmid_skips_used(self, mock_run):
        """Test VMID allocation skips used IDs."""
        from app.services.qcow2_bulk_cloner import _get_next_available_vmid
        
        # Mock: VMIDs 100, 102 are in use
        def mock_qm_status(cmd, **kwargs):
            vmid = int(cmd[2])  # cmd = ["qm", "status", "100"]
            result = MagicMock()
            result.returncode = 0 if vmid in [100, 102] else 1
            return result
        
        mock_run.side_effect = mock_qm_status
        
        vmids = _get_next_available_vmid(start_vmid=100, count=3, dry_run=False)
        
        assert 100 not in vmids  # Used
        assert 102 not in vmids  # Used
        assert len(vmids) == 3


class TestTimeEstimation:
    """Test time estimation function."""
    
    def test_estimate_clone_time_basic(self):
        """Test time estimation returns reasonable values."""
        from app.services.qcow2_bulk_cloner import estimate_clone_time
        
        # 10 VMs, 20GB template, 5 concurrent
        time_secs = estimate_clone_time(count=10, template_size_gb=20.0, concurrency=5)
        
        # Should be at least a few seconds
        assert time_secs > 0
        # Should be under an hour for 10 VMs
        assert time_secs < 3600
    
    def test_estimate_clone_time_scales_with_count(self):
        """Test time estimation scales with VM count."""
        from app.services.qcow2_bulk_cloner import estimate_clone_time
        
        time_10 = estimate_clone_time(count=10, template_size_gb=20.0, concurrency=5)
        time_100 = estimate_clone_time(count=100, template_size_gb=20.0, concurrency=5)
        
        # 100 VMs should take longer than 10
        assert time_100 > time_10


class TestModuleExports:
    """Test that module exports expected functions."""
    
    def test_bulk_clone_from_template_exists(self):
        """Test main function is exported."""
        from app.services.qcow2_bulk_cloner import bulk_clone_from_template
        assert callable(bulk_clone_from_template)
    
    def test_dataclasses_exported(self):
        """Test dataclasses are exported."""
        from app.services.qcow2_bulk_cloner import (
            CloudInitOptions,
            CloneResult,
            BulkCloneResult,
        )
        assert CloudInitOptions is not None
        assert CloneResult is not None
        assert BulkCloneResult is not None
    
    def test_estimate_function_exists(self):
        """Test estimate function is exported."""
        from app.services.qcow2_bulk_cloner import estimate_clone_time
        assert callable(estimate_clone_time)


class TestIntegrationWithExistingCode:
    """Test integration with existing proxmox_operations module."""
    
    def test_can_import_alongside_proxmox_operations(self):
        """Test new module can be imported alongside existing code."""
        from app.services.proxmox_operations import clone_vm_from_template
        from app.services.qcow2_bulk_cloner import bulk_clone_from_template
        
        # Both should be callable
        assert callable(clone_vm_from_template)
        assert callable(bulk_clone_from_template)
    
    def test_sanitize_vm_name_similar_behavior(self):
        """Test VM name sanitization follows similar rules."""
        from app.services.proxmox_operations import sanitize_vm_name
        
        # Test that existing sanitization works
        # Our new module uses similar inline logic
        name1 = sanitize_vm_name("Test VM 123!")
        assert name1.islower() or name1.replace('-', '').isalnum()
        
        name2 = sanitize_vm_name("with--double--dashes")
        assert "--" not in name2


def run_all_tests():
    """Run all tests."""
    print("\n=== Running QCOW2 Bulk Cloner Tests ===\n")
    
    test_classes = [
        TestValidation,
        TestCloudInitOptions,
        TestCloneResult,
        TestBulkCloneResult,
        TestDryRun,
        TestVMIDAllocation,
        TestTimeEstimation,
        TestModuleExports,
        TestIntegrationWithExistingCode,
    ]
    
    passed = 0
    failed = 0
    
    for test_class in test_classes:
        instance = test_class()
        for method_name in dir(instance):
            if method_name.startswith('test_'):
                try:
                    getattr(instance, method_name)()
                    print(f"✓ {test_class.__name__}.{method_name}")
                    passed += 1
                except Exception as e:
                    print(f"✗ {test_class.__name__}.{method_name}: {e}")
                    failed += 1
    
    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
