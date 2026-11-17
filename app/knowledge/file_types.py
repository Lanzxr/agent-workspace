from __future__ import annotations

import codecs
import zipfile
from dataclasses import dataclass
from pathlib import Path

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg",
    ".env", ".log", ".xml", ".html", ".htm", ".sql", ".sh", ".py", ".js", ".ts", ".java", ".go",
    ".rs", ".properties", ".rst", ".tex",
}
OFFICE_EXTENSIONS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | OFFICE_EXTENSIONS | IMAGE_EXTENSIONS | {".pdf"}
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class DetectedFile:
    filename: str
    content_type: str
    sensitive: bool


def detect_file(path: Path, original_name: str) -> DetectedFile:
    """Use content signatures first; extensions only disambiguate legacy Office and plain text."""
    extension = Path(original_name).suffix.lower()
    with path.open("rb") as source:
        sample = source.read(8192)
    if not sample:
        raise ValueError("empty files are not supported")

    if sample.startswith(b"%PDF-"):
        normalized, content_type = ".pdf", "application/pdf"
    elif sample.startswith(PNG_SIGNATURE):
        normalized, content_type = ".png", "image/png"
    elif sample.startswith(b"\xff\xd8\xff"):
        normalized, content_type = ".jpg", "image/jpeg"
    elif sample.startswith(OLE_SIGNATURE):
        normalized = extension if extension in {".doc", ".ppt", ".xls"} else ""
        if not normalized:
            raise ValueError("legacy Office binary type cannot be identified from its contents")
        content_type = {".doc": "application/msword", ".ppt": "application/vnd.ms-powerpoint",
                        ".xls": "application/vnd.ms-excel"}[normalized]
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        if "word/document.xml" in names:
            normalized, content_type = ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif any(name.startswith("xl/worksheets/") for name in names):
            normalized, content_type = ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif "ppt/presentation.xml" in names:
            normalized, content_type = ".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        else:
            raise ValueError("ZIP file is not a supported Office document")
    else:
        if b"\x00" in sample:
            raise ValueError("binary file content does not match a supported format")
        _validate_utf8_text(path)
        if extension in OFFICE_EXTENSIONS or extension in IMAGE_EXTENSIONS or extension == ".pdf":
            raise ValueError("file extension does not match the file contents")
        normalized = extension if extension in TEXT_EXTENSIONS else ".txt"
        content_type = "text/plain; charset=utf-8"

    stem = Path(original_name).stem or "attachment"
    safe_stem = "".join(char for char in stem if char.isalnum() or char in " ._-()")[:140].strip(" .") or "attachment"
    filename = f"{safe_stem}{normalized}"
    lower_name = Path(original_name).name.lower()
    sensitive = extension == ".env" or lower_name == ".env" or lower_name.startswith(".env.")
    return DetectedFile(filename, content_type, sensitive)


def _validate_utf8_text(path: Path) -> None:
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                if b"\x00" in chunk:
                    raise ValueError("binary file content does not match a supported format")
                decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError as error:
        raise ValueError("text files must be UTF-8 encoded") from error
