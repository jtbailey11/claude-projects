"""Sorting report — generates a summary of the batch run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .processor import BatchReport


def generate_text_report(report: BatchReport) -> str:
    """Generate a human-readable text report of the batch run."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("  DOCUMENT SORTING REPORT")
    lines.append(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"  Total files processed:  {report.total_files}")
    lines.append(f"  Successfully sorted:    {report.sorted_count}")
    lines.append(f"  Sent to Unidentified:   {report.unidentified_count}")
    if report.left_in_place_count:
        lines.append(f"  Left in place:          {report.left_in_place_count}")
    lines.append(f"  Duplicates detected:    {report.duplicate_count}")
    lines.append(f"  Errors:                 {len(report.errors)}")
    lines.append(f"  Time elapsed:           {report.elapsed_seconds:.1f}s")
    lines.append("")

    if report.new_categories_created:
        lines.append("  NEW CATEGORIES CREATED:")
        for cat in report.new_categories_created:
            lines.append(f"    + {cat}")
        lines.append("")

    # Per-file details
    lines.append("-" * 60)
    lines.append("  FILE DETAILS")
    lines.append("-" * 60)

    for result in report.results:
        c = result.classification
        flag = ""
        if result.routed_to_unidentified:
            flag = " [UNIDENTIFIED]"
        if result.left_in_place:
            flag += " [LEFT IN PLACE]"
        if result.was_duplicate:
            flag += " [DUPLICATE]"

        lines.append(f"\n  {c.file_path.name}{flag}")
        lines.append(f"    Category:    {c.category}")
        lines.append(f"    Confidence:  {c.confidence:.0%}")
        lines.append(f"    Summary:     {c.summary}")
        if c.date_detected:
            lines.append(f"    Date found:  {c.date_detected}")
        lines.append(f"    Destination: {result.destination}")
        lines.append(f"    Reasoning:   {c.reasoning}")

    if report.errors:
        lines.append("")
        lines.append("-" * 60)
        lines.append("  ERRORS")
        lines.append("-" * 60)
        for path, err in report.errors:
            lines.append(f"\n  {path.name}")
            lines.append(f"    {err}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def generate_json_report(report: BatchReport) -> str:
    """Generate a machine-readable JSON report of the batch run."""
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_files": report.total_files,
            "sorted": report.sorted_count,
            "unidentified": report.unidentified_count,
            "left_in_place": report.left_in_place_count,
            "duplicates": report.duplicate_count,
            "errors": len(report.errors),
            "elapsed_seconds": round(report.elapsed_seconds, 2),
            "new_categories": report.new_categories_created,
        },
        "files": [
            {
                "source": str(r.classification.file_path),
                "category": r.classification.category,
                "confidence": r.classification.confidence,
                "summary": r.classification.summary,
                "date_detected": r.classification.date_detected,
                "suggested_filename": r.classification.suggested_filename,
                "destination": str(r.destination),
                "is_new_category": r.classification.is_new_category,
                "was_duplicate": r.was_duplicate,
                "routed_to_unidentified": r.routed_to_unidentified,
                "left_in_place": r.left_in_place,
                "reasoning": r.classification.reasoning,
            }
            for r in report.results
        ],
        "errors": [
            {"file": str(p), "error": e} for p, e in report.errors
        ],
    }
    return json.dumps(data, indent=2)


def save_report(report: BatchReport, output_dir: Path, fmt: str = "text") -> Path:
    """Write the report to a file in the output directory.

    Args:
        report: The batch report.
        output_dir: Where to save the report.
        fmt: 'text' or 'json'.

    Returns:
        Path to the saved report file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if fmt == "json":
        path = output_dir / f"sort_report_{timestamp}.json"
        path.write_text(generate_json_report(report))
    else:
        path = output_dir / f"sort_report_{timestamp}.txt"
        path.write_text(generate_text_report(report))

    return path
