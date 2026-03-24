from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote, urlparse


def normalize_filename(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    decoded = unquote(text)
    parsed = urlparse(decoded)
    candidate = parsed.path or decoded
    name = Path(candidate).name or Path(decoded).name
    normalized = re.sub(r"\s+", " ", str(name or "").strip())
    return normalized or None


def file_extension(value: str | None) -> str | None:
    name = normalize_filename(value)
    if not name:
        return None
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix or None


def guess_mime_type(*values: str | None) -> str | None:
    for value in values:
        name = normalize_filename(value)
        if not name:
            continue
        mime_type, _encoding = mimetypes.guess_type(name, strict=False)
        if mime_type:
            return mime_type
    return None
