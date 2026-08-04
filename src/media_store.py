"""Local media library helpers for dashboard uploads."""
import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Set, Tuple

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS: Set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
ALLOWED_MIMES: Set[str] = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}
MAX_BYTES = 5 * 1024 * 1024  # 5 MB (X image limit is higher; keep uploads modest)


def media_root() -> Path:
    root = Path(os.getenv("MEDIA_UPLOAD_DIR", "media/uploads")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_allowed_image(filename: str, mime_type: Optional[str] = None) -> bool:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False
    if mime_type and mime_type.lower() not in ALLOWED_MIMES:
        return False
    return True


def save_upload(file_storage) -> Tuple[str, str, str, int]:
    """
    Save a Werkzeug FileStorage to disk.
    Returns (stored_filename, original_name, mime_type, file_size).
    """
    original = (file_storage.filename or "image").strip()
    if not is_allowed_image(original, getattr(file_storage, "mimetype", None)):
        raise ValueError("Only JPG, PNG, GIF, or WEBP images are allowed")

    data = file_storage.read()
    size = len(data)
    if size == 0:
        raise ValueError("Empty file")
    if size > MAX_BYTES:
        raise ValueError(f"Image too large (max {MAX_BYTES // (1024 * 1024)} MB)")

    ext = Path(original).suffix.lower()
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = media_root() / stored
    dest.write_bytes(data)
    mime = file_storage.mimetype or "application/octet-stream"
    return stored, original, mime, size


def save_bytes(
    data: bytes,
    original_name: str = "telegram.jpg",
    mime_type: str = "image/jpeg",
) -> Tuple[str, str, str, int]:
    """Save raw image bytes. Returns (stored_filename, original_name, mime_type, file_size)."""
    if not data:
        raise ValueError("Empty file")
    if len(data) > MAX_BYTES:
        raise ValueError(f"Image too large (max {MAX_BYTES // (1024 * 1024)} MB)")

    original = (original_name or "telegram.jpg").strip()
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        # Telegram often sends .jpg; sniff from mime
        mime = (mime_type or "").lower()
        if "png" in mime:
            ext = ".png"
        elif "gif" in mime:
            ext = ".gif"
        elif "webp" in mime:
            ext = ".webp"
        else:
            ext = ".jpg"
        original = f"telegram{ext}"

    stored = f"{uuid.uuid4().hex}{ext}"
    dest = media_root() / stored
    dest.write_bytes(data)
    return stored, original, mime_type or "image/jpeg", len(data)


def absolute_path_for(filename: str) -> Path:
    path = media_root() / Path(filename).name
    if not path.exists():
        raise FileNotFoundError(filename)
    return path


def delete_file(file_path: str) -> None:
    try:
        p = Path(file_path)
        if not p.is_absolute():
            p = media_root() / Path(file_path).name
        if p.exists():
            p.unlink()
    except Exception as e:
        logger.warning(f"Could not delete media file {file_path}: {e}")
