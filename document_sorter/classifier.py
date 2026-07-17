"""Document classifier — supports Claude, Gemini, and Ollama (Gemma) providers."""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from .config import GENERALITY_PROMPTS, Provider, SorterConfig


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
    belongs_here: bool = True  # refine mode only: False = doesn't belong in the parent category


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _encode_image(path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type) for an image file."""
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        mime = "application/octet-stream"
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return data, mime


def _pdf_first_pages_as_images(path: Path, max_pages: int = 3) -> list[tuple[str, str]]:
    """Convert the first N pages of a PDF to base64-encoded PNG images."""
    try:
        from pdf2image import convert_from_path
        import io

        images = convert_from_path(str(path), first_page=1, last_page=max_pages, dpi=200)
        results: list[tuple[str, str]] = []
        for img in images:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
            results.append((b64, "image/png"))
        return results
    except Exception:
        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        return [(data, "application/pdf")]


def _build_system_prompt(
    config: SorterConfig,
    existing_categories: list[str],
    parent_category: str | None = None,
) -> str:
    """Build the system prompt for the classifier.

    When parent_category is given, builds the pass-2 refinement prompt instead:
    existing_categories is then the list of subfolders within that parent.
    """
    if parent_category is not None:
        return _build_refine_prompt(config, parent_category, existing_categories)

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


def _build_refine_prompt(
    config: SorterConfig,
    parent_category: str,
    existing_subfolders: list[str],
) -> str:
    """Build the pass-2 system prompt: choose a subfolder within an existing category."""
    subs_list = (
        "\n".join(f"  - {s}" for s in existing_subfolders)
        if existing_subfolders else "  (none yet)"
    )

    return f"""\
You are refining an already-sorted household document archive. This document has
already been filed into the top-level category "{parent_category}". Your job is to
choose ONE subfolder within "{parent_category}" for it.

EXISTING SUBFOLDERS OF "{parent_category}":
{subs_list}

RULES:
1. STRONGLY prefer an existing subfolder when one is a reasonable fit. Only propose
   a new subfolder if nothing existing fits AND this document type will plausibly
   recur (statements, bills, policies, records of the same kind).
2. Subfolder names must be a single level — never include "/" in the name. Keep the
   naming style consistent with the existing subfolders.
3. If this document does NOT actually belong in "{parent_category}" at all, set
   "belongs_here" to false and set "category" to your best guess of the correct
   top-level category instead.
4. Provide a confidence score from 0.0 to 1.0. If you are unsure which subfolder
   fits, use a low confidence — the file will simply stay loose in
   "{parent_category}", which is a perfectly fine outcome. Do not force a choice.

Respond with ONLY a JSON object (no markdown fences) in this exact structure:
{{
  "category": "Subfolder Name",
  "confidence": 0.85,
  "summary": "One-line description of the document",
  "date_detected": "YYYY-MM-DD or null",
  "suggested_filename": null,
  "is_new_category": false,
  "belongs_here": true,
  "reasoning": "Brief explanation of classification"
}}"""


def _parse_response(raw_text: str) -> dict:
    """Parse JSON from a model response, stripping markdown fences if present."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()
    return json.loads(text)


def _to_classification(file_path: Path, data: dict) -> Classification:
    """Convert parsed JSON dict to a Classification dataclass."""
    return Classification(
        file_path=file_path,
        category=data["category"],
        confidence=float(data["confidence"]),
        summary=data.get("summary", ""),
        date_detected=data.get("date_detected"),
        suggested_filename=data.get("suggested_filename"),
        is_new_category=data.get("is_new_category", False),
        reasoning=data.get("reasoning", ""),
        belongs_here=bool(data.get("belongs_here", True)),
    )


# ---------------------------------------------------------------------------
# Provider: Claude (Anthropic)
# ---------------------------------------------------------------------------

def _classify_claude(
    file_path: Path,
    config: SorterConfig,
    existing_categories: list[str],
    client: object | None = None,
    parent_category: str | None = None,
) -> Classification:
    import anthropic

    if client is None:
        client = anthropic.Anthropic()

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

    system_prompt = _build_system_prompt(config, existing_categories, parent_category)
    response = client.messages.create(
        model=config.resolved_model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )

    data = _parse_response(response.content[0].text)
    return _to_classification(file_path, data)


