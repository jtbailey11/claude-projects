"""Command-line interface for the document sorter."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .config import Generality, Provider, SorterConfig
from .processor import BatchReport, discover_files, process_batch
from .report import save_report


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
        "--refine",
        action="store_true",
        help="Pass 2: sort files WITHIN existing category folders into subfolders "
             "(operates on the output folder; low-confidence files stay in place)",
    )
    p.add_argument(
        "--refine-min-files",
        type=int,
        default=8,
        help="Only refine category folders with at least this many loose files (default: 8)",
    )
    p.add_argument(
        "-p", "--provider",
        choices=["ollama", "gemini", "claude"],
        default="ollama",
        help="AI provider: ollama (local, free), gemini (cloud, free tier), claude (cloud, paid). Default: ollama",
    )
    p.add_argument(
        "--model",
        default="",
        help="Model name override (default: auto per provider — gemma4:e4b / gemini-2.0-flash / claude-sonnet-4-5)",
    )
    p.add_argument(
        "--ollama-host",
        default="http://localhost:11434",
        help="Ollama server URL (default: http://localhost:11434)",
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

    # Google Drive options
    drive_group = p.add_argument_group("Google Drive")
    drive_group.add_argument(
        "--drive",
        action="store_true",
        help="Use Google Drive instead of local folders",
    )
    drive_group.add_argument(
        "--drive-inbox",
        default="Unsorted Scans",
        help="Name of the inbox folder in Google Drive (default: 'Unsorted Scans')",
    )
    drive_group.add_argument(
        "--drive-output",
        default="Sorted Documents",
        help="Name of the output folder in Google Drive (default: 'Sorted Documents')",
    )
    drive_group.add_argument(
        "--drive-credentials",
        default="credentials.json",
        help="Path to Google OAuth credentials JSON (default: credentials.json)",
    )
    drive_group.add_argument(
        "--drive-token",
        default="token.json",
        help="Path to saved Google OAuth token (default: token.json)",
    )

    # GUI options
    gui_group = p.add_argument_group("Web GUI")
    gui_group.add_argument(
        "--gui",
        action="store_true",
        help="Launch the web GUI instead of sorting from the command line",
    )
    gui_group.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port for the web GUI (default: 5000)",
    )
    gui_group.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for the web GUI (default: 127.0.0.1)",
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
    if report.left_in_place_count:
        table.add_row("Left in place", str(report.left_in_place_count))
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


def _run_local(args: argparse.Namespace, config: SorterConfig) -> int:
    """Run the sorter against local folders."""
    # Validate inbox
    if not config.inbox_dir.exists():
        console.print(
            f"[red]Inbox folder not found:[/red] {config.inbox_dir.resolve()}\n"
            f"Create it and add your scanned documents, then run again."
        )
        return 1

    files = discover_files(config.inbox_dir, config)
    if not files:
        console.print(
            f"[yellow]No supported documents found in:[/yellow] {config.inbox_dir.resolve()}\n"
            f"Supported types: {', '.join(config.supported_extensions)}"
        )
        return 0

    mode = "[bold yellow]DRY RUN[/bold yellow] " if config.dry_run else ""
    action = "Moving" if config.move_files else "Copying"
    console.print(Panel(
        f"{mode}[bold]Document Sorter[/bold]\n"
        f"Inbox:       {config.inbox_dir.resolve()}\n"
        f"Output:      {config.output_dir.resolve()}\n"
        f"Files found: {len(files)}\n"
        f"Provider:    {config.provider.value} ({config.resolved_model})\n"
        f"Threshold:   {config.confidence_threshold:.0%}\n"
        f"Generality:  {config.generality.value}\n"
        f"Action:      {action}",
        border_style="blue",
    ))

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

    _print_summary(report)

    if args.report != "none":
        report_path = save_report(report, config.output_dir, fmt=args.report)
        console.print(f"\n[dim]Report saved to: {report_path}[/dim]")

    return 0 if not report.errors else 1


def _run_drive(args: argparse.Namespace, config: SorterConfig) -> int:
    """Run the sorter against Google Drive."""
    try:
        from .drive import check_drive_available
        check_drive_available()
    except ImportError as e:
        console.print(f"[red]{e}[/red]")
        return 1

    from .processor import process_batch_drive

    mode = "[bold yellow]DRY RUN[/bold yellow] " if config.dry_run else ""
    action = "Moving" if config.move_files else "Copying"
    console.print(Panel(
        f"{mode}[bold]Document Sorter[/bold] [blue](Google Drive)[/blue]\n"
        f"Drive inbox:  {config.drive_inbox_folder}\n"
        f"Drive output: {config.drive_output_folder}\n"
        f"Provider:     {config.provider.value} ({config.resolved_model})\n"
        f"Threshold:    {config.confidence_threshold:.0%}\n"
        f"Generality:   {config.generality.value}\n"
        f"Action:       {action}",
        border_style="blue",
    ))

    console.print("[dim]Authenticating with Google Drive...[/dim]")

    # We don't know file count until we query Drive, so start indeterminate
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Connecting to Drive...", total=None)
        started = False

        def on_progress(idx: int, total: int, file_path: Path, status: str) -> None:
            nonlocal started
            if not started:
                progress.update(task, total=total)
                started = True
            progress.update(task, advance=1, description=f"[cyan]{file_path.name}[/cyan] {status}")

        report = process_batch_drive(config, progress_callback=on_progress)

    if report.total_files == 0:
        console.print(
            f"[yellow]No supported documents found in Drive folder:[/yellow] "
            f"{config.drive_inbox_folder}\n"
            f"Supported types: {', '.join(config.supported_extensions)}"
        )
        return 0

    _print_summary(report)

    # Save report locally (Drive reports always go local)
    if args.report != "none":
        report_dir = Path(".")
        report_path = save_report(report, report_dir, fmt=args.report)
        console.print(f"\n[dim]Report saved to: {report_path}[/dim]")

    return 0 if not report.errors else 1


def _run_refine(args: argparse.Namespace, config: SorterConfig) -> int:
    """Run pass-2 refinement: sort within category folders into subfolders."""
    location = (
        f"Drive folder: {config.drive_output_folder}" if config.use_drive
        else f"Sorted tree:  {config.output_dir.resolve()}"
    )
    mode = "[bold yellow]DRY RUN[/bold yellow] " if config.dry_run else ""
    console.print(Panel(
        f"{mode}[bold]Document Sorter — Refine (Pass 2)[/bold]\n"
        f"{location}\n"
        f"Provider:    {config.provider.value} ({config.resolved_model})\n"
        f"Threshold:   {config.confidence_threshold:.0%} (below → file stays in place)\n"
        f"Min files:   {config.refine_min_files} per folder to trigger refinement",
        border_style="blue",
    ))

    if config.use_drive:
        try:
            from .drive import check_drive_available
            check_drive_available()
        except ImportError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        from .processor import process_refine_drive as refine_fn
        console.print("[dim]Authenticating with Google Drive...[/dim]")
    else:
        if not config.output_dir.exists():
            console.print(
                f"[red]Sorted folder not found:[/red] {config.output_dir.resolve()}\n"
                f"Run a normal sort first, then refine."
            )
            return 1
        from .processor import process_refine as refine_fn

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning category folders...", total=None)
        started = False

        def on_progress(idx: int, total: int, file_path: Path, status: str) -> None:
            nonlocal started
            if not started:
                progress.update(task, total=total)
                started = True
            progress.update(task, advance=1, description=f"[cyan]{file_path.name}[/cyan] {status}")

        report = refine_fn(config, progress_callback=on_progress)

    if report.total_files == 0:
        console.print(
            f"[yellow]Nothing to refine.[/yellow] No category folder has "
            f"{config.refine_min_files}+ loose files (adjust with --refine-min-files)."
        )
        return 0

    _print_summary(report)

    if args.report != "none":
        report_dir = Path(".") if config.use_drive else config.output_dir
        report_path = save_report(report, report_dir, fmt=args.report)
        console.print(f"\n[dim]Report saved to: {report_path}[/dim]")

    return 0 if not report.errors else 1


def _run_gui(args: argparse.Namespace, config: SorterConfig) -> int:
    """Launch the web GUI."""
    try:
        from .web import run_server
    except ImportError:
        console.print(
            "[red]Flask is required for the web GUI.[/red]\n"
            "Install it with: pip install -e '.[gui]'"
        )
        return 1

    # Ensure inbox exists for the GUI
    config.inbox_dir.mkdir(parents=True, exist_ok=True)

    console.print(Panel(
        f"[bold]Document Sorter — Web GUI[/bold]\n"
        f"Inbox:  {config.inbox_dir.resolve()}\n"
        f"Output: {config.output_dir.resolve()}\n"
        f"URL:    http://{args.host}:{args.port}",
        border_style="blue",
    ))
    run_server(config, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = SorterConfig(
        inbox_dir=Path(args.inbox),
        output_dir=Path(args.output),
        confidence_threshold=args.threshold,
        generality=Generality(args.generality),
        provider=Provider(args.provider),
        model=args.model,
        ollama_host=args.ollama_host,
        move_files=args.move,
        dry_run=args.dry_run,
        detect_duplicates=not args.no_duplicates,
        max_new_categories=args.max_new_categories,
        refine_min_files=args.refine_min_files,
        seed_categories=[] if args.no_seed else SorterConfig.seed_categories,
        use_drive=args.drive,
        drive_inbox_folder=args.drive_inbox,
        drive_output_folder=args.drive_output,
        drive_credentials_path=Path(args.drive_credentials),
        drive_token_path=Path(args.drive_token),
    )

    if args.gui:
        return _run_gui(args, config)
    elif args.refine:
        return _run_refine(args, config)
    elif config.use_drive:
        return _run_drive(args, config)
    else:
        return _run_local(args, config)


if __name__ == "__main__":
    sys.exit(main())
