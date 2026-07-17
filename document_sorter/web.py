"""Flask web GUI for the document sorter."""

from __future__ import annotations

import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from .classifier import classify_document, create_client
from .config import Generality, Provider, SorterConfig
from .folders import (
    ensure_category_folder,
    get_existing_categories,
    place_file,
)
from .processor import BatchReport, discover_files, process_batch, process_refine
from .thumbnails import generate_thumbnail

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config: SorterConfig | None = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    app.secret_key = "docsort-local-only"

    if config is None:
        config = SorterConfig()

    # Store config and state on the app
    app.config["SORTER"] = config
    app.config["THUMB_CACHE"] = Path(".docsort_thumbs")
    # In-progress sort job state
    app.config["JOB"] = {
        "running": False,
        "progress": 0,
        "total": 0,
        "current_file": "",
        "results": [],
        "errors": [],
        "done": False,
    }

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.route("/")
    def index():
        """Main dashboard."""
        cfg: SorterConfig = app.config["SORTER"]
        files = discover_files(cfg.inbox_dir, cfg)
        categories = get_existing_categories(cfg.output_dir)

        # Seed categories if needed
        if not categories and cfg.seed_categories:
            cfg.output_dir.mkdir(parents=True, exist_ok=True)
            for cat in cfg.seed_categories:
                ensure_category_folder(cfg.output_dir, cat)
            categories = get_existing_categories(cfg.output_dir)

        return render_template(
            "index.html",
            files=[f.name for f in files],
            file_count=len(files),
            categories=categories,
            config=cfg,
            job=app.config["JOB"],
        )

    @app.route("/thumbnail/<path:filename>")
    def thumbnail(filename: str):
        """Serve a thumbnail for a document in the inbox."""
        cfg: SorterConfig = app.config["SORTER"]
        file_path = cfg.inbox_dir / filename
        if not file_path.exists():
            return "Not found", 404

        thumb = generate_thumbnail(file_path, app.config["THUMB_CACHE"])
        if thumb and thumb.exists():
            return send_file(thumb, mimetype="image/png")
        return "No thumbnail", 404

    @app.route("/api/files")
    def api_files():
        """List inbox files as JSON."""
        cfg: SorterConfig = app.config["SORTER"]
        files = discover_files(cfg.inbox_dir, cfg)
        return jsonify([{"name": f.name, "size": f.stat().st_size} for f in files])

    @app.route("/api/categories")
    def api_categories():
        """List category folders as JSON."""
        cfg: SorterConfig = app.config["SORTER"]
        return jsonify(get_existing_categories(cfg.output_dir))

    @app.route("/api/categories", methods=["POST"])
    def api_create_category():
        """Create a new category folder."""
        cfg: SorterConfig = app.config["SORTER"]
        data = request.get_json()
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        ensure_category_folder(cfg.output_dir, name)
        return jsonify({"created": name})

    @app.route("/api/sort", methods=["POST"])
    def api_start_sort():
        """Start a batch sort job in a background thread."""
        job = app.config["JOB"]
        if job["running"]:
            return jsonify({"error": "Sort already in progress"}), 409

        cfg: SorterConfig = app.config["SORTER"]
        data = request.get_json() or {}

        # Allow overriding settings per-run
        threshold = float(data.get("threshold", cfg.confidence_threshold))
        generality = data.get("generality", cfg.generality.value)
        move = data.get("move", cfg.move_files)

        run_config = SorterConfig(
            inbox_dir=cfg.inbox_dir,
            output_dir=cfg.output_dir,
            confidence_threshold=threshold,
            generality=Generality(generality),
            provider=cfg.provider,
            model=cfg.model,
            ollama_host=cfg.ollama_host,
            move_files=move,
            dry_run=False,
            detect_duplicates=cfg.detect_duplicates,
            max_new_categories=cfg.max_new_categories,
            seed_categories=cfg.seed_categories,
        )

        # Reset job state
        job.update({
            "running": True,
            "progress": 0,
            "total": 0,
            "current_file": "",
            "results": [],
            "errors": [],
            "done": False,
        })

        thread = threading.Thread(
            target=_run_sort_job, args=(app, run_config), daemon=True,
        )
        thread.start()
        return jsonify({"started": True})

    @app.route("/api/sort/status")
    def api_sort_status():
        """Poll the current sort job status."""
        job = app.config["JOB"]
        return jsonify(job)

    @app.route("/api/refine", methods=["POST"])
    def api_start_refine():
        """Start a pass-2 refine job (sort within category folders) in the background."""
        job = app.config["JOB"]
        if job["running"]:
            return jsonify({"error": "A job is already in progress"}), 409

        cfg: SorterConfig = app.config["SORTER"]
        data = request.get_json() or {}
        threshold = float(data.get("threshold", cfg.confidence_threshold))

        run_config = SorterConfig(
            inbox_dir=cfg.inbox_dir,
            output_dir=cfg.output_dir,
            confidence_threshold=threshold,
            generality=cfg.generality,
            provider=cfg.provider,
            model=cfg.model,
            ollama_host=cfg.ollama_host,
            dry_run=False,
            detect_duplicates=cfg.detect_duplicates,
            max_new_categories=cfg.max_new_categories,
            refine_min_files=cfg.refine_min_files,
            seed_categories=cfg.seed_categories,
        )

        job.update({
            "running": True,
            "progress": 0,
            "total": 0,
            "current_file": "",
            "results": [],
            "errors": [],
            "done": False,
        })

        thread = threading.Thread(
            target=_run_refine_job, args=(app, run_config), daemon=True,
        )
        thread.start()
        return jsonify({"started": True})

    @app.route("/api/classify/<path:filename>", methods=["POST"])
    def api_classify_single(filename: str):
        """Classify a single file without moving it (preview)."""
        cfg: SorterConfig = app.config["SORTER"]
        file_path = cfg.inbox_dir / filename
        if not file_path.exists():
            return jsonify({"error": "File not found"}), 404

        categories = get_existing_categories(cfg.output_dir)

        client = create_client(cfg)
        classification = classify_document(file_path, cfg, categories, client=client)

        return jsonify({
            "category": classification.category,
            "confidence": classification.confidence,
            "summary": classification.summary,
            "date_detected": classification.date_detected,
            "suggested_filename": classification.suggested_filename,
            "is_new_category": classification.is_new_category,
            "reasoning": classification.reasoning,
        })

    @app.route("/api/move", methods=["POST"])
    def api_move_file():
        """Manually move a file to a specific category."""
        cfg: SorterConfig = app.config["SORTER"]
        data = request.get_json()
        filename = data.get("filename", "")
        category = data.get("category", "")

        if not filename or not category:
            return jsonify({"error": "filename and category required"}), 400

        file_path = cfg.inbox_dir / filename
        if not file_path.exists():
            return jsonify({"error": "File not found"}), 404

        dest_folder = ensure_category_folder(cfg.output_dir, category)
        dest_path, was_dup = place_file(file_path, dest_folder, None, cfg)

        return jsonify({
            "moved": True,
            "destination": str(dest_path),
            "was_duplicate": was_dup,
        })

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        """Get or update sorter settings."""
        cfg: SorterConfig = app.config["SORTER"]
        if request.method == "GET":
            return jsonify({
                "inbox_dir": str(cfg.inbox_dir),
                "output_dir": str(cfg.output_dir),
                "confidence_threshold": cfg.confidence_threshold,
                "generality": cfg.generality.value,
                "provider": cfg.provider.value,
                "move_files": cfg.move_files,
                "max_new_categories": cfg.max_new_categories,
                "model": cfg.model,
            })

        data = request.get_json()
        if "confidence_threshold" in data:
            cfg.confidence_threshold = float(data["confidence_threshold"])
        if "generality" in data:
            cfg.generality = Generality(data["generality"])
        if "provider" in data:
            cfg.provider = Provider(data["provider"])
        if "move_files" in data:
            cfg.move_files = bool(data["move_files"])
        if "max_new_categories" in data:
            cfg.max_new_categories = int(data["max_new_categories"])
        return jsonify({"updated": True})

    return app


