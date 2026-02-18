"""Folder manager — handles category folder creation, matching, and file placement."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .config import SorterConfig


def get_existing_categories(output_dir: Path) -> list[str]:
    """Scan the output directory and return existing category folder names.

    Only considers immediate subdirectories (ignores files at the root).
    """
    if not output_dir.exists():
        return []
    return sorted(
        d.name for d in output_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def ensure_category_folder(output_dir: Path, category: str) -> Path:
    """Create the category folder (and any parent subdirs) if it doesn't exist.

    Supports nested categories like 'Insurance/Auto/Claims'.
    Returns the Path to the (possibly new) folder.
    """
    folder = output_dir / category
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def ensure_unidentified_folder(config: SorterConfig) -> Path:
    """Return the path to the 'Unidentified' folder, creating it if needed."""
    return ensure_category_folder(config.output_dir, config.unidentified_dir_name)


def _file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file for duplicate detection."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def find_duplicate(file_path: Path, dest_folder: Path) -> Path | None:
    """Check if a byte-identical file already exists in the destination folder.

    Returns the path of the existing duplicate, or None.
    """
    src_hash = _file_hash(file_path)
    for existing in dest_folder.iterdir():
        if existing.is_file() and _file_hash(existing) == src_hash:
            return existing
    return None


def _unique_dest_path(dest_folder: Path, filename: str, extension: str) -> Path:
    """Generate a unique file path in dest_folder, appending (1), (2), etc. if needed."""
    candidate = dest_folder / f"{filename}{extension}"
    counter = 1
    while candidate.exists():
        candidate = dest_folder / f"{filename} ({counter}){extension}"
        counter += 1
    return candidate


def place_file(
    source: Path,
    dest_folder: Path,
    suggested_filename: str | None,
    config: SorterConfig,
) -> tuple[Path, bool]:
    """Copy or move a file into the destination folder.

    Args:
        source: Original file path.
        dest_folder: Target category folder.
        suggested_filename: Optional descriptive name (without extension).
        config: Sorter configuration.

    Returns:
        (destination_path, was_duplicate): The path where the file ended up
        and whether a duplicate was detected.
    """
    extension = source.suffix

    # Use suggested filename if available, otherwise keep original name
    if suggested_filename:
        # Sanitize: remove characters that are problematic in filenames
        clean_name = "".join(
            c for c in suggested_filename if c not in r'\/:*?"<>|'
        ).strip()
        if clean_name:
            filename = clean_name
        else:
            filename = source.stem
    else:
        filename = source.stem

    # Check for duplicates
    was_duplicate = False
    if config.detect_duplicates:
        dup = find_duplicate(source, dest_folder)
        if dup is not None:
            was_duplicate = True
            # Still place the file but mark it
            filename = f"{filename} (DUPLICATE of {dup.stem})"

    dest_path = _unique_dest_path(dest_folder, filename, extension)

    if not config.dry_run:
        if config.move_files:
            shutil.move(str(source), str(dest_path))
        else:
            shutil.copy2(str(source), str(dest_path))

    return dest_path, was_duplicate
