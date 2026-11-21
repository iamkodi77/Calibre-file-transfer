"""Persistent file library. Uploaded bytes are never used as filesystem paths."""
from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import sqlite3
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

BOOK_EXTENSIONS = {"epub", "mobi", "azw", "azw3", "azw4", "kfx", "fb2", "cbz", "cbr", "cb7", "djvu", "djv", "kepub"}
DOCUMENT_EXTENSIONS = {"pdf", "txt", "md", "rtf", "doc", "docx", "odt", "html", "htm", "xhtml", "chm", "ppt", "pptx"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "svg", "avif", "heic"}
MAX_XML_SIZE = 2 * 1024 * 1024
MAX_COVER_SIZE = 12 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_filename(name: str) -> str:
    # Multipart filenames may contain Windows or Unix paths. Keep only the basename.
    name = str(name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in _xml_safe(name) if ch >= " " and ch != "\x7f").strip().strip(".")
    if not name:
        raise ValueError("Please choose a file with a name.")
    # Leave room for a reasonably portable filename on the reader, preserving the extension.
    stem, ext = os.path.splitext(name)
    ext = ext[:32]
    while len((stem + ext).encode("utf-8")) > 220:
        stem = stem[:-1]
    name = (stem + ext).strip()
    if not name or name in {".", ".."}:
        raise ValueError("Please choose a file with a usable name.")
    return name


def extension_of(name: str) -> str:
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix if len(suffix) <= 31 else ""


def category_of(ext: str) -> str:
    if ext in BOOK_EXTENSIONS:
        return "books"
    if ext in DOCUMENT_EXTENSIONS:
        return "documents"
    if ext in IMAGE_EXTENSIONS:
        return "images"
    return "other"


def image_type(head: bytes) -> str | None:
    # Only safe raster formats are served inline. In particular, never inline SVG/HTML.
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _bounded_zip_read(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > limit:
        raise ValueError("Archive member is too large")
    with zf.open(info) as src:
        data = src.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Archive member is too large")
    return data


def _xml_safe(value: str) -> str:
    # XML 1.0 disallows controls and surrogate code points, even when escaped.
    return "".join(ch for ch in value if ch in "\t\n\r" or 0x20 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD or 0x10000 <= ord(ch) <= 0x10FFFF)


def _clean_text(value: str | None, limit: int = 300) -> str:
    return " ".join(_xml_safe(value or "").split())[:limit]


def read_epub_metadata(path: Path) -> tuple[str, str, bytes | None, str | None]:
    """Read common EPUB 2/3 title, author and cover without unpacking the archive."""
    try:
        with zipfile.ZipFile(path) as zf:
            container = ET.fromstring(_bounded_zip_read(zf, "META-INF/container.xml", MAX_XML_SIZE))
            rootfile = next((el.attrib.get("full-path") for el in container.iter() if el.tag.rsplit("}", 1)[-1] == "rootfile"), None)
            if not rootfile:
                return "", "", None, None
            rootfile = posixpath.normpath(unquote(rootfile))
            if rootfile.startswith("../") or rootfile.startswith("/"):
                return "", "", None, None
            opf = ET.fromstring(_bounded_zip_read(zf, rootfile, MAX_XML_SIZE))
            title = author = ""
            items = []
            cover_id = None
            for el in opf.iter():
                local = el.tag.rsplit("}", 1)[-1]
                if local == "title" and not title:
                    title = _clean_text("".join(el.itertext()))
                elif local == "creator" and not author:
                    author = _clean_text("".join(el.itertext()))
                elif local == "item":
                    items.append(el.attrib)
                elif local == "meta" and el.attrib.get("name", "").lower() == "cover":
                    cover_id = el.attrib.get("content")
            cover = next((item for item in items if "cover-image" in item.get("properties", "").split()), None)
            if not cover and cover_id:
                cover = next((item for item in items if item.get("id") == cover_id), None)
            if cover and cover.get("href"):
                member = posixpath.normpath(posixpath.join(posixpath.dirname(rootfile), unquote(cover["href"]).split("#", 1)[0]))
                if not member.startswith(("/", "../")):
                    data = _bounded_zip_read(zf, member, MAX_COVER_SIZE)
                    mime = image_type(data[:16])
                    if mime:
                        return title, author, data, mime
            return title, author, None, None
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile, RuntimeError):
        # A malformed EPUB is still a perfectly valid file to store and download.
        return "", "", None, None


