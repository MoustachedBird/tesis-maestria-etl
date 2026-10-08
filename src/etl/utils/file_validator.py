"""
File validation for downloaded files.

Provides multi-level validation including file existence, size,
hash verification, and ZIP integrity checks.
"""

import hashlib
import zipfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src.meta.models import ErrorType


class ValidationResult(BaseModel):
    """
    Result of file validation with detailed check information.

    Attributes:
        is_valid: Overall validation result
        checks_passed: List of validation checks that passed
        checks_failed: List of validation checks that failed
        error_type: Classification of validation error (if any)
        error_message: Detailed error message (if any)
        file_path: Path to validated file
        actual_size_bytes: Actual file size
        actual_hash: Actual file hash
    """

    is_valid: bool = Field(description="Overall validation result")
    checks_passed: list[str] = Field(default_factory=list, description="Passed checks")
    checks_failed: list[str] = Field(default_factory=list, description="Failed checks")
    error_type: ErrorType | None = Field(default=None, description="Error classification")
    error_message: str | None = Field(default=None, description="Error details")
    file_path: Path | None = Field(default=None, description="Validated file path")
    actual_size_bytes: int | None = Field(default=None, description="Actual file size")
    actual_hash: str | None = Field(default=None, description="Actual file hash")

    model_config = ConfigDict(arbitrary_types_allowed=True)


