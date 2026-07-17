# Document Sorter

AI-powered household document sorter and classifier. Drop your scanned documents into an inbox folder, and the sorter uses AI vision to read each one, classify it, and file it into the right category folder. Works with a free local model (Ollama/Gemma), Google Gemini's free tier, or Claude.

## Features

- **Vision-based classification** — reads the actual document content (not just filenames) to determine the category
- **Three AI providers** — Ollama/Gemma (local, free, private — the default), Google Gemini (cloud, free tier), or Claude (cloud, paid, most accurate)
- **Confidence thresholds** — documents below the confidence threshold go to an "Unidentified" folder for manual review
- **Adjustable generality** — choose between broad categories ("Financial"), moderate ("Tax Documents"), or specific ("Federal Tax Returns/2024")
- **Two-pass sorting** — optional `--refine` pass sorts files *within* category folders into subfolders, with safeguards against over-fragmentation
- **Dynamic category management** — uses existing folders when they fit, creates new categories when needed (with a per-batch limit)
- **Duplicate detection** — SHA-256 hashing flags byte-identical files already in the destination
- **Descriptive renaming** — suggests meaningful filenames based on document content (e.g., "Electric Bill - 2024-03-15.pdf")
- **Date extraction** — pulls statement/invoice dates from documents when visible
- **Batch processing** — processes an entire inbox folder in one run
- **Dry run mode** — classify everything without moving any files
- **Google Drive integration** — scan to Drive, sort directly in Drive, no local storage needed
- **Web GUI** — browser-based interface with document thumbnails, click-to-classify, and manual review
- **Sorting reports** — text or JSON report of every decision made

## Quick Start

### 1. Install

Install with the extras for the provider(s) you want:

```bash
pip install -e '.[ollama]'   # local Gemma via Ollama (default provider, free)
pip install -e '.[gemini]'   # Google Gemini (free tier)
pip install -e '.[claude]'   # Claude (paid, most accurate)
pip install -e '.[all]'      # everything: all providers + Drive + web GUI
```

Add `drive` for Google Drive support and `gui` for the web GUI, e.g. `pip install -e '.[ollama,gui]'`.

Requires Python 3.10+. For PDF support, you also need `poppler-utils`:

```bash
# Ubuntu/Debian
sudo apt install poppler-utils

# macOS
brew install poppler
```

### 2. Set up your AI provider

**Ollama (default — local, free, private):**

