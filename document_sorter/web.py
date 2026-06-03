"""Flask web GUI for the document sorter."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from .classifier import classify_document, create_client
from .config import Generality, Provider, SorterConfig
from .folders import (
    ensure_category_folder,
    ensure_unidentified_folder,
    get_existing_categories,
    place_file,
)
from .processor import discover_files
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


def _run_sort_job(app: Flask, config: SorterConfig) -> None:
    """Background sort job that updates app.config['JOB'] as it runs."""
    from .classifier import classify_document, create_client
    from .folders import ensure_category_folder, ensure_unidentified_folder, get_existing_categories, place_file

    job = app.config["JOB"]
    files = discover_files(config.inbox_dir, config)
    job["total"] = len(files)

    if not files:
        job["running"] = False
        job["done"] = True
        return

    config.output_dir.mkdir(parents=True, exist_ok=True)
    existing_categories = get_existing_categories(config.output_dir)

    if not existing_categories and config.seed_categories:
        for cat in config.seed_categories:
            ensure_category_folder(config.output_dir, cat)
        existing_categories = get_existing_categories(config.output_dir)

    new_category_count = 0
    client = create_client(config)

    for idx, file_path in enumerate(files):
        job["current_file"] = file_path.name
        job["progress"] = idx

        try:
            classification = classify_document(
                file_path, config, existing_categories, client=client,
            )

            routed_to_unidentified = False

            if classification.confidence < config.confidence_threshold:
                dest_folder = ensure_unidentified_folder(config)
                routed_to_unidentified = True
            elif classification.is_new_category:
                if new_category_count >= config.max_new_categories:
                    dest_folder = ensure_unidentified_folder(config)
                    routed_to_unidentified = True
                else:
                    dest_folder = ensure_category_folder(config.output_dir, classification.category)
                    existing_categories.append(classification.category)
                    new_category_count += 1
            else:
                dest_folder = ensure_category_folder(config.output_dir, classification.category)

            dest_path, was_dup = place_file(
                file_path, dest_folder, classification.suggested_filename, config,
            )

            job["results"].append({
                "filename": file_path.name,
                "category": classification.category,
                "confidence": classification.confidence,
                "summary": classification.summary,
                "destination": str(dest_path),
                "unidentified": routed_to_unidentified,
                "duplicate": was_dup,
                "reasoning": classification.reasoning,
            })

        except Exception as e:
            job["errors"].append({"filename": file_path.name, "error": str(e)})

    job["progress"] = len(files)
    job["current_file"] = ""
    job["running"] = False
    job["done"] = True


def run_server(config: SorterConfig, host: str = "127.0.0.1", port: int = 5000) -> None:
    """Start the web GUI server."""
    app = create_app(config)
    print(f"Document Sorter GUI running at http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
