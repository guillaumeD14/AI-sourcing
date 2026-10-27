"""Index local SQLite : catalogues, passages produits (FTS5 avec repli), empreintes d'images.

Chaque opération ouvre sa propre connexion (sûr avec les threads de Streamlit). L'index est reconstructible :
si le schéma change, il est recréé et la synchronisation repart des PDF Cloudinary.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np

from sourcing_images import DESCRIPTOR_DIMS, ImageDescriptor, similarity_scores, vector_from_blob
from sourcing_pdf_extract import ExtractedImage, ExtractedPassage
from sourcing_textutils import fts_match_query, normalize

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
  public_id TEXT PRIMARY KEY, logical_name TEXT, supplier TEXT, part_index INTEGER, part_total INTEGER,
  page_offset INTEGER DEFAULT 0, version TEXT, size_bytes INTEGER, etag TEXT, resource_type TEXT, secure_url TEXT,
  page_count INTEGER DEFAULT 0, passages INTEGER DEFAULT 0, images INTEGER DEFAULT 0,
  status TEXT, status_detail TEXT, indexed_at TEXT);
CREATE TABLE IF NOT EXISTS passages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL, local_page INTEGER, kind TEXT, title TEXT, text TEXT,
  refs TEXT, weights TEXT, unit_weights TEXT, packaging TEXT, structured INTEGER, content_hash TEXT);
CREATE INDEX IF NOT EXISTS idx_passages_file ON passages(public_id);
CREATE TABLE IF NOT EXISTS images (
  id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL, local_page INTEGER, passage_id INTEGER,
  vec BLOB, dhash INTEGER, thumb BLOB);
CREATE INDEX IF NOT EXISTS idx_images_file ON images(public_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class FileRecord:
    public_id: str
    logical_name: str = ""
    supplier: str = ""
    part_index: int | None = None
    part_total: int | None = None
    page_offset: int = 0
    version: str | None = None
    size_bytes: int | None = None
    etag: str | None = None
    resource_type: str | None = None
    secure_url: str | None = None
    page_count: int = 0
    passages: int = 0
    images: int = 0
    status: str = "pending"
    status_detail: str = ""
    indexed_at: str | None = None


@dataclass
class StoreStats:
    files: int
    indexed: int
    needs_ocr: int
    errors: int
    passages: int
    images: int


@dataclass
class PassageRow:
    id: int
    public_id: str
    local_page: int
    kind: str
    title: str
    text: str
    refs: list[str]
    weights_g: list[float]
    unit_weights_g: list[float]
    packaging: list[str]
    structured: bool
    content_hash: str
    supplier: str
    logical_name: str
    page_offset: int
    part_index: int | None
    bm25: float = 0.0

    @property
    def original_page(self) -> int:
        return self.local_page + self.page_offset


@dataclass
class ImageMatch:
    image_id: int
    passage_id: int | None
    public_id: str
    local_page: int
    similarity: float


_FILE_COLUMNS = [f for f in FileRecord.__dataclass_fields__]


class IndexStore:
    def __init__(self, path: str | Path, *, use_fts: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._image_cache: tuple[int, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None
        self.fts_available = use_fts and self._probe_fts()
        self._init_schema()

    # -- connexion -------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _probe_fts() -> bool:
        try:
            probe = sqlite3.connect(":memory:")
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
            probe.close()
            return True
        except sqlite3.Error:
            return False

    def _init_schema(self) -> None:
        with self._write_lock, self._conn() as conn:
            conn.executescript("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);")
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is not None and int(row["value"]) != SCHEMA_VERSION:
                for table in ("files", "passages", "images", "passages_fts"):
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.executescript(_SCHEMA)
            if self.fts_available:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(title, body, tokenize='unicode61 remove_diacritics 2')")
            else:
                conn.execute("CREATE TABLE IF NOT EXISTS passages_fts (rowid INTEGER PRIMARY KEY, title TEXT, body TEXT)")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('index_version', '1')")

    # -- version / stats ---------------------------------------------------

    def version(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='index_version'").fetchone()
            return int(row["value"]) if row else 1

    def _bump(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='index_version'")

    def stats(self) -> StoreStats:
        with self._conn() as conn:
            def count(sql: str) -> int:
                return int(conn.execute(sql).fetchone()[0])
            return StoreStats(
                files=count("SELECT COUNT(*) FROM files"), indexed=count("SELECT COUNT(*) FROM files WHERE status='indexed'"),
                needs_ocr=count("SELECT COUNT(*) FROM files WHERE status='needs_ocr'"),
                errors=count("SELECT COUNT(*) FROM files WHERE status='error'"),
                passages=count("SELECT COUNT(*) FROM passages"), images=count("SELECT COUNT(*) FROM images"))

    def is_empty(self) -> bool:
        return self.stats().files == 0

    # -- fichiers ----------------------------------------------------------

    @staticmethod
    def _file_from_row(row: sqlite3.Row) -> FileRecord:
        return FileRecord(**{k: row[k] for k in _FILE_COLUMNS})

    def get_file(self, public_id: str) -> FileRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM files WHERE public_id=?", (public_id,)).fetchone()
        return self._file_from_row(row) if row else None

    def list_files(self) -> list[FileRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM files ORDER BY supplier, logical_name, part_index, public_id").fetchall()
        return [self._file_from_row(r) for r in rows]

    def suppliers(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT supplier FROM files WHERE status='indexed' AND supplier<>'' ORDER BY supplier").fetchall()
        return [r["supplier"] for r in rows]

    def save_file_status(self, record: FileRecord) -> None:
        """Enregistre un fichier sans passages (PDF scanné, erreur) en supprimant d'éventuels anciens passages."""
        with self._write_lock, self._conn() as conn:
            self._delete_content(conn, record.public_id)
            self._upsert_file(conn, record)
            self._bump(conn)

    def replace_file(self, record: FileRecord, passages: Sequence[ExtractedPassage], images: Sequence[ExtractedImage]) -> None:
        """Remplace atomiquement tout le contenu d'un PDF (réindexation incrémentale)."""
        with self._write_lock, self._conn() as conn:
            self._delete_content(conn, record.public_id)
            ids: list[int] = []
            for p in passages:
                cursor = conn.execute(
                    "INSERT INTO passages(public_id, local_page, kind, title, text, refs, weights, unit_weights, packaging, structured, content_hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (record.public_id, p.local_page, p.kind, p.title, p.text, json.dumps(p.refs), json.dumps(p.weights_g),
                     json.dumps(p.unit_weights_g), json.dumps(p.packaging, ensure_ascii=False), int(p.structured), p.content_hash))
                row_id = int(cursor.lastrowid or 0)
                ids.append(row_id)
                conn.execute("INSERT INTO passages_fts(rowid, title, body) VALUES (?,?,?)", (row_id, normalize(p.title), normalize(p.text)))
            for image in images:
                passage_id = ids[image.passage_index] if image.passage_index is not None and image.passage_index < len(ids) else None
                dhash = image.descriptor.dhash
                conn.execute("INSERT INTO images(public_id, local_page, passage_id, vec, dhash, thumb) VALUES (?,?,?,?,?,?)",
                             (record.public_id, image.local_page, passage_id, image.descriptor.vector_blob(),
                              dhash - (1 << 64) if dhash >= (1 << 63) else dhash, image.descriptor.thumb))
            record.passages, record.images, record.indexed_at = len(passages), len(images), utc_now()
            self._upsert_file(conn, record)
            self._bump(conn)

    def remove_files(self, public_ids: Sequence[str]) -> None:
        if not public_ids:
            return
        with self._write_lock, self._conn() as conn:
            for public_id in public_ids:
                self._delete_content(conn, public_id)
                conn.execute("DELETE FROM files WHERE public_id=?", (public_id,))
            self._bump(conn)

    def update_grouping(self, updates: Sequence[tuple[str, str, str, int | None, int | None, int]]) -> None:
        """``(public_id, logical_name, supplier, part_index, part_total, page_offset)`` sans toucher aux passages."""
        with self._write_lock, self._conn() as conn:
            conn.executemany("UPDATE files SET logical_name=?, supplier=?, part_index=?, part_total=?, page_offset=? WHERE public_id=?",
                             [(ln, sup, pi, pt, off, pid) for pid, ln, sup, pi, pt, off in updates])
            self._bump(conn)

    @staticmethod
    def _delete_content(conn: sqlite3.Connection, public_id: str) -> None:
        conn.execute("DELETE FROM passages_fts WHERE rowid IN (SELECT id FROM passages WHERE public_id=?)", (public_id,))
        conn.execute("DELETE FROM passages WHERE public_id=?", (public_id,))
        conn.execute("DELETE FROM images WHERE public_id=?", (public_id,))

    @staticmethod
    def _upsert_file(conn: sqlite3.Connection, record: FileRecord) -> None:
        values = [getattr(record, c) for c in _FILE_COLUMNS]
        placeholders = ",".join("?" for _ in _FILE_COLUMNS)
        conn.execute(f"INSERT OR REPLACE INTO files({','.join(_FILE_COLUMNS)}) VALUES ({placeholders})", values)

    # -- recherche plein texte ----------------------------------------------

    _SELECT = ("SELECT p.*, f.supplier AS f_supplier, f.logical_name AS f_logical, f.page_offset AS f_offset, f.part_index AS f_part "
               "FROM passages p JOIN files f ON f.public_id = p.public_id")

    @staticmethod
    def _passage_from_row(row: sqlite3.Row, bm25: float = 0.0) -> PassageRow:
        return PassageRow(
            id=row["id"], public_id=row["public_id"], local_page=row["local_page"], kind=row["kind"], title=row["title"] or "",
            text=row["text"] or "", refs=json.loads(row["refs"] or "[]"), weights_g=json.loads(row["weights"] or "[]"),
            unit_weights_g=json.loads(row["unit_weights"] or "[]"), packaging=json.loads(row["packaging"] or "[]"),
            structured=bool(row["structured"]), content_hash=row["content_hash"] or "", supplier=row["f_supplier"] or "",
            logical_name=row["f_logical"] or "", page_offset=row["f_offset"] or 0, part_index=row["f_part"], bm25=bm25)

    def search_candidates(self, terms: Sequence[str], *, suppliers: Sequence[str] = (), limit: int = 400) -> list[PassageRow]:
        """Candidats classés par BM25 (FTS5) ou, sans FTS5, par nombre de termes présents."""
        terms = [t for t in terms if normalize(t)]
        if not terms:
            return []
        supplier_sql, supplier_args = "", []
        if suppliers:
            supplier_sql = f" AND f.supplier IN ({','.join('?' for _ in suppliers)})"
            supplier_args = list(suppliers)
        with self._conn() as conn:
            if self.fts_available:
                rows = conn.execute(
                    "SELECT p.*, f.supplier AS f_supplier, f.logical_name AS f_logical, f.page_offset AS f_offset, f.part_index AS f_part, "
                    "-bm25(passages_fts, 4.0, 1.0) AS score FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid "
                    "JOIN files f ON f.public_id = p.public_id "
                    f"WHERE passages_fts MATCH ?{supplier_sql} AND f.status='indexed' ORDER BY score DESC LIMIT ?",
                    [fts_match_query(terms), *supplier_args, limit]).fetchall()
                return [self._passage_from_row(r, float(r["score"])) for r in rows]
            return self._fallback_search(conn, terms, supplier_sql, supplier_args, limit)

    def _fallback_search(self, conn: sqlite3.Connection, terms: Sequence[str], supplier_sql: str, supplier_args: list[str], limit: int) -> list[PassageRow]:
        norm_terms = list(dict.fromkeys(normalize(t) for t in terms))[:20]
        conditions = " OR ".join("(x.body LIKE ? OR x.title LIKE ?)" for _ in norm_terms)
        args: list[Any] = [a for t in norm_terms for a in (f"%{t}%", f"%{t}%")]
        rows = conn.execute(
            f"{self._SELECT} JOIN passages_fts x ON x.rowid = p.id WHERE ({conditions}){supplier_sql} AND f.status='indexed' LIMIT ?",
            [*args, *supplier_args, limit * 5]).fetchall()
        scored = []
        for row in rows:
            body = normalize(f"{row['title']} {row['text']}")
            scored.append((float(sum(body.count(t) for t in norm_terms)), row))
        scored.sort(key=lambda item: -item[0])
        return [self._passage_from_row(r, s) for s, r in scored[:limit]]

    def get_passages(self, ids: Sequence[int]) -> list[PassageRow]:
        if not ids:
            return []
        with self._conn() as conn:
            rows = conn.execute(f"{self._SELECT} WHERE p.id IN ({','.join('?' for _ in ids)})", list(ids)).fetchall()
        by_id = {r["id"]: self._passage_from_row(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    # -- images -------------------------------------------------------------

    def _image_matrix(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Matrice des empreintes, rechargée seulement si l'index a changé."""
        version = self.version()
        if self._image_cache and self._image_cache[0] == version:
            return self._image_cache[1:]
        with self._conn() as conn:
            rows = conn.execute("SELECT i.id, i.passage_id, i.vec, i.dhash FROM images i JOIN files f ON f.public_id=i.public_id WHERE f.status='indexed'").fetchall()
        ids = np.array([r["id"] for r in rows], dtype=np.int64)
        matrix = np.stack([vector_from_blob(r["vec"]) for r in rows]) if rows else np.zeros((0, DESCRIPTOR_DIMS), dtype=np.float32)
        dhashes = np.array([r["dhash"] & ((1 << 64) - 1) for r in rows], dtype=np.uint64)
        passage_ids = np.array([r["passage_id"] if r["passage_id"] is not None else -1 for r in rows], dtype=np.int64)
        self._image_cache = (version, ids, matrix, dhashes, passage_ids)
        return ids, matrix, dhashes, passage_ids

    def visual_matches(self, descriptor: ImageDescriptor, *, top_k: int = 60, min_similarity: float = 0.15,
                       suppliers: Sequence[str] = ()) -> list[ImageMatch]:
        ids, matrix, dhashes, passage_ids = self._image_matrix()
        if ids.size == 0:
            return []
        scores = similarity_scores(descriptor, matrix, dhashes)
        order = np.argsort(-scores)[: top_k * (3 if suppliers else 1)]
        wanted = [int(ids[i]) for i in order if scores[i] >= min_similarity]
        if not wanted:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT i.id, i.public_id, i.local_page, i.passage_id, f.supplier FROM images i JOIN files f ON f.public_id=i.public_id "
                f"WHERE i.id IN ({','.join('?' for _ in wanted)})", wanted).fetchall()
        meta = {r["id"]: r for r in rows}
        similarity = {int(ids[i]): float(scores[i]) for i in order}
        matches = [ImageMatch(i, meta[i]["passage_id"], meta[i]["public_id"], meta[i]["local_page"], similarity[i])
                   for i in wanted if i in meta and (not suppliers or meta[i]["supplier"] in suppliers)]
        return matches[:top_k]

    def image_thumb(self, image_id: int) -> bytes | None:
        with self._conn() as conn:
            row = conn.execute("SELECT thumb FROM images WHERE id=?", (image_id,)).fetchone()
        return bytes(row["thumb"]) if row and row["thumb"] else None

    def thumbs_for_passages(self, passage_ids: Sequence[int]) -> dict[int, bytes]:
        """Une vignette (la première photo rattachée) par passage."""
        if not passage_ids:
            return {}
        with self._conn() as conn:
            rows = conn.execute(f"SELECT passage_id, thumb FROM images WHERE passage_id IN ({','.join('?' for _ in passage_ids)}) ORDER BY id",
                                list(passage_ids)).fetchall()
        result: dict[int, bytes] = {}
        for row in rows:
            result.setdefault(row["passage_id"], bytes(row["thumb"]))
        return result

    # -- sauvegarde / restauration ---------------------------------------------

    def copy_to(self, target: Path) -> None:
        """Copie cohérente de l'index (API de sauvegarde SQLite) pour l'envoyer sur Cloudinary."""
        with self._write_lock:
            source = sqlite3.connect(self.path, timeout=30)
            try:
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()

    def restore_from(self, source_path: Path) -> None:
        """Remplace le contenu de l'index par une sauvegarde (uniquement si le schéma correspond)."""
        probe = sqlite3.connect(source_path)
        try:
            row = probe.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or int(row[0]) != SCHEMA_VERSION:
                raise ValueError("schéma de sauvegarde incompatible")
            with self._write_lock:
                destination = sqlite3.connect(self.path, timeout=30)
                try:
                    probe.backup(destination)
                finally:
                    destination.close()
        finally:
            probe.close()
        with self._write_lock, self._conn() as conn:
            self._bump(conn)
        self._image_cache = None
