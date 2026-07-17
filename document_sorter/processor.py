"""Batch processor — orchestrates scanning, classifying, and sorting documents."""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .classifier import Classification, classify_document, create_client
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
    left_in_place: bool = False  # refine mode: file stayed loose in its category folder


@dataclass
class BatchReport:
    """Summary of a complete batch run."""

    total_files: int = 0
    sorted_count: int = 0
    unidentified_count: int = 0
    duplicate_count: int = 0
    left_in_place_count: int = 0  # refine mode only
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
    client = create_client(config)

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


def discover_refine_work(config: SorterConfig) -> list[tuple[Path, list[Path]]]:
    """Find category folders eligible for pass-2 refinement.

    Returns (category_folder, loose_files) pairs for every top-level category
    folder (excluding Unidentified) that has at least config.refine_min_files
    supported files sitting directly in it. Files already inside subfolders are
    never touched, so refinement is idempotent and depth is capped at two levels.
    """
    if not config.output_dir.exists():
        return []
    work: list[tuple[Path, list[Path]]] = []
    for folder in sorted(config.output_dir.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        if folder.name == config.unidentified_dir_name:
            continue
        loose = discover_files(folder, config)
        if len(loose) >= config.refine_min_files:
            work.append((folder, loose))
    return work


def process_refine(
    config: SorterConfig,
    progress_callback: callable | None = None,
) -> BatchReport:
    """Pass 2: refine sorted category folders into subfolders.

    For each top-level category folder with enough loose files, classifies each
    file into a subfolder of that category. Safeguards:

    - Folders with fewer than config.refine_min_files loose files are skipped
      (small folders don't benefit from subfolders).
    - Low-confidence files are left in place, not sent to Unidentified — staying
      loose in the right category is a fine outcome.
    - If the classifier says a file doesn't belong in its category at all, it is
      re-classified against the top-level categories and moved out (or to
      Unidentified if the re-classification is also uncertain).
    - Files keep their pass-1 names; refinement never renames.

    Returns:
        A BatchReport summarising everything that happened.
    """
    # Refinement always moves files within the sorted tree — copying would
    # leave duplicates behind at the category root.
    config = replace(config, move_files=True)

    report = BatchReport()
    start_time = time.time()

    work = discover_refine_work(config)
    report.total_files = sum(len(files) for _, files in work)

    if not work:
        report.elapsed_seconds = time.time() - start_time
        return report

    top_level = [
        c for c in get_existing_categories(config.output_dir)
        if c != config.unidentified_dir_name
    ]

    client = create_client(config)
    idx = 0

    for folder, files in work:
        subfolders = get_existing_categories(folder)
        new_subfolder_count = 0

        for file_path in files:
            idx += 1
            status = ""
            try:
                classification = classify_document(
                    file_path, config, subfolders, client=client,
                    parent_category=folder.name,
                )

                if not classification.belongs_here:
                    # Escape hatch: pass 1 may have misfiled this document.
                    # Re-classify against the top-level categories before moving it.
                    reclassified = classify_document(
                        file_path, config, top_level, client=client,
                    )
                    confident = reclassified.confidence >= config.confidence_threshold

                    if confident and reclassified.category == folder.name:
                        # The two passes disagree — don't churn the file
                        report.results.append(SortResult(
                            classification=classification,
                            destination=file_path,
                            was_duplicate=False,
                            routed_to_unidentified=False,
                            left_in_place=True,
                        ))
                        report.left_in_place_count += 1
                        status = f"Contradictory signals — left in {folder.name}"
                    else:
                        if confident and reclassified.category in top_level:
                            dest_folder = ensure_category_folder(
                                config.output_dir, reclassified.category,
                            )
                            routed_to_unidentified = False
                            status = f"Misfiled: {folder.name} → {reclassified.category}"
                        else:
                            dest_folder = ensure_unidentified_folder(config)
                            routed_to_unidentified = True
                            status = f"Doesn't belong in {folder.name} → Unidentified"

                        dest_path, was_duplicate = place_file(
                            file_path, dest_folder, None, config,
                        )
                        report.results.append(SortResult(
                            classification=reclassified,
                            destination=dest_path,
                            was_duplicate=was_duplicate,
                            routed_to_unidentified=routed_to_unidentified,
                        ))
                        if routed_to_unidentified:
                            report.unidentified_count += 1
                        else:
                            report.sorted_count += 1
                        if was_duplicate:
                            report.duplicate_count += 1

                elif classification.confidence < config.confidence_threshold:
                    # Unsure which subfolder — the file stays loose in its category
                    report.results.append(SortResult(
                        classification=classification,
                        destination=file_path,
                        was_duplicate=False,
                        routed_to_unidentified=False,
                        left_in_place=True,
                    ))
                    report.left_in_place_count += 1
                    status = f"Low confidence ({classification.confidence:.0%}) — left in {folder.name}"

                else:
                    # Subfolders are a single level — strip any nesting the model added
                    subfolder = classification.category.replace("/", " - ").strip()
                    is_new = subfolder not in subfolders

                    if is_new and new_subfolder_count >= config.max_new_categories:
                        report.results.append(SortResult(
                            classification=classification,
                            destination=file_path,
                            was_duplicate=False,
                            routed_to_unidentified=False,
                            left_in_place=True,
                        ))
                        report.left_in_place_count += 1
                        status = f"New subfolder limit reached — left in {folder.name}"
                    else:
                        if not config.dry_run:
                            dest_folder = ensure_category_folder(folder, subfolder)
                        else:
                            dest_folder = folder / subfolder
                        if is_new:
                            subfolders.append(subfolder)
                            report.new_categories_created.append(f"{folder.name}/{subfolder}")
                            new_subfolder_count += 1

                        # No suggested filename: refinement keeps pass-1 names
                        dest_path, was_duplicate = place_file(
                            file_path, dest_folder, None, config,
                        )
                        report.results.append(SortResult(
                            classification=classification,
                            destination=dest_path,
                            was_duplicate=was_duplicate,
                            routed_to_unidentified=False,
                        ))
                        report.sorted_count += 1
                        if was_duplicate:
                            report.duplicate_count += 1
                        status = f"→ {folder.name}/{subfolder} ({classification.confidence:.0%})"

            except Exception as e:
                report.errors.append((file_path, str(e)))
                status = f"ERROR: {e}"

            if progress_callback:
                progress_callback(idx, report.total_files, file_path, status)

    report.elapsed_seconds = time.time() - start_time
    return report


def discover_drive_files(service: object, folder_id: str, config: SorterConfig) -> list[dict]:
    """List supported document files in a Drive folder."""
    from .drive import list_files_in_folder
    return list_files_in_folder(service, folder_id, config.supported_extensions)


def process_batch_drive(
    config: SorterConfig,
    progress_callback: callable | None = None,
) -> BatchReport:
    """Process all documents in a Google Drive inbox folder.

    Downloads each file to a temp dir for classification, then moves/copies
    the original Drive file into the correct category folder in Drive.

    Args:
        config: Sorter configuration (use_drive must be True).
        progress_callback: Optional callable(current_index, total, file_path, status_msg).

    Returns:
        A BatchReport summarising everything that happened.
    """
    from .drive import (
        authenticate,
        check_duplicate_by_hash,
        download_to_temp,
        ensure_folder_path,
        find_or_create_folder,
        get_file_md5,
        list_subfolders,
        move_file,
        rename_file,
        upload_file,
    )

    report = BatchReport()
    start_time = time.time()
    tmp_dirs: list[Path] = []

    try:
        # Authenticate
        service = authenticate(config.drive_credentials_path, config.drive_token_path)

        # Find or create inbox and output folders in Drive
        inbox_id = find_or_create_folder(service, config.drive_inbox_folder)
        output_id = find_or_create_folder(service, config.drive_output_folder)

        # Discover files in Drive inbox
        drive_files = discover_drive_files(service, inbox_id, config)
        report.total_files = len(drive_files)

        if not drive_files:
            report.elapsed_seconds = time.time() - start_time
            return report

        # Load existing category folders from Drive output
        existing_folders = list_subfolders(service, output_id)
        existing_categories = sorted(f["name"] for f in existing_folders)
        # Map category name → Drive folder ID
        cat_id_map: dict[str, str] = {f["name"]: f["id"] for f in existing_folders}

        # Seed default categories if output folder is empty
        if not existing_categories and config.seed_categories:
            for cat in config.seed_categories:
                if not config.dry_run:
                    cat_id = find_or_create_folder(service, cat, output_id)
                    cat_id_map[cat] = cat_id
            existing_categories = sorted(cat_id_map.keys())

        new_category_count = 0
        client = create_client(config)

        for idx, drive_file in enumerate(drive_files):
            file_name = drive_file["name"]
            file_id = drive_file["id"]
            status = ""

            try:
                # Download to temp for classification
                local_path = download_to_temp(service, file_id, file_name)
                tmp_dirs.append(local_path.parent)

                # Classify using the local copy
                classification = classify_document(
                    local_path, config, existing_categories, client=client,
                )

                routed_to_unidentified = False
                dest_category = classification.category

                if classification.confidence < config.confidence_threshold:
                    dest_category = config.unidentified_dir_name
                    routed_to_unidentified = True
                    status = f"Low confidence ({classification.confidence:.0%}) → Unidentified"
                elif classification.is_new_category:
                    if new_category_count >= config.max_new_categories:
                        dest_category = config.unidentified_dir_name
                        routed_to_unidentified = True
                        status = "New category limit reached → Unidentified"
                    else:
                        existing_categories.append(dest_category)
                        report.new_categories_created.append(dest_category)
                        new_category_count += 1
                        status = f"New category: {dest_category}"
                else:
                    status = f"→ {dest_category} ({classification.confidence:.0%})"

                # Resolve the destination folder in Drive
                if dest_category not in cat_id_map:
                    if not config.dry_run:
                        cat_id_map[dest_category] = ensure_folder_path(
                            service, dest_category, output_id,
                        )
                    else:
                        cat_id_map[dest_category] = "dry-run"

                dest_folder_id = cat_id_map[dest_category]

                # Check for duplicates via MD5
                was_duplicate = False
                if config.detect_duplicates and not config.dry_run:
                    src_md5 = get_file_md5(service, file_id)
                    if src_md5:
                        dup = check_duplicate_by_hash(service, dest_folder_id, src_md5)
                        if dup:
                            was_duplicate = True

                # Build the destination filename
                if classification.suggested_filename:
                    clean = "".join(
                        c for c in classification.suggested_filename
                        if c not in r'\/:*?"<>|'
                    ).strip()
                    ext = Path(file_name).suffix
                    new_name = f"{clean}{ext}" if clean else file_name
                else:
                    new_name = file_name

                if was_duplicate:
                    stem = Path(new_name).stem
                    ext = Path(new_name).suffix
                    new_name = f"{stem} (DUPLICATE){ext}"

                # Move or copy the file in Drive
                dest_display = Path(config.drive_output_folder) / dest_category / new_name

                if not config.dry_run:
                    if config.move_files:
                        move_file(service, file_id, dest_folder_id)
                        rename_file(service, file_id, new_name)
                    else:
                        upload_file(service, local_path, dest_folder_id, name=new_name)

                result = SortResult(
                    classification=classification,
                    destination=dest_display,
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
                report.errors.append((Path(file_name), str(e)))
                status = f"ERROR: {e}"

            if progress_callback:
                progress_callback(idx + 1, len(drive_files), Path(file_name), status)

    finally:
        # Clean up temp directories
        for tmp_dir in tmp_dirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    report.elapsed_seconds = time.time() - start_time
    return report


def process_refine_drive(
    config: SorterConfig,
    progress_callback: callable | None = None,
) -> BatchReport:
    """Pass 2 for Google Drive: refine sorted category folders into subfolders.

    Same safeguards as process_refine (minimum file count, leave-in-place on low
    confidence, misfile escape hatch, no renaming). Duplicate detection is
    skipped — files were already deduplicated on their way into the archive.

    Returns:
        A BatchReport summarising everything that happened.
    """
    from .drive import (
        authenticate,
        download_to_temp,
        find_or_create_folder,
        list_subfolders,
        move_file,
    )

    report = BatchReport()
    start_time = time.time()
    tmp_dirs: list[Path] = []

    try:
        service = authenticate(config.drive_credentials_path, config.drive_token_path)
        output_id = find_or_create_folder(service, config.drive_output_folder)

        category_folders = [
            f for f in list_subfolders(service, output_id)
            if f["name"] != config.unidentified_dir_name
        ]
        top_level = sorted(f["name"] for f in category_folders)
        top_id_map: dict[str, str] = {f["name"]: f["id"] for f in category_folders}

        # Build the worklist: only folders with enough loose files get refined
        work: list[tuple[str, str, list[dict], list[str], dict[str, str]]] = []
        for f in category_folders:
            loose = discover_drive_files(service, f["id"], config)
            if len(loose) >= config.refine_min_files:
                subs = list_subfolders(service, f["id"])
                work.append((
                    f["name"],
                    f["id"],
                    loose,
                    sorted(s["name"] for s in subs),
                    {s["name"]: s["id"] for s in subs},
                ))

        report.total_files = sum(len(w[2]) for w in work)
        if report.total_files == 0:
            report.elapsed_seconds = time.time() - start_time
            return report

        client = create_client(config)
        unidentified_id: str | None = None
        idx = 0

        for folder_name, folder_id, drive_files, subfolders, sub_id_map in work:
            new_subfolder_count = 0

            for drive_file in drive_files:
                idx += 1
                file_name = drive_file["name"]
                file_id = drive_file["id"]
                display_base = Path(config.drive_output_folder)
                status = ""

                try:
                    local_path = download_to_temp(service, file_id, file_name)
                    tmp_dirs.append(local_path.parent)

                    classification = classify_document(
                        local_path, config, subfolders, client=client,
                        parent_category=folder_name,
                    )

                    if not classification.belongs_here:
                        reclassified = classify_document(
                            local_path, config, top_level, client=client,
                        )
                        confident = reclassified.confidence >= config.confidence_threshold

                        if confident and reclassified.category == folder_name:
                            # The two passes disagree — don't churn the file
                            report.results.append(SortResult(
                                classification=classification,
                                destination=display_base / folder_name / file_name,
                                was_duplicate=False,
                                routed_to_unidentified=False,
                                left_in_place=True,
                            ))
                            report.left_in_place_count += 1
                            status = f"Contradictory signals — left in {folder_name}"
                        else:
                            if confident and reclassified.category in top_id_map:
                                dest_id = top_id_map[reclassified.category]
                                dest_display = display_base / reclassified.category / file_name
                                routed_to_unidentified = False
                                status = f"Misfiled: {folder_name} → {reclassified.category}"
                            else:
                                if unidentified_id is None and not config.dry_run:
                                    unidentified_id = find_or_create_folder(
                                        service, config.unidentified_dir_name, output_id,
                                    )
                                dest_id = unidentified_id or "dry-run"
                                dest_display = display_base / config.unidentified_dir_name / file_name
                                routed_to_unidentified = True
                                status = f"Doesn't belong in {folder_name} → Unidentified"

                            if not config.dry_run:
                                move_file(service, file_id, dest_id)

                            report.results.append(SortResult(
                                classification=reclassified,
                                destination=dest_display,
                                was_duplicate=False,
                                routed_to_unidentified=routed_to_unidentified,
                            ))
                            if routed_to_unidentified:
                                report.unidentified_count += 1
                            else:
                                report.sorted_count += 1

                    elif classification.confidence < config.confidence_threshold:
                        report.results.append(SortResult(
                            classification=classification,
                            destination=display_base / folder_name / file_name,
                            was_duplicate=False,
                            routed_to_unidentified=False,
                            left_in_place=True,
                        ))
                        report.left_in_place_count += 1
                        status = f"Low confidence ({classification.confidence:.0%}) — left in {folder_name}"

                    else:
                        subfolder = classification.category.replace("/", " - ").strip()
                        is_new = subfolder not in sub_id_map

                        if is_new and new_subfolder_count >= config.max_new_categories:
                            report.results.append(SortResult(
                                classification=classification,
                                destination=display_base / folder_name / file_name,
                                was_duplicate=False,
                                routed_to_unidentified=False,
                                left_in_place=True,
                            ))
                            report.left_in_place_count += 1
                            status = f"New subfolder limit reached — left in {folder_name}"
                        else:
                            if is_new:
                                if not config.dry_run:
                                    sub_id_map[subfolder] = find_or_create_folder(
                                        service, subfolder, folder_id,
                                    )
                                else:
                                    sub_id_map[subfolder] = "dry-run"
                                subfolders.append(subfolder)
                                report.new_categories_created.append(f"{folder_name}/{subfolder}")
                                new_subfolder_count += 1

                            if not config.dry_run:
                                move_file(service, file_id, sub_id_map[subfolder])

                            report.results.append(SortResult(
                                classification=classification,
                                destination=display_base / folder_name / subfolder / file_name,
                                was_duplicate=False,
                                routed_to_unidentified=False,
                            ))
                            report.sorted_count += 1
                            status = f"→ {folder_name}/{subfolder} ({classification.confidence:.0%})"

                except Exception as e:
                    report.errors.append((Path(file_name), str(e)))
                    status = f"ERROR: {e}"

                if progress_callback:
                    progress_callback(idx, report.total_files, Path(file_name), status)

    finally:
        for tmp_dir in tmp_dirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    report.elapsed_seconds = time.time() - start_time
    return report
