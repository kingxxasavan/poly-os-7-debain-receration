"""Vara's knowledge of your files: a search index of the documents in your folders, kept up to date.

Documents, Desktop, Downloads, Projects (and Vara's workspace) are scanned: text and code, Markdown,
PDFs (pdftotext), Word and LibreOffice documents, and presentations. Their words go into a SQLite
full-text index in ~/.local/share/polyos/vara/index.db (only on this computer, readable only by
you); Vara searches it with search_documents and reads a whole document with read_document.

The index is refreshed in the background every half hour, a few hundred changed files at a time at
low priority, so it never gets in the way. Hidden folders, the private places Vara never looks
(vara_tools.PRIVATE) and big files are left out. Settings > Vara turns it off and clears it.
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
import subprocess
import threading
import time
import zipfile
from pathlib import Path

FOLDERS = ("Documents", "Desktop", "Downloads", "Projects", "Notes", "School", "Work")
TEXT = {".txt", ".md", ".markdown", ".rst", ".org", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".kt",
        ".sh", ".scad", ".ino", ".tex", ".xml", ".sql", ".log"}
DOCS = {".pdf", ".docx", ".odt", ".pptx", ".odp", ".rtf"}
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", "target", ".cache"}
MAX_BYTES = 25 * 1024 * 1024
MAX_CHARS = 200_000


def _xml_text(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    text = re.sub(r"</(w:p|text:p|text:h|a:p)>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def extract(path: Path) -> str:
    """The words in a file ("" if it can't be read)."""
    suffix = path.suffix.lower()
    try:
        if suffix in TEXT:
            with open(path, "rb") as fh:
                return fh.read(MAX_CHARS * 2).decode("utf-8", "replace")[:MAX_CHARS]
        if suffix == ".pdf":
            proc = subprocess.run(["pdftotext", "-q", "-l", "80", str(path), "-"], capture_output=True, timeout=45)
            return proc.stdout.decode("utf-8", "replace")[:MAX_CHARS] if proc.returncode == 0 else ""
        if suffix in (".docx", ".pptx", ".odt", ".odp"):
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                parts = (["word/document.xml"] if suffix == ".docx" else
                         sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                                key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group())) if suffix == ".pptx" else
                         ["content.xml"])
                text = "\n".join(_xml_text(zf.read(n)) for n in parts if n in names)
            return re.sub(r"[ \t]+", " ", text)[:MAX_CHARS]
        if suffix == ".rtf":
            raw = path.read_text("latin-1")[:MAX_CHARS * 2]
            return re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", raw)[:MAX_CHARS]
    except (OSError, zipfile.BadZipFile, KeyError, subprocess.TimeoutExpired, ValueError):
        return ""
    return ""


class DocIndex:
    def __init__(self, path: Path, home: Path, private: tuple[str, ...] = ()):
        self.path = path
        self.home = home
        self.private = [(home / p).resolve() for p in private]
        self._lock = threading.Lock()

    def _db(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER)")
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(path UNINDEXED, name, body, tokenize='porter unicode61')")
        db.execute("CREATE TABLE IF NOT EXISTS info (key TEXT PRIMARY KEY, value TEXT)")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return db

    def roots(self, extra: list[Path] | None = None) -> list[Path]:
        found = [self.home / f for f in FOLDERS if (self.home / f).is_dir()]
        for folder in extra or []:
            if folder.is_dir() and folder != self.home and not any(folder == r or r in folder.parents for r in found):
                found.append(folder)
        return found

    def _allowed(self, path: Path) -> bool:
        real = path.resolve()
        return not any(real == p or p in real.parents for p in self.private)

    def candidates(self, roots: list[Path]):
        for root in roots:
            if not self._allowed(root):  # e.g. a Desktop that's a link into ~/.ssh
                continue
            for folder, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP and self._allowed(Path(folder) / d)]
                for name in files:
                    path = Path(folder) / name
                    if not name.startswith(".") and path.suffix.lower() in TEXT | DOCS and self._allowed(path):
                        yield path

    def update(self, roots: list[Path], budget: float = 60, max_files: int = 400, clock=time.monotonic) -> dict:
        """Index what's new or changed (within the time and file budget), and drop what's gone."""
        start = clock()
        added = removed = 0
        finished = True
        with self._lock:
            db = self._db()
            known = {p: (m, s) for p, m, s in db.execute("SELECT path, mtime, size FROM files")}
            seen = set()
            for path in self.candidates(roots):
                key = str(path)
                seen.add(key)
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_size > MAX_BYTES or known.get(key) == (st.st_mtime, st.st_size):
                    continue
                if added >= max_files or clock() - start > budget:
                    finished = False  # the rest next time
                    continue
                body = extract(path)
                db.execute("DELETE FROM docs WHERE path = ?", (key,))
                db.execute("INSERT INTO docs (path, name, body) VALUES (?, ?, ?)", (key, path.name, body))
                db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (key, st.st_mtime, st.st_size))
                added += 1
            for key in set(known) - seen:
                if finished or not Path(key).exists():
                    db.execute("DELETE FROM docs WHERE path = ?", (key,))
                    db.execute("DELETE FROM files WHERE path = ?", (key,))
                    removed += 1
            db.execute("INSERT OR REPLACE INTO info VALUES ('updated', ?)", (str(time.time()),))
            db.commit()
            total = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            db.close()
        return {"added": added, "removed": removed, "files": total, "finished": finished}

    def search(self, query: str, limit: int = 8) -> list[dict]:
        words = re.findall(r"\w+", query.lower())[:12]
        if not words or not self.path.exists():
            return []
        with self._lock:
            db = self._db()
            try:
                for joiner in (" ", " OR "):  # every word first, then any of them
                    match = joiner.join(f'"{w}"' for w in words)
                    rows = db.execute("SELECT path, snippet(docs, 2, '«', '»', '…', 14) FROM docs WHERE docs MATCH ? "
                                      "ORDER BY bm25(docs, 0, 4, 1) LIMIT ?", (match, limit)).fetchall()
                    if rows:
                        return [{"path": p, "snippet": " ".join(s.split())} for p, s in rows]
                return []
            finally:
                db.close()

    def stats(self) -> dict:
        if not self.path.exists():
            return {"files": 0, "updated": None}
        with self._lock:
            db = self._db()
            try:
                files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                row = db.execute("SELECT value FROM info WHERE key = 'updated'").fetchone()
                return {"files": files, "updated": float(row[0]) if row else None}
            finally:
                db.close()

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
