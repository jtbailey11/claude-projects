"""Batch processor — orchestrates scanning, classifying, and sorting documents."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from .classifier import Classification, classify_document
from .config import SorterConfig
from .folders import (
    ensure_category_folder,
    ensure_unidentified_folder,
    get_existing_categories,
    place_file,
)


@dataclass
class SortResult:
    """Outcome of sorting a single file."""

    classification: Classification
    destination: Path
    was_duplicate: bool
    routed_to_unidentified: bool


@dataclass
class BatchReport:
    """Summary of a complete batch run."""

    total_files: int = 0
    sorted_count: int = 0
    unidentified_count: int = 0
    duplicate_count: int = 0
    new_categories_created: list[str] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    results: list[SortResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.total_files == 0:
            return 0.0
        return (self.sorted_count + self.unidentified_count) / self.total_files


def discover_files(inbox: Path, config: SorterConfig) -> list[Path]:
    """Find all supported document files in the inbox directory (non-recursive)."""
    if not inbox.exists():
        return []
    files = [
        f for f in sorted(inbox.iterdir())
        if f.is_file() and f.suffix.lower() in config.supported_extensions
    ]
    return files


def process_batch(
    config: SorterConfig,
    progress_callback: callable | None = None,
) -> BatchReport:
    """Process all documents in the inbox folder.

    Args:
        config: Sorter configuration.
        progress_callback: Optional callable(current_index, total, file_path, status_msg)
            called after each file is processed (useful for progress bars).

    Returns:
        A BatchReport summarising everything that happened.
    """
    report = BatchReport()
    start_time = time.time()

    # Discover files
    files = discover_files(config.inbox_dir, config)
    report.total_files = len(files)

    if not files:
        report.elapsed_seconds = time.time() - start_time
        return report

    # Ensure output directory exists
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # Load existing categories
    existing_categories = get_existing_categories(config.output_dir)

    # Seed default categories if output dir is empty
    if not existing_categories and config.seed_categories:
        for cat in config.seed_categories:
            if not config.dry_run:
                ensure_category_folder(config.output_dir, cat)
        existing_categories = get_existing_categories(config.output_dir)

    # Track how many new categories we've created this batch
    new_category_count = 0

    # Initialise API client once for the whole batch
    client = anthropic.Anthropic()

    for idx, file_path in enumerate(files):
        status = ""
        try:
            # Classify
            classification = classify_document(
                file_path, config, existing_categories, client=client,
            )

            # Decide destination
            routed_to_unidentified = False

            if classification.confidence < config.confidence_threshold:
                # Below threshold → unidentified
                dest_folder = ensure_unidentified_folder(config)
                routed_to_unidentified = True
                status = f"Low confidence ({classification.confidence:.0%}) → Unidentified"

            elif classification.is_new_category:
                if new_category_count >= config.max_new_categories:
                    # Too many new categories this batch — play it safe
                    dest_folder = ensure_unidentified_folder(config)
                    routed_to_unidentified = True
                    status = f"New category limit reached → Unidentified"
                else:
                    if not config.dry_run:
                        dest_folder = ensure_category_folder(
                            config.output_dir, classification.category,
                        )
                    else:
                        dest_folder = config.output_dir / classification.category
                    existing_categories.append(classification.category)
                    report.new_categories_created.append(classification.category)
                    new_category_count += 1
                    status = f"New category: {classification.category}"
            else:
                dest_folder = ensure_category_folder(
                    config.output_dir, classification.category,
                )
                status = f"→ {classification.category} ({classification.confidence:.0%})"

            # Place file
            dest_path, was_duplicate = place_file(
                file_path, dest_folder, classification.suggested_filename, config,
            )

            result = SortResult(
                classification=classification,
                destination=dest_path,
                was_duplicate=was_duplicate,
                routed_to_unidentified=routed_to_unidentified,
            )
            report.results.append(result)

            if routed_to_unidentified:
                report.unidentified_count += 1
            else:
                report.sorted_count += 1

            if was_duplicate:
                report.duplicate_count += 1

        except Exception as e:
            report.errors.append((file_path, str(e)))
            status = f"ERROR: {e}"

        if progress_callback:
            progress_callback(idx + 1, len(files), file_path, status)

    report.elapsed_seconds = time.time() - start_time
    return report
