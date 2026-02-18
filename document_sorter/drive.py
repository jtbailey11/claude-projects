"""Google Drive integration — authenticate, list, download, upload, and move files."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

    HAS_DRIVE = True
except ImportError:
    HAS_DRIVE = False

# If modifying scopes, delete token.json so the user re-authenticates.
SCOPES = ["https://www.googleapis.com/auth/drive"]

TOKEN_PATH = Path("token.json")
CREDENTIALS_PATH = Path("credentials.json")

# Google Drive MIME type for folders
FOLDER_MIME = "application/vnd.google-apps.folder"


def check_drive_available() -> None:
    """Raise if Google Drive dependencies aren't installed."""
    if not HAS_DRIVE:
        raise ImportError(
            "Google Drive dependencies not installed. "
            "Install them with: pip install -e '.[drive]'"
        )


def authenticate(
    credentials_path: Path = CREDENTIALS_PATH,
    token_path: Path = TOKEN_PATH,
) -> object:
    """Authenticate with Google Drive and return a Drive API service object.

    On first run, opens a browser for OAuth consent. Subsequent runs use the
    saved token.json.
    """
    check_drive_available()

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"OAuth credentials file not found at {credentials_path.resolve()}.\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials → "
                    "OAuth 2.0 Client IDs → Download JSON, and save as 'credentials.json'."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES,
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


def find_folder_by_name(
    service: object,
    name: str,
    parent_id: str | None = None,
) -> str | None:
    """Find a folder by name under an optional parent. Returns folder ID or None."""
    q = f"name = '{name}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"

    results = service.files().list(
        q=q, spaces="drive", fields="files(id, name)", pageSize=1,
    ).execute()

    files = results.get("files", [])
    return files[0]["id"] if files else None


def find_or_create_folder(
    service: object,
    name: str,
    parent_id: str | None = None,
) -> str:
    """Find a folder by name, or create it. Returns the folder ID."""
    existing = find_folder_by_name(service, name, parent_id)
    if existing:
        return existing

    metadata = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def ensure_folder_path(service: object, path: str, root_id: str | None = None) -> str:
    """Create nested folders from a path like 'Insurance/Auto/Claims'.

    Returns the ID of the deepest folder.
    """
    parent_id = root_id
    for part in path.split("/"):
        part = part.strip()
        if not part:
            continue
        parent_id = find_or_create_folder(service, part, parent_id)
    return parent_id


def list_subfolders(service: object, parent_id: str) -> list[dict]:
    """List immediate child folders of a parent. Returns list of {id, name}."""
    q = f"'{parent_id}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
    results = service.files().list(
        q=q, spaces="drive", fields="files(id, name)", pageSize=200,
    ).execute()
    return results.get("files", [])


def list_files_in_folder(
    service: object,
    folder_id: str,
    extensions: tuple[str, ...] | None = None,
) -> list[dict]:
    """List files in a Drive folder. Returns list of {id, name, mimeType}.

    Optionally filter by file extension.
    """
    q = f"'{folder_id}' in parents and mimeType != '{FOLDER_MIME}' and trashed = false"
    results = service.files().list(
        q=q, spaces="drive", fields="files(id, name, mimeType, md5Checksum)",
        pageSize=500,
    ).execute()

    files = results.get("files", [])
    if extensions:
        files = [
            f for f in files
            if any(f["name"].lower().endswith(ext) for ext in extensions)
        ]
    return files


def download_file(service: object, file_id: str, dest_path: Path) -> Path:
    """Download a file from Drive to a local path."""
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest_path


def download_to_temp(service: object, file_id: str, filename: str) -> Path:
    """Download a Drive file to a temp directory. Returns the local Path."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="docsort_"))
    return download_file(service, file_id, tmp_dir / filename)


def upload_file(
    service: object,
    local_path: Path,
    parent_id: str,
    name: str | None = None,
) -> str:
    """Upload a local file to a Drive folder. Returns the new file ID."""
    file_name = name or local_path.name
    metadata = {"name": file_name, "parents": [parent_id]}
    media = MediaFileUpload(str(local_path), resumable=True)
    result = service.files().create(
        body=metadata, media_body=media, fields="id",
    ).execute()
    return result["id"]


def move_file(service: object, file_id: str, new_parent_id: str) -> None:
    """Move a file to a different folder in Drive."""
    # Get current parents
    f = service.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(f.get("parents", []))

    service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=previous_parents,
        fields="id, parents",
    ).execute()


def rename_file(service: object, file_id: str, new_name: str) -> None:
    """Rename a file in Drive."""
    service.files().update(
        fileId=file_id, body={"name": new_name}, fields="id, name",
    ).execute()


def check_duplicate_by_hash(
    service: object,
    folder_id: str,
    md5: str,
) -> dict | None:
    """Check if a file with the same MD5 hash exists in a folder."""
    files = list_files_in_folder(service, folder_id)
    for f in files:
        if f.get("md5Checksum") == md5:
            return f
    return None


def get_file_md5(service: object, file_id: str) -> str | None:
    """Get the MD5 checksum of a Drive file."""
    f = service.files().get(fileId=file_id, fields="md5Checksum").execute()
    return f.get("md5Checksum")