class FileValidator:
    """
    Multi-level file validator for downloaded files.

    Performs 5 levels of validation:
    1. File exists and is readable
    2. File size matches expected (within tolerance)
    3. Hash verification (SHA256 by default)
    4. ZIP file integrity (can be opened)
    5. ZIP contents validation (expected files present)

    Example:
        >>> validator = FileValidator()
        >>> result = validator.validate(
        ...     file_path=Path("data.zip"),
        ...     expected_size_bytes=1048576,
        ...     expected_hash="abc123...",
        ... )
        >>> if result.is_valid:
        ...     print("File is valid!")
        ... else:
        ...     print(f"Validation failed: {result.error_message}")
    """

    def __init__(
        self,
        hash_algorithm: str = "sha256",
        size_tolerance_percent: float = 5.0,
    ):
        """
        Initialize file validator.

        Args:
            hash_algorithm: Hash algorithm to use (md5, sha1, sha256, sha512)
            size_tolerance_percent: Acceptable file size variation percentage
        """
        self.hash_algorithm = hash_algorithm.lower()
        self.size_tolerance_percent = size_tolerance_percent

        # Validate hash algorithm
        supported_algorithms = ["md5", "sha1", "sha256", "sha512"]
        if self.hash_algorithm not in supported_algorithms:
            raise ValueError(
                f"Unsupported hash algorithm: {hash_algorithm}. "
                f"Supported: {', '.join(supported_algorithms)}"
            )

    def validate(
        self,
        file_path: Path,
        expected_size_bytes: int | None = None,
        expected_hash: str | None = None,
        validate_zip: bool = True,
        expected_zip_files: list[str] | None = None,
    ) -> ValidationResult:
        """
        Perform multi-level file validation.

        Args:
            file_path: Path to file to validate
            expected_size_bytes: Expected file size in bytes
            expected_hash: Expected file hash
            validate_zip: Whether to validate ZIP integrity
            expected_zip_files: List of expected files in ZIP archive

        Returns:
            ValidationResult with detailed check information
        """
        checks_passed = []
        checks_failed = []
        error_type = None
        error_message = None
        actual_size = None
        actual_hash = None

        try:
            # Check 1: File exists and readable
            if not self._check_file_exists(file_path):
                checks_failed.append("file_exists")
                error_type = ErrorType.VALIDATION_ERROR
                error_message = f"File does not exist or is not readable: {file_path}"
                return ValidationResult(
                    is_valid=False,
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                    error_type=error_type,
                    error_message=error_message,
                    file_path=file_path,
                )
            checks_passed.append("file_exists")

            # Get actual file size
            actual_size = file_path.stat().st_size

            # Check 2: File size (if expected size provided)
            if expected_size_bytes is not None:
                if self._check_file_size(file_path, expected_size_bytes):
                    checks_passed.append("file_size")
                else:
                    checks_failed.append("file_size")
                    error_type = ErrorType.SIZE_MISMATCH
                    variance = abs(actual_size - expected_size_bytes)
                    error_message = (
                        f"File size mismatch: expected {expected_size_bytes} bytes, "
                        f"got {actual_size} bytes (variance: {variance} bytes, "
                        f"{(variance / expected_size_bytes * 100):.1f}%)"
                    )

            # Check 3: Hash verification (if expected hash provided)
            if expected_hash is not None:
                actual_hash = self._calculate_hash(file_path)
                if self._check_hash(actual_hash, expected_hash):
                    checks_passed.append("file_hash")
                else:
                    checks_failed.append("file_hash")
                    error_type = ErrorType.HASH_MISMATCH
                    error_message = f"Hash mismatch: expected {expected_hash}, got {actual_hash}"
            else:
                # Calculate hash even if not validating for metadata purposes
                actual_hash = self._calculate_hash(file_path)

            # Check 4: ZIP integrity (if file is ZIP and validation enabled)
            if validate_zip and file_path.suffix.lower() == ".zip":
                if self._check_zip_integrity(file_path):
                    checks_passed.append("zip_integrity")
                else:
                    checks_failed.append("zip_integrity")
                    error_type = ErrorType.ZIP_CORRUPT
                    error_message = f"ZIP file is corrupted: {file_path}"

            # Check 5: ZIP contents (if expected files provided)
            if expected_zip_files and file_path.suffix.lower() == ".zip":
                missing_files = self._check_zip_contents(file_path, expected_zip_files)
                if not missing_files:
                    checks_passed.append("zip_contents")
                else:
                    checks_failed.append("zip_contents")
                    error_type = ErrorType.VALIDATION_ERROR
                    error_message = f"ZIP missing expected files: {', '.join(missing_files)}"

            # Determine overall validation result
            is_valid = len(checks_failed) == 0

            return ValidationResult(
                is_valid=is_valid,
                checks_passed=checks_passed,
                checks_failed=checks_failed,
                error_type=error_type if not is_valid else None,
                error_message=error_message if not is_valid else None,
                file_path=file_path,
                actual_size_bytes=actual_size,
                actual_hash=actual_hash,
            )

        except Exception as e:
            # Unexpected error during validation
            return ValidationResult(
                is_valid=False,
                checks_passed=checks_passed,
                checks_failed=["validation_error"],
                error_type=ErrorType.UNKNOWN,
                error_message=f"Validation error: {str(e)}",
                file_path=file_path,
                actual_size_bytes=actual_size,
                actual_hash=actual_hash,
            )

    def _check_file_exists(self, file_path: Path) -> bool:
        """
        Check if file exists and is readable.

        Args:
            file_path: Path to file

        Returns:
            True if file exists and is readable
        """
        try:
            return file_path.exists() and file_path.is_file() and file_path.stat().st_size > 0
        except (OSError, PermissionError):
            return False

    def _check_file_size(self, file_path: Path, expected_size_bytes: int) -> bool:
        """
        Check if file size matches expected size within tolerance.

        Args:
            file_path: Path to file
            expected_size_bytes: Expected file size

        Returns:
            True if size is within tolerance
        """
        actual_size = file_path.stat().st_size
        tolerance = expected_size_bytes * (self.size_tolerance_percent / 100.0)
        lower_bound = expected_size_bytes - tolerance
        upper_bound = expected_size_bytes + tolerance
        return lower_bound <= actual_size <= upper_bound

    def _calculate_hash(self, file_path: Path) -> str:
        """
        Calculate file hash.

        Args:
            file_path: Path to file

        Returns:
            Hexadecimal hash string
        """
        hash_func = hashlib.new(self.hash_algorithm)

        # Read file in chunks for memory efficiency
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                hash_func.update(chunk)

        return hash_func.hexdigest()

    def _check_hash(self, actual_hash: str, expected_hash: str) -> bool:
        """
        Compare actual hash with expected hash (case-insensitive).

        Args:
            actual_hash: Calculated hash
            expected_hash: Expected hash

        Returns:
            True if hashes match
        """
        return actual_hash.lower() == expected_hash.lower()

    def _check_zip_integrity(self, file_path: Path) -> bool:
        """
        Check if ZIP file can be opened and is not corrupted.

        Args:
            file_path: Path to ZIP file

        Returns:
            True if ZIP file is valid
        """
        try:
            with zipfile.ZipFile(file_path, "r") as zip_file:
                # Test ZIP integrity
                bad_file = zip_file.testzip()
                return bad_file is None
        except (zipfile.BadZipFile, OSError):
            return False

    def _check_zip_contents(self, file_path: Path, expected_files: list[str]) -> list[str]:
        """
        Check if ZIP contains all expected files.

        Args:
            file_path: Path to ZIP file
            expected_files: List of expected file names/patterns

        Returns:
            List of missing files (empty if all present)
        """
        try:
            with zipfile.ZipFile(file_path, "r") as zip_file:
                zip_contents = set(zip_file.namelist())
                missing_files = []

                for expected_file in expected_files:
                    if expected_file not in zip_contents:
                        missing_files.append(expected_file)

                return missing_files
        except (zipfile.BadZipFile, OSError):
            return expected_files  # All files considered missing if can't open

    def quick_validate(self, file_path: Path) -> bool:
        """
        Quick validation: just check if file exists and is readable.

        Args:
            file_path: Path to file

        Returns:
            True if file exists and is readable
        """
        return self._check_file_exists(file_path)

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"FileValidator("
            f"algorithm={self.hash_algorithm}, "
            f"tolerance={self.size_tolerance_percent}%)"
        )