Install [Ollama](https://ollama.com/download), then pull a vision-capable model:

```bash
ollama pull gemma4:e4b
```

That's it — no API key needed. Your documents never leave your machine.

**Gemini (cloud, generous free tier):**

Get a free API key from [Google AI Studio](https://aistudio.google.com/apikey) and export it:

```bash
export GEMINI_API_KEY=...
```

**Claude (cloud, paid, most accurate):**

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
  -p, --provider NAME     ollama | gemini | claude (default: ollama)
  --model MODEL           Model override (default per provider: gemma4:e4b /
                          gemini-2.0-flash / claude-sonnet-4-5)
  --ollama-host URL       Ollama server URL (default: http://localhost:11434)
  --move                  Move files instead of copying
  --dry-run               Classify only, don't move/copy files
  --no-duplicates         Disable duplicate detection
  --max-new-categories N  Max new folders per batch (default: 5)
  --refine                Pass 2: sort within category folders into subfolders
  --refine-min-files N    Min loose files for a folder to be refined (default: 8)
  --report FORMAT         text | json | none (default: text)
  --no-seed               Don't create default category folders

Google Drive:
  --drive                 Use Google Drive instead of local folders
  --drive-inbox NAME      Drive inbox folder name (default: 'Unsorted Scans')
  --drive-output NAME     Drive output folder name (default: 'Sorted Documents')
  --drive-credentials F   Path to OAuth credentials JSON (default: credentials.json)
  --drive-token F         Path to saved OAuth token (default: token.json)

Web GUI:
  --gui                   Launch the web GUI in your browser
  --port PORT             Port for the web GUI (default: 5000)
  --host HOST             Host for the web GUI (default: 127.0.0.1)
```

## Examples

**Basic sort with defaults (local Ollama/Gemma):**
```bash
docsort
```

**Use Gemini or Claude instead:**
```bash
docsort -p gemini
docsort -p claude
```

**Custom paths and strict threshold:**
```bash
docsort ~/scans -o ~/Documents/Filed -t 0.85
```

**Two-pass: sort broadly first, then refine into subfolders:**
```bash
docsort -g broad --move
docsort --refine
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

**Refine Drive category folders into subfolders (pass 2):**
```bash
docsort --drive --refine
```

**Launch the web GUI:**
```bash
docsort --gui
```

**GUI with custom inbox and port:**
```bash
docsort ~/scans --gui --port 8080
```

## Web GUI

The web GUI gives you a visual interface for sorting documents — no terminal needed after launching.

```bash
docsort --gui
```

Then open http://127.0.0.1:5000 in your browser. The GUI provides:

- **Thumbnail grid** — see all your inbox documents at a glance
- **Click to preview** — select a document and see its thumbnail in the sidebar
- **Preview Classification** — classify a single document before committing
- **Sort All** — batch sort with a progress bar, same as the CLI
- **Refine Subfolders** — run the pass-2 refinement from the toolbar
- **Manual assignment** — drag uncertain documents to the right category yourself
- **Create categories** — add new folders from the sidebar
- **Adjustable settings** — change threshold, generality, and AI provider from the toolbar
- **Results table** — see what was classified where after a batch run

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

## Two-Pass Sorting

For larger archives, a two-pass approach usually beats trying to get deep folder structures in one shot:

1. **Pass 1** — sort the inbox into broad, stable top-level categories: `docsort -g broad --move`
2. **Pass 2** — refine each category folder into subfolders: `docsort --refine`

Pass 2 looks at each top-level category folder and sorts its loose files into subfolders (e.g. `Financial/Bank Statements`, `Financial/Tax Documents`). Because the classifier only chooses among documents and subfolders of one category at a time, subfolder names come out much more consistent than single-pass deep sorting.

Built-in safeguards:

- **Minimum folder size** — folders with fewer than 8 loose files are skipped (`--refine-min-files` to adjust). Splitting four documents into three subfolders makes an archive worse, not better.
- **Graceful uncertainty** — if the classifier isn't confident about a subfolder, the file just stays loose in its category folder. It is *not* sent to Unidentified; being loose in the right category is a fine outcome.
- **Misfile escape hatch** — if the classifier decides a document doesn't belong in its category at all, it's re-classified against the top-level categories and moved to the right one (or to Unidentified if that's also uncertain). Pass-1 mistakes get surfaced instead of buried deeper.
- **Depth cap** — refinement only ever creates one level of subfolders, and never touches files already inside subfolders. Re-running it is safe and only processes new arrivals.
- **No renaming** — files keep the names they got in pass 1.

Refinement always *moves* files (copying within the sorted tree would create duplicates). It works for both local folders and Google Drive (`--drive --refine`), and it's the "Refine Subfolders" button in the web GUI.

## How It Works

1. **Discover** — scans the inbox folder for supported file types
2. **Seed** — creates default category folders if the output directory is empty (Financial, Medical, Insurance, Legal, Home & Property, Vehicle, Education, Employment, Personal, Correspondence)
3. **Classify** — sends each document to the configured AI provider's vision model with the list of existing categories and the generality prompt
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
├── classifier.py     # Multi-provider vision classification (Ollama, Gemini, Claude)
├── config.py         # Configuration dataclass and generality levels
├── drive.py          # Google Drive API integration (OAuth, upload, move)
├── folders.py        # Local folder management, file placement, duplicate detection
├── processor.py      # Batch orchestration (local + Drive)
├── report.py         # Text and JSON report generation
├── thumbnails.py     # Thumbnail generation for document previews
├── web.py            # Flask web GUI application
└── templates/
    └── index.html    # Web GUI HTML template
```
