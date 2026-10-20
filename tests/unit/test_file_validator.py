"""Tests for FileValidator."""

import hashlib
import zipfile

import pytest

from src.etl.utils.file_validator import FileValidator, ValidationResult
from src.shared.enums import ErrorType


@pytest.mark.unit
class TestFileValidatorInit:
    """Test FileValidator initialization."""

    def test_default_algorithm(self):
        v = FileValidator()
        assert v.hash_algorithm == "sha256"

    def test_custom_algorithm(self):
        v = FileValidator(hash_algorithm="md5")
        assert v.hash_algorithm == "md5"

    def test_unsupported_algorithm_raises(self):
        with pytest.raises(ValueError, match="Unsupported hash algorithm"):
            FileValidator(hash_algorithm="blake2")

    def test_supported_algorithms(self):
        for algo in ["md5", "sha1", "sha256", "sha512"]:
            v = FileValidator(hash_algorithm=algo)
            assert v.hash_algorithm == algo

    def test_algorithm_case_insensitive(self):
        v = FileValidator(hash_algorithm="SHA256")
        assert v.hash_algorithm == "sha256"

    def test_custom_tolerance(self):
        v = FileValidator(size_tolerance_percent=10.0)
        assert v.size_tolerance_percent == 10.0

    def test_repr(self):
        v = FileValidator()
        r = repr(v)
        assert "FileValidator" in r
        assert "sha256" in r