class Library:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.files_dir = self.root / "files"
        self.covers_dir = self.root / "covers"
        self.tmp_dir = self.root / "tmp"
        for directory in (self.root, self.files_dir, self.covers_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "library.sqlite3"
        self.lock = threading.RLock()
        self._initialize()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self):
        with self.lock, self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '', extension TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL, size INTEGER NOT NULL, added_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, cover_kind TEXT, cover_mime TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    message TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id TEXT PRIMARY KEY, file_id TEXT NOT NULL, filename TEXT NOT NULL,
                    device_id TEXT NOT NULL, device_name TEXT NOT NULL, status TEXT NOT NULL,
                    bytes_sent INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS transfers_created_idx ON transfers(created_at DESC);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('library_uuid', ?)", (str(uuid.uuid4()),))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('library_name', 'My library')")
            db.execute("UPDATE transfers SET status='failed', error=?, updated_at=? WHERE status IN ('queued', 'sending')",
                       ("The server restarted before this transfer finished.", now_iso()))

    def setting(self, key: str) -> str:
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else ""

    def set_library_name(self, name: str) -> str:
        name = _clean_text(name, 80)
        if not name:
            raise ValueError("Library name cannot be empty.")
        with self.lock, self.db() as db:
            db.execute("UPDATE settings SET value=? WHERE key='library_name'", (name,))
        return name

    def log_event(self, kind: str, message: str, detail: str = "") -> None:
        with self.lock, self.db() as db:
            db.execute("INSERT INTO events(kind,message,detail,created_at) VALUES(?,?,?,?)",
                       (kind[:40], message[:250], detail[:500], now_iso()))

    def events(self, limit: int = 60) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 200),))]

    @staticmethod
    def _public(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["has_cover"] = bool(result.pop("cover_kind"))
        result.pop("cover_mime", None)
        return result

    def add_file(self, stream, original_name: str) -> dict:
        filename = clean_filename(original_name)
        file_id = str(uuid.uuid4())
        temp = self.tmp_dir / (file_id + ".part")
        destination = self.files_dir / file_id
        cover = self.covers_dir / file_id
        try:
            with temp.open("xb") as out:
                shutil.copyfileobj(stream, out, length=1024 * 1024)
            size = temp.stat().st_size
            os.replace(temp, destination)
            ext = extension_of(filename)
            title = _clean_text(Path(filename).stem.replace("_", " ")) or filename
            author = ""
            cover_kind = cover_mime = None
            if ext == "epub":
                epub_title, epub_author, cover_bytes, mime = read_epub_metadata(destination)
                title = epub_title or title
                author = epub_author
                if cover_bytes and mime:
                    cover.write_bytes(cover_bytes)
                    cover_kind, cover_mime = "extracted", mime
            if not cover_kind and ext in IMAGE_EXTENSIONS:
                with destination.open("rb") as f:
                    mime = image_type(f.read(16))
                if mime:
                    cover_kind, cover_mime = "file", mime
            stamp = now_iso()
            with self.lock, self.db() as db:
                db.execute("""INSERT INTO files(id,filename,title,author,extension,category,size,added_at,updated_at,cover_kind,cover_mime)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                           (file_id, filename, title, author, ext, category_of(ext), size, stamp, stamp, cover_kind, cover_mime))
                db.execute("INSERT INTO events(kind,message,detail,created_at) VALUES(?,?,?,?)", ("upload", "Added to your library", filename, stamp))
            return self.get_file(file_id)
        except Exception:
            temp.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            cover.unlink(missing_ok=True)
            raise

    def list_files(self) -> list[dict]:
        with self.db() as db:
            return [self._public(row) for row in db.execute("SELECT * FROM files ORDER BY added_at DESC, rowid DESC")]

    def get_file(self, file_id: str) -> dict | None:
        with self.db() as db:
            return self._public(db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone())

    def file_path(self, file_id: str) -> Path:
        # A caller must first find the ID in the database; never join arbitrary paths.
        if not re.fullmatch(r"[0-9a-f-]{36}", file_id):
            raise ValueError("Invalid file ID")
        return self.files_dir / file_id

    def cover_for(self, file_id: str) -> tuple[Path, str] | None:
        with self.db() as db:
            row = db.execute("SELECT cover_kind,cover_mime FROM files WHERE id=?", (file_id,)).fetchone()
        if not row or not row["cover_kind"]:
            return None
        path = self.file_path(file_id) if row["cover_kind"] == "file" else self.covers_dir / file_id
        return (path, row["cover_mime"]) if path.is_file() else None

    def update_file(self, file_id: str, title: str, author: str) -> dict | None:
        title, author = _clean_text(title), _clean_text(author)
        if not title:
            raise ValueError("Title cannot be empty.")
        with self.lock, self.db() as db:
            cursor = db.execute("UPDATE files SET title=?,author=?,updated_at=? WHERE id=?", (title, author, now_iso(), file_id))
            if not cursor.rowcount:
                return None
        return self.get_file(file_id)

    def delete_file(self, file_id: str) -> bool:
        with self.lock, self.db() as db:
            row = db.execute("SELECT filename FROM files WHERE id=?", (file_id,)).fetchone()
            if not row:
                return False
            db.execute("DELETE FROM files WHERE id=?", (file_id,))
            db.execute("INSERT INTO events(kind,message,detail,created_at) VALUES(?,?,?,?)", ("delete", "Removed from your library", row[0], now_iso()))
        for path in (self.file_path(file_id), self.covers_dir / file_id):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.exception("Could not remove %s", path)
        return True

    def create_transfer(self, file: dict, device_id: str, device_name: str) -> dict:
        transfer_id = str(uuid.uuid4())
        stamp = now_iso()
        with self.lock, self.db() as db:
            db.execute("""INSERT INTO transfers(id,file_id,filename,device_id,device_name,status,total_bytes,created_at,updated_at)
                          VALUES(?,?,?,?,?,'queued',?,?,?)""",
                       (transfer_id, file["id"], file["filename"], device_id, device_name, file["size"], stamp, stamp))
        return self.get_transfer(transfer_id)

    def get_transfer(self, transfer_id: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            return dict(row) if row else None

    def transfers(self, limit: int = 100) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM transfers ORDER BY created_at DESC, rowid DESC LIMIT ?", (min(max(limit, 1), 200),))]

    def update_transfer(self, transfer_id: str, status: str, bytes_sent: int | None = None, error: str = "") -> None:
        with self.lock, self.db() as db:
            row = db.execute("SELECT status,filename,device_name,total_bytes,bytes_sent FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row or row["status"] in ("sent", "failed"):
                return
            sent = row["bytes_sent"] if bytes_sent is None else max(0, min(bytes_sent, row["total_bytes"]))
            db.execute("UPDATE transfers SET status=?,bytes_sent=?,error=?,updated_at=? WHERE id=?", (status, sent, error[:500], now_iso(), transfer_id))
            if status == "sent":
                db.execute("INSERT INTO events(kind,message,detail,created_at) VALUES(?,?,?,?)", ("transfer", "Sent to " + row["device_name"], row["filename"], now_iso()))
            elif status == "failed":
                db.execute("INSERT INTO events(kind,message,detail,created_at) VALUES(?,?,?,?)", ("error", "Transfer could not finish", row["filename"] + " · " + error[:200], now_iso()))
