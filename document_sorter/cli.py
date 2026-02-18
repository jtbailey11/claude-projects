"""Command-line interface for the document sorter."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .config import Generality, SorterConfig
from .processor import BatchReport, discover_files, process_batch
from .report import generate_text_report, save_report


console = Console()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="docsort",
        description="AI-powered household document sorter using Claude's vision API.",
    )
    p.add_argument(
        "inbox",
        nargs="?",
        default="unsorted",
        help="Path to the folder of unsorted documents (default: ./unsorted)",
    )
    p.add_argument(
        "-o", "--output",
        default="sorted",
        help="Path to the output folder for sorted documents (default: ./sorted)",
    )
    p.add_argument(
        "-t", "--threshold",
        type=float,
        default=0.70,
        help="Confidence threshold (0.0–1.0). Below this → Unidentified (default: 0.70)",
    )
    p.add_argument(
        "-g", "--generality",
        choices=["broad", "moderate", "specific"],
        default="moderate",
        help="Category granularity level (default: moderate)",
    )
    p.add_argument(
        "--move",
        action="store_true",
        help="Move files instead of copying them",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify documents but don't actually move/copy files",
    )
    p.add_argument(
        "--no-duplicates",
        action="store_true",
        help="Disable duplicate detection",
    )
    p.add_argument(
        "--max-new-categories",
        type=int,
        default=5,
        help="Max new category folders to create per batch (default: 5)",
    )
    p.add_argument(
        "--model",
        default="claude-sonnet-4-5-20250929",
        help="Claude model to use for classification",
    )
    p.add_argument(
        "--report",
        choices=["text", "json", "none"],
        default="text",
        help="Report format to save after sorting (default: text)",
    )
    p.add_argument(
        "--no-seed",
        action="store_true",
        help="Don't create default seed category folders",
    )
    return p


def _print_summary(report: BatchReport) -> None:
    """Print a rich summary table to the console."""
    table = Table(title="Sorting Summary", show_header=False, border_style="blue")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Total files", str(report.total_files))
    table.add_row("Sorted", f"[green]{report.sorted_count}[/green]")
    table.add_row("Unidentified", f"[yellow]{report.unidentified_count}[/yellow]")
    table.add_row("Duplicates", str(report.duplicate_count))
    table.add_row("Errors", f"[red]{len(report.errors)}[/red]" if report.errors else "0")
    table.add_row("Time", f"{report.elapsed_seconds:.1f}s")

    console.print()
    console.print(table)

    if report.new_categories_created:
        console.print(f"\n[bold]New categories created:[/bold]")
        for cat in report.new_categories_created:
            console.print(f"  [green]+[/green] {cat}")

    if report.errors:
        console.print(f"\n[bold red]Errors:[/bold red]")
        for path, err in report.errors:
            console.print(f"  {path.name}: {err}")


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = SorterConfig(
        inbox_dir=Path(args.inbox),
        output_dir=Path(args.output),
        confidence_threshold=args.threshold,
        generality=Generality(args.generality),
        move_files=args.move,
        dry_run=args.dry_run,
        detect_duplicates=not args.no_duplicates,
        max_new_categories=args.max_new_categories,
        model=args.model,
        seed_categories=[] if args.no_seed else SorterConfig.seed_categories,
    )

    # Validate inbox
    if not config.inbox_dir.exists():
        console.print(
            f"[red]Inbox folder not found:[/red] {config.inbox_dir.resolve()}\n"
            f"Create it and add your scanned documents, then run again."
        )
        return 1

    # Discover files first
    files = discover_files(config.inbox_dir, config)
    if not files:
        console.print(
            f"[yellow]No supported documents found in:[/yellow] {config.inbox_dir.resolve()}\n"
            f"Supported types: {', '.join(config.supported_extensions)}"
        )
        return 0

    # Header
    mode = "[bold yellow]DRY RUN[/bold yellow] " if config.dry_run else ""
    action = "Moving" if config.move_files else "Copying"
    console.print(Panel(
        f"{mode}[bold]Document Sorter[/bold]\n"
        f"Inbox:       {config.inbox_dir.resolve()}\n"
        f"Output:      {config.output_dir.resolve()}\n"
        f"Files found: {len(files)}\n"
        f"Threshold:   {config.confidence_threshold:.0%}\n"
        f"Generality:  {config.generality.value}\n"
        f"Action:      {action}",
        border_style="blue",
    ))

    # Process with progress bar
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Sorting documents...", total=len(files))

        def on_progress(idx: int, total: int, file_path: Path, status: str) -> None:
            progress.update(task, advance=1, description=f"[cyan]{file_path.name}[/cyan] {status}")

        report = process_batch(config, progress_callback=on_progress)

    # Print summary
    _print_summary(report)

    # Save report
    if args.report != "none":
        report_path = save_report(report, config.output_dir, fmt=args.report)
        console.print(f"\n[dim]Report saved to: {report_path}[/dim]")

    return 0 if not report.errors else 1


if __name__ == "__main__":
    sys.exit(main())
