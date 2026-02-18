"""Document classifier using Claude's vision API."""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import anthropic

from .config import GENERALITY_PROMPTS, SorterConfig


@dataclass
class Classification:
    """Result of classifying a single document."""

    file_path: Path
    category: str
    confidence: float  # 0.0 – 1.0
    summary: str  # one-line description of the document
    date_detected: str | None  # e.g. "2024-03-15" if found in the document
    suggested_filename: str | None  # a more descriptive filename
    is_new_category: bool  # True if the model proposed a category not in the existing list
    reasoning: str  # brief explanation of why this category was chosen


def _encode_image(path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type) for an image file."""
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        mime = "application/octet-stream"
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return data, mime


def _pdf_first_pages_as_images(path: Path, max_pages: int = 3) -> list[tuple[str, str]]:
    """Convert the first N pages of a PDF to base64-encoded PNG images.

    Returns a list of (base64_data, media_type) tuples.
    Falls back to sending the raw PDF bytes if pdf2image is unavailable.
    """
    try:
        from pdf2image import convert_from_path

        images = convert_from_path(str(path), first_page=1, last_page=max_pages, dpi=200)
        results: list[tuple[str, str]] = []
        for img in images:
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
            results.append((b64, "image/png"))
        return results
    except Exception:
        # Fallback: send the raw PDF as base64 (Claude can handle PDFs directly)
        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        return [(data, "application/pdf")]


def _build_system_prompt(config: SorterConfig, existing_categories: list[str]) -> str:
    """Build the system prompt for the classifier."""
    generality_guidance = GENERALITY_PROMPTS[config.generality]
    cats_list = "\n".join(f"  - {c}" for c in existing_categories) if existing_categories else "  (none yet)"

    return f"""\
You are a household document classifier. Your job is to look at a scanned document
and determine what category folder it belongs in.

GENERALITY LEVEL: {config.generality.value}
{generality_guidance}

EXISTING CATEGORY FOLDERS:
{cats_list}

RULES:
1. Prefer assigning the document to an existing category if it is a reasonable fit.
2. If no existing category fits well, you may suggest a NEW category name — but keep it
   consistent with the generality level and naming style of existing categories.
3. Provide a confidence score from 0.0 to 1.0 for your classification.
4. Extract any date visible on the document (statement date, invoice date, etc.).
5. Suggest a descriptive filename for the document (without extension).

Respond with ONLY a JSON object (no markdown fences) in this exact structure:
{{
  "category": "Category Name",
  "confidence": 0.85,
  "summary": "One-line description of the document",
  "date_detected": "YYYY-MM-DD or null",
  "suggested_filename": "Descriptive Name - Date",
  "is_new_category": false,
  "reasoning": "Brief explanation of classification"
}}"""


def classify_document(
    file_path: Path,
    config: SorterConfig,
    existing_categories: list[str],
    client: anthropic.Anthropic | None = None,
) -> Classification:
    """Classify a single document file using Claude's vision capabilities.

    Args:
        file_path: Path to the document (PDF or image).
        config: Sorter configuration.
        existing_categories: Currently existing category folder names.
        client: Optional pre-initialized Anthropic client.

    Returns:
        A Classification result.
    """
    if client is None:
        client = anthropic.Anthropic()

    # Build content blocks with the document image(s)
    content: list[dict] = []

    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        pages = _pdf_first_pages_as_images(file_path)
        for b64_data, media_type in pages:
            if media_type == "application/pdf":
                content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": media_type, "data": b64_data},
                })
            else:
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64_data},
                })
    else:
        b64_data, media_type = _encode_image(file_path)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64_data},
        })

    content.append({
        "type": "text",
        "text": f"Please classify this scanned household document. Filename: {file_path.name}",
    })

    system_prompt = _build_system_prompt(config, existing_categories)

    response = client.messages.create(
        model=config.model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )

    raw_text = response.content[0].text.strip()

    # Parse the JSON response, stripping markdown fences if present
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]
        if raw_text.endswith("```"):
            raw_text = raw_text[: raw_text.rfind("```")]
        raw_text = raw_text.strip()

    data = json.loads(raw_text)

    return Classification(
        file_path=file_path,
        category=data["category"],
        confidence=float(data["confidence"]),
        summary=data.get("summary", ""),
        date_detected=data.get("date_detected"),
        suggested_filename=data.get("suggested_filename"),
        is_new_category=data.get("is_new_category", False),
        reasoning=data.get("reasoning", ""),
    )