@pytest.mark.unit
class TestFileValidatorExistence:
    """Test file existence check."""

    def test_existing_file_passes(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("content")
        v = FileValidator()
        result = v.validate(f, validate_zip=False)
        assert result.is_valid
        assert "file_exists" in result.checks_passed

    def test_nonexistent_file_fails(self, tmp_path):
        f = tmp_path / "missing.txt"
        v = FileValidator()
        result = v.validate(f, validate_zip=False)
        assert not result.is_valid
        assert "file_exists" in result.checks_failed
        assert result.error_type == ErrorType.VALIDATION_ERROR

    def test_empty_file_fails(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        v = FileValidator()
        result = v.validate(f, validate_zip=False)
        assert not result.is_valid
        assert "file_exists" in result.checks_failed

    def test_quick_validate_existing(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("content")
        v = FileValidator()
        assert v.quick_validate(f) is True

    def test_quick_validate_missing(self, tmp_path):
        v = FileValidator()
        assert v.quick_validate(tmp_path / "missing.txt") is False


@pytest.mark.unit
class TestFileValidatorSize:
    """Test file size validation."""

    def test_exact_size_passes(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"x" * 100)
        v = FileValidator(size_tolerance_percent=5.0)
        result = v.validate(f, expected_size_bytes=100, validate_zip=False)
        assert result.is_valid
        assert "file_size" in result.checks_passed

    def test_size_within_tolerance_passes(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"x" * 104)  # 4% over
        v = FileValidator(size_tolerance_percent=5.0)
        result = v.validate(f, expected_size_bytes=100, validate_zip=False)
        assert result.is_valid
        assert "file_size" in result.checks_passed

    def test_size_exceeds_tolerance_fails(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"x" * 200)  # 100% over
        v = FileValidator(size_tolerance_percent=5.0)
        result = v.validate(f, expected_size_bytes=100, validate_zip=False)
        assert not result.is_valid
        assert "file_size" in result.checks_failed
        assert result.error_type == ErrorType.SIZE_MISMATCH

    def test_size_not_checked_when_no_expected(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"x" * 100)
        v = FileValidator()
        result = v.validate(f, validate_zip=False)
        assert "file_size" not in result.checks_passed
        assert "file_size" not in result.checks_failed


@pytest.mark.unit
class TestFileValidatorHash:
    """Test hash verification."""

    def test_correct_hash_passes(self, tmp_path):
        f = tmp_path / "test.txt"
        content = b"test content"
        f.write_bytes(content)
        expected_hash = hashlib.sha256(content).hexdigest()

        v = FileValidator(hash_algorithm="sha256")
        result = v.validate(f, expected_hash=expected_hash, validate_zip=False)
        assert result.is_valid
        assert "file_hash" in result.checks_passed

    def test_wrong_hash_fails(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"test content")

        v = FileValidator()
        result = v.validate(f, expected_hash="badhash", validate_zip=False)
        assert not result.is_valid
        assert "file_hash" in result.checks_failed
        assert result.error_type == ErrorType.HASH_MISMATCH

    def test_hash_case_insensitive(self, tmp_path):
        f = tmp_path / "test.txt"
        content = b"test content"
        f.write_bytes(content)
        expected_hash = hashlib.sha256(content).hexdigest().upper()

        v = FileValidator()
        result = v.validate(f, expected_hash=expected_hash, validate_zip=False)
        assert result.is_valid

    def test_hash_calculated_even_without_expected(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"test content")
        v = FileValidator()
        result = v.validate(f, validate_zip=False)
        assert result.actual_hash is not None

    def test_md5_hash(self, tmp_path):
        f = tmp_path / "test.txt"
        content = b"test content"
        f.write_bytes(content)
        expected_hash = hashlib.md5(content).hexdigest()

        v = FileValidator(hash_algorithm="md5")
        result = v.validate(f, expected_hash=expected_hash, validate_zip=False)
        assert result.is_valid


@pytest.mark.unit
class TestFileValidatorZip:
    """Test ZIP file validation."""

    def test_valid_zip_passes(self, tmp_path):
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("data.csv", "a,b,c\n1,2,3")

        v = FileValidator()
        result = v.validate(zip_path, validate_zip=True)
        assert result.is_valid
        assert "zip_integrity" in result.checks_passed

    def test_corrupt_zip_fails(self, tmp_path):
        zip_path = tmp_path / "corrupt.zip"
        zip_path.write_bytes(b"not a zip file at all")

        v = FileValidator()
        result = v.validate(zip_path, validate_zip=True)
        assert not result.is_valid
        assert "zip_integrity" in result.checks_failed
        assert result.error_type == ErrorType.ZIP_CORRUPT

    def test_zip_not_validated_for_non_zip(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("not a zip")
        v = FileValidator()
        result = v.validate(f, validate_zip=True)
        assert result.is_valid
        assert "zip_integrity" not in result.checks_passed
        assert "zip_integrity" not in result.checks_failed

    def test_zip_contents_check_passes(self, tmp_path):
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("data.csv", "a,b,c")
            zf.writestr("readme.txt", "info")

        v = FileValidator()
        result = v.validate(
            zip_path,
            validate_zip=True,
            expected_zip_files=["data.csv", "readme.txt"],
        )
        assert result.is_valid
        assert "zip_contents" in result.checks_passed

    def test_zip_contents_check_fails_missing(self, tmp_path):
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("data.csv", "a,b,c")

        v = FileValidator()
        result = v.validate(
            zip_path,
            validate_zip=True,
            expected_zip_files=["data.csv", "missing.csv"],
        )
        assert not result.is_valid
        assert "zip_contents" in result.checks_failed
        assert result.error_type == ErrorType.VALIDATION_ERROR


@pytest.mark.unit
class TestValidationResult:
    """Test ValidationResult model."""

    def test_valid_result(self):
        result = ValidationResult(
            is_valid=True,
            checks_passed=["file_exists", "file_hash"],
        )
        assert result.is_valid
        assert len(result.checks_passed) == 2
        assert len(result.checks_failed) == 0

    def test_invalid_result(self):
        result = ValidationResult(
            is_valid=False,
            checks_failed=["file_hash"],
            error_type=ErrorType.HASH_MISMATCH,
            error_message="Hash mismatch",
        )
        assert not result.is_valid
        assert result.error_type == ErrorType.HASH_MISMATCH
