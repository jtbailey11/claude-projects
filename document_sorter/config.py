"""Configuration and settings for the document sorter."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Generality(Enum):
    """Controls how broad or specific category folders are.

    BROAD:    Few top-level folders (e.g., "Financial", "Medical", "Legal")
    MODERATE: Balanced grouping  (e.g., "Tax Documents", "Insurance Claims")
    SPECIFIC: Fine-grained folders (e.g., "Federal Tax Returns/2024", "Auto Insurance/Claims")
    """

    BROAD = "broad"
    MODERATE = "moderate"
    SPECIFIC = "specific"


class Provider(Enum):
    """AI provider for document classification."""

    OLLAMA = "ollama"
    GEMINI = "gemini"
    CLAUDE = "claude"


PROVIDER_DEFAULT_MODELS = {
    Provider.OLLAMA: "gemma4:e4b",
    Provider.GEMINI: "gemini-2.0-flash",
    Provider.CLAUDE: "claude-sonnet-4-5-20250929",
}


# Descriptions sent to the classifier so it understands the desired granularity
GENERALITY_PROMPTS = {
    Generality.BROAD: (
        "Use broad, high-level categories. Aim for roughly 5-10 top-level folders "
        "like 'Financial', 'Medical', 'Legal', 'Insurance', 'Home', 'Vehicle', "
        "'Education', 'Personal', 'Correspondence'."
    ),
    Generality.MODERATE: (
        "Use moderately specific categories. Aim for subcategory-level folders "
        "like 'Tax Documents', 'Bank Statements', 'Medical Bills', 'Insurance Policies', "
        "'Warranties', 'Receipts', 'School Records'."
    ),
    Generality.SPECIFIC: (
        "Use very specific categories with sub-folders where appropriate. "
        "Examples: 'Tax Returns/Federal/2024', 'Insurance/Auto/Claims', "
        "'Medical/Lab Results', 'Utilities/Electric'. Be as precise as possible."
    ),
}


@dataclass
class SorterConfig:
    """All tunable settings for the document sorter."""

    # Paths
    inbox_dir: Path = Path("unsorted")
    output_dir: Path = Path("sorted")
    unidentified_dir_name: str = "Unidentified"

    # Classification
    confidence_threshold: float = 0.70
    generality: Generality = Generality.MODERATE
    provider: Provider = Provider.OLLAMA
    model: str = ""  # empty = use provider default
    ollama_host: str = "http://localhost:11434"

    # Behaviour
    move_files: bool = False  # False = copy, True = move originals
    dry_run: bool = False
    max_new_categories: int = 5  # limit on new folders created per batch

    # Two-pass refinement (pass 2: sort within category folders into subfolders)
    refine_min_files: int = 8  # only refine category folders with at least this many loose files

    # File types to process
    supported_extensions: tuple[str, ...] = (
        ".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp",
    )

    # Duplicate detection
    detect_duplicates: bool = True

    # Google Drive settings
    use_drive: bool = False
    drive_inbox_folder: str = "Unsorted Scans"
    drive_output_folder: str = "Sorted Documents"
    drive_credentials_path: Path = Path("credentials.json")
    drive_token_path: Path = Path("token.json")

    # Default household categories to seed the folder structure
    seed_categories: list[str] = field(default_factory=lambda: [
        "Financial",
        "Medical",
        "Insurance",
        "Legal",
        "Home & Property",
        "Vehicle",
        "Education",
        "Employment",
        "Personal",
        "Correspondence",
    ])

    @property
    def resolved_model(self) -> str:
        """Return the model name, falling back to provider default if not set."""
        if self.model:
            return self.model
        return PROVIDER_DEFAULT_MODELS[self.provider]
