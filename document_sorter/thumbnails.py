"""Thumbnail generation for document previews."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

THUMB_SIZE = (300, 400)


def _cache_key(file_path: Path) -> str:
    """Generate a stable cache key from file path and mtime."""
    stat = file_path.stat()
    raw = f"{file_path.resolve()}:{stat.st_mtime}:{stat.st_size}"
    return hashlib.md5(raw.encode()).hexdigest()


def generate_thumbnail(file_path: Path, cache_dir: Path) -> Path | None:
    """Generate a thumbnail for a document file.

    Returns the path to a cached PNG thumbnail, or None if generation fails.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(file_path)
    cached = cache_dir / f"{key}.png"

    if cached.exists():
        return cached

    suffix = file_path.suffix.lower()

    try:
        if suffix == ".pdf":
            return _thumbnail_pdf(file_path, cached)
        elif suffix in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".tif"):
            return _thumbnail_image(file_path, cached)
    except Exception:
        return None

    return None


def _thumbnail_image(file_path: Path, dest: Path) -> Path:
    """Create a thumbnail from an image file."""
    with Image.open(file_path) as img:
        img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(dest, "PNG")
    return dest


def _thumbnail_pdf(file_path: Path, dest: Path) -> Path:
    """Create a thumbnail from the first page of a PDF."""
    try:
        from pdf2image import convert_from_path

        images = convert_from_path(str(file_path), first_page=1, last_page=1, dpi=150)
        if images:
            img = images[0]
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            img.save(dest, "PNG")
            return dest
    except Exception:
        pass

    # Fallback: return a placeholder-style image
    img = Image.new("RGB", THUMB_SIZE, color=(240, 240, 240))
    img.save(dest, "PNG")
    return dest
