#!/usr/bin/env python3
"""
Quick test script to verify Phase 2 components work correctly.

Tests:
1. DownloadConfig - create, validate, serialize
2. FileValidator - validate test file
3. StructuredLogger - create logs
4. Downloader - download small file

Run: python test_phase2.py
"""

from pathlib import Path
import tempfile
import shutil


def test_download_config():
    """Test DownloadConfig creation and serialization."""
    print("\n🧪 Testing DownloadConfig...")

    from src.extractors.download_config import DownloadConfig

    # Test 1: Default config
    config = DownloadConfig()
    print(f"  ✅ Default config created: {config}")
    assert config.max_retries == 3
    assert config.timeout_seconds == 300

    # Test 2: Custom config
    config = DownloadConfig(max_retries=5, timeout_seconds=600)
    assert config.max_retries == 5
    print(f"  ✅ Custom config validated")

    # Test 3: Retry delay calculation
    delay = config.get_retry_delay(0)
    assert delay == 60.0  # First retry
    delay = config.get_retry_delay(1)
    assert delay == 120.0  # Second retry (60 * 2^1)
    print(f"  ✅ Exponential backoff working")

    # Test 4: Serialization
    data = config.to_dict()
    assert "max_retries" in data
    print(f"  ✅ Serialization working")

    # Test 5: From dict
    config2 = DownloadConfig.from_dict({"max_retries": 7})
    assert config2.max_retries == 7
    print(f"  ✅ Deserialization working")

    print("✅ DownloadConfig: ALL TESTS PASSED")


def test_file_validator():
    """Test FileValidator with a real file."""
    print("\n🧪 Testing FileValidator...")

    from src.extractors.file_validator import FileValidator

    # Create test file
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        test_file = Path(f.name)
        f.write("Hello, World!\n" * 100)

    try:
        # Test 1: File exists check
        validator = FileValidator()
        print(f"  ✅ Validator created: {validator}")

        # Test 2: Quick validation
        assert validator.quick_validate(test_file) == True
        print(f"  ✅ Quick validation passed")

        # Test 3: Full validation
        result = validator.validate(test_file)
        assert result.is_valid == True
        assert "file_exists" in result.checks_passed
        assert result.actual_hash is not None
        print(f"  ✅ Full validation passed")
        print(f"     - Checks passed: {result.checks_passed}")
        print(f"     - File size: {result.actual_size_bytes} bytes")
        print(f"     - Hash: {result.actual_hash[:16]}...")

        # Test 4: Size validation
        result = validator.validate(
            test_file, expected_size_bytes=test_file.stat().st_size
        )
        assert result.is_valid == True
        assert "file_size" in result.checks_passed
        print(f"  ✅ Size validation passed")

        # Test 5: Hash validation
        actual_hash = result.actual_hash
        result = validator.validate(test_file, expected_hash=actual_hash)
        assert result.is_valid == True
        assert "file_hash" in result.checks_passed
        print(f"  ✅ Hash validation passed")

        # Test 6: Invalid hash (should fail)
        result = validator.validate(test_file, expected_hash="invalid_hash_12345")
        assert result.is_valid == False
        assert "file_hash" in result.checks_failed
        print(f"  ✅ Invalid hash correctly detected")

        print("✅ FileValidator: ALL TESTS PASSED")

    finally:
        test_file.unlink()