def _report_to_job(job: dict, report: BatchReport) -> None:
    """Copy a BatchReport's outcome into the polling job dict."""
    job["results"] = [
        {
            "filename": r.classification.file_path.name,
            "category": r.classification.category,
            "confidence": r.classification.confidence,
            "summary": r.classification.summary,
            "destination": str(r.destination),
            "unidentified": r.routed_to_unidentified,
            "duplicate": r.was_duplicate,
            "left_in_place": r.left_in_place,
            "reasoning": r.classification.reasoning,
        }
        for r in report.results
    ]
    job["errors"] = [
        {"filename": p.name, "error": e} for p, e in report.errors
    ]


def _run_batch_job(app: Flask, config: SorterConfig, batch_fn) -> None:
    """Run a batch function (sort or refine) in the background, updating the job dict."""
    job = app.config["JOB"]

    def on_progress(idx: int, total: int, file_path: Path, status: str) -> None:
        job["total"] = total
        job["progress"] = idx
        job["current_file"] = file_path.name

    try:
        report = batch_fn(config, progress_callback=on_progress)
        _report_to_job(job, report)
    except Exception as e:
        job["errors"].append({"filename": "", "error": str(e)})

    job["progress"] = job["total"]
    job["current_file"] = ""
    job["running"] = False
    job["done"] = True


def _run_sort_job(app: Flask, config: SorterConfig) -> None:
    """Background pass-1 sort job (inbox → category folders)."""
    _run_batch_job(app, config, process_batch)


def _run_refine_job(app: Flask, config: SorterConfig) -> None:
    """Background pass-2 refine job (category folders → subfolders)."""
    _run_batch_job(app, config, process_refine)


def run_server(config: SorterConfig, host: str = "127.0.0.1", port: int = 5000) -> None:
    """Start the web GUI server."""
    app = create_app(config)
    print(f"Document Sorter GUI running at http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
