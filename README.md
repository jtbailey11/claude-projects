# Document Sorter

AI-powered household document sorter and classifier. Drop your scanned documents into an inbox folder, and the sorter uses Claude's vision API to read each one, classify it, and file it into the right category folder.

## Features

- **Vision-based classification** — sends scanned PDFs and images to Claude, which reads the actual document content to determine the category
- **Confidence thresholds** — documents below the confidence threshold go to an "Unidentified" folder for manual review
- **Adjustable generality** — choose between broad categories ("Financial"), moderate ("Tax Documents"), or specific ("Federal Tax Returns/2024")
- **Dynamic category management** — uses existing folders when they fit, creates new categories when needed (with a per-batch limit)
- **Duplicate detection** — SHA-256 hashing flags byte-identical files already in the destination
- **Descriptive renaming** — suggests meaningful filenames based on document content (e.g., "Electric Bill - 2024-03-15.pdf")
- **Date extraction** — pulls statement/invoice dates from documents when visible
- **Batch processing** — processes an entire inbox folder in one run
- **Dry run mode** — classify everything without moving any files
- **Google Drive integration** — scan to Drive, sort directly in Drive, no local storage needed
- **Sorting reports** — text or JSON report of every decision made

## Quick Start

### 1. Install

```bash
pip install -e .
```

For Google Drive support:

```bash
pip install -e '.[drive]'
```

Requires Python 3.10+. For PDF support, you also need `poppler-utils`:

```bash
# Ubuntu/Debian
sudo apt install poppler-utils

# macOS
brew install poppler
```

### 2. Set your API key

```bash
cp .env.example .env
# Edit .env and add your Anthropic API key
```

Or export it directly:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Add documents

Put scanned documents (PDF, PNG, JPG, TIFF, BMP, WEBP) into an `unsorted/` folder:

```bash
mkdir unsorted
# Copy or scan your documents into unsorted/
```

### 4. Run

```bash
docsort
```

That's it. Your documents will be classified and copied into `sorted/<Category>/`.

## Usage

```
docsort [inbox] [options]

positional arguments:
  inbox                   Path to unsorted folder (default: ./unsorted)

options:
  -o, --output DIR        Output folder for sorted documents (default: ./sorted)
  -t, --threshold FLOAT   Confidence threshold 0.0-1.0 (default: 0.70)
  -g, --generality LEVEL  broad | moderate | specific (default: moderate)
  --move                  Move files instead of copying
  --dry-run               Classify only, don't move/copy files
  --no-duplicates         Disable duplicate detection
  --max-new-categories N  Max new folders per batch (default: 5)
  --model MODEL           Claude model to use (default: claude-sonnet-4-5-20250929)
  --report FORMAT         text | json | none (default: text)
  --no-seed               Don't create default category folders

Google Drive:
  --drive                 Use Google Drive instead of local folders
  --drive-inbox NAME      Drive inbox folder name (default: 'Unsorted Scans')
  --drive-output NAME     Drive output folder name (default: 'Sorted Documents')
  --drive-credentials F   Path to OAuth credentials JSON (default: credentials.json)
  --drive-token F         Path to saved OAuth token (default: token.json)
```

## Examples

**Basic sort with defaults (local):**
```bash
docsort
```

**Custom paths and strict threshold:**
```bash
docsort ~/scans -o ~/Documents/Filed -t 0.85
```

**Broad categories, move instead of copy:**
```bash
docsort --generality broad --move
```

**Specific categories for detailed filing:**
```bash
docsort --generality specific
```

**Dry run to preview what would happen:**
```bash
docsort --dry-run
```

**JSON report for programmatic use:**
```bash
docsort --report json
```

**Sort from Google Drive (scan to "Unsorted Scans" folder, sort into "Sorted Documents"):**
```bash
docsort --drive
```

**Drive with custom folder names and move mode (removes from inbox after sorting):**
```bash
docsort --drive --drive-inbox "Scanned Docs" --drive-output "Filed" --move
```

**Drive dry run to preview classifications without touching files:**
```bash
docsort --drive --dry-run
```

## Google Drive Setup

If you scan documents to Google Drive (or want your sorted files stored there), use the `--drive` flag. One-time setup:

### 1. Create a Google Cloud project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable the **Google Drive API** (APIs & Services → Enable APIs)

### 2. Create OAuth credentials

1. Go to APIs & Services → Credentials
2. Click **Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app**
4. Download the JSON file and save it as `credentials.json` in your project directory

### 3. First run

```bash
docsort --drive
```

On the first run, a browser window will open asking you to authorize access to your Google Drive. After authorizing, a `token.json` file is saved locally so you won't need to re-authorize.

### 4. Folder structure in Drive

The sorter looks for (or creates) two top-level folders in your Drive:

- **Unsorted Scans** — your inbox; scan documents here
- **Sorted Documents** — the output; category subfolders are created here

You can customize these names with `--drive-inbox` and `--drive-output`.

### Workflow: Scan → Drive → Sort

1. Configure your scanner to save to your "Unsorted Scans" folder in Google Drive
2. Run `docsort --drive` whenever you want to sort the inbox
3. Sorted documents appear in "Sorted Documents/Category Name/" in Drive
4. Use `--move` if you want the originals removed from the inbox after sorting

## How It Works

1. **Discover** — scans the inbox folder for supported file types
2. **Seed** — creates default category folders if the output directory is empty (Financial, Medical, Insurance, Legal, Home & Property, Vehicle, Education, Employment, Personal, Correspondence)
3. **Classify** — sends each document to Claude's vision API with the list of existing categories and the generality prompt
4. **Route** — based on the confidence score:
   - **Above threshold** → file goes to the classified category folder
   - **Below threshold** → file goes to "Unidentified" for manual review
   - **New category suggested** → creates the folder (up to the per-batch limit)
5. **Place** — copies (or moves) the file, with optional descriptive renaming
6. **Report** — writes a summary of every classification decision

## Default Categories

When starting fresh, these seed categories are created:

- Financial
- Medical
- Insurance
- Legal
- Home & Property
- Vehicle
- Education
- Employment
- Personal
- Correspondence

The classifier will use these when they fit, or propose new ones when they don't. You can also create your own folders in the output directory before running — the sorter will discover and use them.

## Project Structure

```
document_sorter/
├── __init__.py       # Package metadata
├── cli.py            # Command-line interface (argparse + rich)
├── classifier.py     # Claude vision API integration
├── config.py         # Configuration dataclass and generality levels
├── drive.py          # Google Drive API integration (OAuth, upload, move)
├── folders.py        # Local folder management, file placement, duplicate detection
├── processor.py      # Batch orchestration (local + Drive)
└── report.py         # Text and JSON report generation
```