# ---------------------------------------------------------------------------
# Provider: Gemini (Google)
# ---------------------------------------------------------------------------

def _classify_gemini(
    file_path: Path,
    config: SorterConfig,
    existing_categories: list[str],
    client: object | None = None,
    parent_category: str | None = None,
) -> Classification:
    from google import genai
    from google.genai import types

    if client is None:
        client = genai.Client()

    system_prompt = _build_system_prompt(config, existing_categories, parent_category)

    # Build parts: images first, then text
    parts: list = []
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        pages = _pdf_first_pages_as_images(file_path)
        for b64_data, media_type in pages:
            parts.append(types.Part.from_bytes(
                data=base64.standard_b64decode(b64_data),
                mime_type=media_type,
            ))
    else:
        mime, _ = mimetypes.guess_type(str(file_path))
        parts.append(types.Part.from_bytes(
            data=file_path.read_bytes(),
            mime_type=mime or "application/octet-stream",
        ))

    parts.append(f"Please classify this scanned household document. Filename: {file_path.name}")

    response = client.models.generate_content(
        model=config.resolved_model,
        contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=1024,
        ),
    )

    data = _parse_response(response.text)
    return _to_classification(file_path, data)


# ---------------------------------------------------------------------------
# Provider: Ollama (local — Gemma, Llama, etc.)
# ---------------------------------------------------------------------------

def _classify_ollama(
    file_path: Path,
    config: SorterConfig,
    existing_categories: list[str],
    client: object | None = None,
    parent_category: str | None = None,
) -> Classification:
    from openai import OpenAI

    if client is None:
        client = OpenAI(
            base_url=f"{config.ollama_host}/v1",
            api_key="ollama",
        )

    system_prompt = _build_system_prompt(config, existing_categories, parent_category)

    # Build message content with images
    user_content: list[dict] = []
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        pages = _pdf_first_pages_as_images(file_path)
        for b64_data, media_type in pages:
            if media_type == "application/pdf":
                # Ollama doesn't support raw PDFs — skip (pdf2image should handle this)
                continue
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64_data}"},
            })
    else:
        b64_data, media_type = _encode_image(file_path)
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64_data}"},
        })

    user_content.append({
        "type": "text",
        "text": f"Please classify this scanned household document. Filename: {file_path.name}",
    })

    response = client.chat.completions.create(
        model=config.resolved_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_tokens=1024,
    )

    data = _parse_response(response.choices[0].message.content)
    return _to_classification(file_path, data)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_PROVIDERS = {
    Provider.CLAUDE: _classify_claude,
    Provider.GEMINI: _classify_gemini,
    Provider.OLLAMA: _classify_ollama,
}


def create_client(config: SorterConfig) -> object:
    """Create a reusable API client for the configured provider."""
    if config.provider == Provider.CLAUDE:
        import anthropic
        return anthropic.Anthropic()
    elif config.provider == Provider.GEMINI:
        from google import genai
        return genai.Client()
    elif config.provider == Provider.OLLAMA:
        from openai import OpenAI
        return OpenAI(
            base_url=f"{config.ollama_host}/v1",
            api_key="ollama",
        )


def classify_document(
    file_path: Path,
    config: SorterConfig,
    existing_categories: list[str],
    client: object | None = None,
    parent_category: str | None = None,
) -> Classification:
    """Classify a single document using the configured provider.

    Args:
        file_path: Path to the document (PDF or image).
        config: Sorter configuration.
        existing_categories: Currently existing category folder names. In refine
            mode (parent_category given), this is the list of subfolders within
            the parent category instead.
        client: Optional pre-initialized client (from create_client).
        parent_category: If given, run in pass-2 refine mode: choose a subfolder
            within this top-level category (with a "doesn't belong here" escape hatch).

    Returns:
        A Classification result.
    """
    classify_fn = _PROVIDERS[config.provider]
    return classify_fn(file_path, config, existing_categories, client, parent_category)