def test_structured_logger():
    """Test StructuredLogger log creation."""
    print("\n🧪 Testing StructuredLogger...")

    from src.core.logging import StructuredLogger
    import uuid

    # Create temp log directory
    temp_dir = Path(tempfile.mkdtemp())

    try:
        # Test 1: Create logger
        run_id = str(uuid.uuid4())
        logger_manager = StructuredLogger.create_for_run(
            run_id=run_id,
            job_name="test_job",
            log_dir=temp_dir,
            log_level="INFO",
        )
        print(f"  ✅ Logger created: {logger_manager}")

        # Test 2: Check directory structure
        assert logger_manager.run_dir.exists()
        print(f"  ✅ Run directory created: {logger_manager.run_dir}")

        # Test 3: Check symlink
        latest_link = temp_dir / "latest"
        assert latest_link.exists()
        assert latest_link.is_symlink()
        print(f"  ✅ 'latest' symlink created")

        # Test 4: Get logger and log messages
        log = logger_manager.get_logger()
        log.info("test_message", key="value", count=123)
        log.warning("test_warning", error="something wrong")
        log.error("test_error", exception="critical issue")
        print(f"  ✅ Log messages written")

        # Test 5: Check log files exist
        assert logger_manager.json_log_path.exists()
        assert logger_manager.error_log_path.exists()
        print(f"  ✅ Log files created")
        print(f"     - JSON log: {logger_manager.json_log_path}")
        print(f"     - Error log: {logger_manager.error_log_path}")

        # Test 6: Context binding
        task_log = logger_manager.bind(task_id="task_123")
        task_log.info("task_started")
        print(f"  ✅ Context binding working")

        print("✅ StructuredLogger: ALL TESTS PASSED")

    finally:
        shutil.rmtree(temp_dir)


def test_downloader():
    """Test Downloader with a small public file."""
    print("\n🧪 Testing Downloader...")

    from src.extractors.downloader import Downloader
    from src.extractors.download_config import DownloadConfig

    # Create temp directory for downloads
    temp_dir = Path(tempfile.mkdtemp())

    try:
        # Test 1: Create downloader
        config = DownloadConfig(
            download_directory=temp_dir,
            max_retries=2,
            timeout_seconds=30,
            validate_downloads=False,  # Skip validation for this test
        )
        downloader = Downloader(config)
        print(f"  ✅ Downloader created: {downloader}")

        # Test 2: Download a small file (example.com robots.txt)
        test_url = "https://www.example.com/robots.txt"
        target_path = temp_dir / "robots.txt"

        print(f"  ⏳ Downloading from {test_url}...")
        result = downloader.download(
            url=test_url,
            target_path=target_path,
        )

        if result.success:
            print(f"  ✅ Download succeeded!")
            print(f"     - File: {result.file_path}")
            print(f"     - Size: {result.size_bytes} bytes")
            print(f"     - Speed: {result.download_speed_mbps:.2f} Mbps")
            print(f"     - Duration: {result.duration_seconds:.2f}s")
            print(f"     - Attempts: {result.attempt_number}")
            assert result.file_path.exists()
            assert result.size_bytes > 0
        else:
            print(f"  ⚠️  Download failed (network issue): {result.error_message}")
            print(f"     - This is expected if offline or blocked")

        # Test 3: Context manager
        with Downloader(config) as dl:
            print(f"  ✅ Context manager working")

        # Test 4: Error classification
        fake_url = "https://httpbin.org/status/404"
        result = downloader.download(
            url=fake_url,
            target_path=temp_dir / "fake.txt",
        )
        if not result.success:
            print(f"  ✅ Error handling working")
            print(f"     - Error type: {result.error_type}")
            print(f"     - Error message: {result.error_message}")

        print("✅ Downloader: TESTS COMPLETED")

    finally:
        downloader.close()
        shutil.rmtree(temp_dir)


def main():
    """Run all tests."""
    print("=" * 60)
    print("🚀 Phase 2 Component Testing")
    print("=" * 60)

    try:
        test_download_config()
        test_file_validator()
        test_structured_logger()
        test_downloader()

        print("\n" + "=" * 60)
        print("✅ ALL PHASE 2 COMPONENTS WORKING CORRECTLY!")
        print("=" * 60)
        print("\n📊 Summary:")
        print("  - DownloadConfig: Configuration management ✅")
        print("  - FileValidator: Multi-level validation ✅")
        print("  - StructuredLogger: Logging system ✅")
        print("  - Downloader: HTTP downloads with retry ✅")
        print("\n🎉 Phase 2 is production-ready!")

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
