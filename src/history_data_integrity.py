"""Portable content hashes and path guards for historical CSV backfills."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import uuid


CANONICAL_HASH_POLICY = "CSV_LINE_ENDINGS_LF_V1"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonicalize_csv_line_endings(value: bytes) -> bytes:
    """Normalize CRLF and LF to LF only; reject lone CR and preserve all fields."""
    normalized = value.replace(b"\r\n", b"\n")
    if b"\r" in normalized:
        raise ValueError("CSV_CANONICALIZATION_UNSUPPORTED_LONE_CR")
    return normalized


def content_hashes(value: bytes) -> dict[str, str]:
    return {
        "raw_sha256": sha256_bytes(value),
        "canonical_sha256": sha256_bytes(canonicalize_csv_line_endings(value)),
        "canonicalization_policy": CANONICAL_HASH_POLICY,
    }


def file_content_hashes(path: Path) -> dict[str, str]:
    return content_hashes(path.read_bytes())


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("RUN_ID_INVALID")
    if run_id.upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError("RUN_ID_WINDOWS_RESERVED")
    return run_id


def ensure_no_symlink_path(root: Path, path: Path) -> Path:
    """Resolve a path and reject symlinked components or escape from root."""
    trusted_root = root.resolve(strict=True)
    candidate = path if path.is_absolute() else trusted_root / path
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(trusted_root)
    except ValueError:
        raise ValueError("BACKFILL_PATH_OUTSIDE_AUTHORIZED_ROOT") from None

    current = trusted_root
    for component in relative.parts:
        current = current / component
        try:
            if current.is_symlink():
                raise ValueError("BACKFILL_SYMLINK_PATH_REJECTED")
        except OSError:
            raise ValueError("BACKFILL_PATH_INSPECTION_FAILED") from None
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(trusted_root)
    except ValueError:
        raise ValueError("BACKFILL_PATH_OUTSIDE_AUTHORIZED_ROOT") from None
    return candidate


def backfill_run_paths(root: Path, run_id: str, *, create: bool = False) -> dict[str, Path]:
    run_id = validate_run_id(run_id)
    trusted_root = root.resolve(strict=True)
    base = trusted_root / "data" / "market_history" / "backfill_runs"
    ensure_no_symlink_path(trusted_root, base)
    if create:
        current = trusted_root
        for component in ("data", "market_history", "backfill_runs"):
            current = ensure_no_symlink_path(trusted_root, current / component)
            if current.exists() and not current.is_dir():
                raise ValueError("BACKFILL_PATH_COMPONENT_NOT_DIRECTORY")
            current.mkdir(exist_ok=True)
            ensure_no_symlink_path(trusted_root, current)

    run_root = ensure_no_symlink_path(trusted_root, base / run_id)
    if run_root.exists() and not run_root.is_dir():
        raise ValueError("BACKFILL_RUN_PATH_NOT_DIRECTORY")
    chunks = ensure_no_symlink_path(trusted_root, run_root / "chunks")
    if chunks.exists() and not chunks.is_dir():
        raise ValueError("BACKFILL_CHUNK_PATH_NOT_DIRECTORY")
    manifest = ensure_no_symlink_path(trusted_root, run_root / "manifest.json")
    if create:
        run_root.mkdir(exist_ok=True)
        ensure_no_symlink_path(trusted_root, run_root)
        chunks.mkdir(exist_ok=True)
        ensure_no_symlink_path(trusted_root, chunks)
    return {"base": base, "run_root": run_root, "chunks": chunks, "manifest": manifest}


def safe_atomic_write_bytes(path: Path, payload: bytes, *, authorized_root: Path, overwrite: bool = True) -> None:
    """Atomically write inside an approved tree without following symlinks."""
    ensure_no_symlink_path(authorized_root, path)
    if path.exists() and not overwrite:
        raise FileExistsError("BACKFILL_OUTPUT_WOULD_OVERWRITE_EXISTING_VERSION")
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_no_symlink_path(authorized_root, path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    ensure_no_symlink_path(authorized_root, temporary)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_no_symlink_path(authorized_root, path)
        if path.exists() and not overwrite:
            raise FileExistsError("BACKFILL_OUTPUT_WOULD_OVERWRITE_EXISTING_VERSION")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
