"""Persistance SQLite de l'index (fichiers, passages, recherche plein texte FTS5).

- Un enregistrement ``files`` par PDF Cloudinary (partie incluse) avec son empreinte
  (public_id, version, taille, etag) : un PDF n'est ré-indexé que si l'empreinte change.
- Un enregistrement ``passages`` par bloc produit / passage court.
- ``meta.index_version`` s'incrémente à chaque modification : il sert de clé de cache
  pour la recherche (aucun recalcul tant que l'index est inchangé).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from sourcing_blocks import Block
from sourcing_catalog_names import CatalogRef

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  public_id TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  version TEXT, bytes INTEGER, etag TEXT, secure_url TEXT, resource_type TEXT,
  logical_key TEXT NOT NULL, logical_name TEXT NOT NULL, display_name TEXT NOT NULL, supplier TEXT NOT NULL,
  part_index INTEGER NOT NULL DEFAULT 1, part_total INTEGER NOT NULL DEFAULT 1,
  start_page INTEGER, page_offset INTEGER, page_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, status_detail TEXT, passages INTEGER NOT NULL DEFAULT 0, indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_logical ON files(logical_key, part_index);
CREATE TABLE IF NOT EXISTS passages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  logical_key TEXT NOT NULL,
  local_page INTEGER NOT NULL,
  text TEXT NOT NULL, norm_text TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', title_norm TEXT NOT NULL DEFAULT '',
  refs TEXT NOT NULL DEFAULT '[]', weights TEXT NOT NULL DEFAULT '[]', packaging TEXT NOT NULL DEFAULT '[]',
  structured INTEGER NOT NULL DEFAULT 0, boilerplate INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passages_file ON passages(file_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_passages_dedup ON passages(logical_key, content_hash);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
  norm_text, title_norm, content='passages', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
  INSERT INTO passages_fts(rowid, norm_text, title_norm) VALUES (new.id, new.norm_text, new.title_norm);
END;
CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
  INSERT INTO passages_fts(passages_fts, rowid, norm_text, title_norm)
  VALUES ('delete', old.id, old.norm_text, old.title_norm);
END;
"""

STATUS_INDEXED = "indexed"
STATUS_NEEDS_OCR = "needs_ocr"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class FileRecord:
    id: int
    public_id: str
    fingerprint: str
    logical_key: str
    logical_name: str
    display_name: str
    supplier: str
    part_index: int
    part_total: int
    page_offset: int | None
    page_count: int
    status: str
    status_detail: str | None
    passages: int
    secure_url: str | None
    resource_type: str | None
    version: str | None = None
    size_bytes: int | None = None
    etag: str | None = None


@dataclass(frozen=True)
class IndexStats:
    files: int
    indexed: int
    needs_ocr: int
    errors: int
    passages: int
    suppliers: int
    version: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IndexStore:
    """Accès SQLite : une connexion courte par opération (sûr avec les threads Streamlit)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            self.fts_available = self._try_create_fts(con)
            con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('index_version', '0')")

    @staticmethod
    def _try_create_fts(con: sqlite3.Connection) -> bool:
        try:
            con.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False  # SQLite sans FTS5 : la recherche bascule sur le mode de repli

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def connect(self) -> "contextmanager[sqlite3.Connection]":  # type: ignore[valid-type]
        """Connexion courte (gestionnaire de contexte) pour les modules de recherche."""
        return self._connect()

    # -- métadonnées ------------------------------------------------------

    def version(self) -> int:
        with self._connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key='index_version'").fetchone()
        return int(row["value"]) if row else 0

    def _bump(self, con: sqlite3.Connection) -> None:
        con.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='index_version'")

    # -- fichiers ---------------------------------------------------------

    def fingerprints(self) -> dict[str, tuple[str, str]]:
        """public_id -> (empreinte, statut)."""
        with self._connect() as con:
            rows = con.execute("SELECT public_id, fingerprint, status FROM files").fetchall()
        return {r["public_id"]: (r["fingerprint"], r["status"]) for r in rows}

    def list_files(self) -> list[FileRecord]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM files ORDER BY supplier, logical_name, part_index").fetchall()
        return [self._record(r) for r in rows]

    @staticmethod
    def _record(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=row["id"], public_id=row["public_id"], fingerprint=row["fingerprint"],
            logical_key=row["logical_key"], logical_name=row["logical_name"], display_name=row["display_name"],
            supplier=row["supplier"], part_index=row["part_index"], part_total=row["part_total"],
            page_offset=row["page_offset"], page_count=row["page_count"], status=row["status"],
            status_detail=row["status_detail"], passages=row["passages"],
            secure_url=row["secure_url"], resource_type=row["resource_type"],
            version=row["version"], size_bytes=row["bytes"], etag=row["etag"],
        )

    def get_file(self, public_id: str) -> FileRecord | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM files WHERE public_id=?", (public_id,)).fetchone()
        return self._record(row) if row else None

    def replace_file(
        self,
        ref: CatalogRef,
        *,
        fingerprint: str,
        version: str | None,
        size: int | None,
        etag: str | None,
        secure_url: str | None,
        resource_type: str | None,
        page_count: int,
        status: str,
        status_detail: str | None,
        pages_blocks: Sequence[tuple[int, Sequence[Block]]],
    ) -> int:
        """Remplace atomiquement un fichier et ses passages. Retourne le nombre de passages insérés."""
        inserted = 0
        with self._connect() as con:
            con.execute(
                "DELETE FROM passages WHERE file_id IN (SELECT id FROM files WHERE public_id=?)", (ref.public_id,)
            )
            con.execute("DELETE FROM files WHERE public_id=?", (ref.public_id,))
            cursor = con.execute(
                """INSERT INTO files(public_id, fingerprint, version, bytes, etag, secure_url, resource_type,
                   logical_key, logical_name, display_name, supplier, part_index, part_total, start_page,
                   page_offset, page_count, status, status_detail, passages, indexed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                (
                    ref.public_id, fingerprint, version, size, etag, secure_url, resource_type,
                    ref.logical_key, ref.logical_name, ref.display_name, ref.supplier, ref.part_index,
                    ref.part_total, ref.start_page, ref.page_offset, page_count, status, status_detail, _now(),
                ),
            )
            file_id = int(cursor.lastrowid or 0)
            for local_page, blocks in pages_blocks:
                for block in blocks:
                    cur = con.execute(
                        """INSERT OR IGNORE INTO passages(file_id, logical_key, local_page, text, norm_text, title,
                           title_norm, refs, weights, packaging, structured, boilerplate, content_hash)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            file_id, ref.logical_key, local_page, block.text, block.norm_text, block.title,
                            _norm(block.title), json.dumps(block.refs), json.dumps(block.weights_g),
                            json.dumps(block.packaging), int(block.structured), int(block.boilerplate),
                            block.content_hash,
                        ),
                    )
                    inserted += cur.rowcount
            con.execute("UPDATE files SET passages=? WHERE id=?", (inserted, file_id))
            self._bump(con)
        self.resolve_offsets()
        return inserted

    def remove_files(self, public_ids: Sequence[str]) -> None:
        if not public_ids:
            return
        with self._connect() as con:
            for public_id in public_ids:
                con.execute(
                    "DELETE FROM passages WHERE file_id IN (SELECT id FROM files WHERE public_id=?)", (public_id,)
                )
                con.execute("DELETE FROM files WHERE public_id=?", (public_id,))
            self._bump(con)

    def resolve_offsets(self) -> None:
        """Calcule le décalage de page des parties dont le nom n'encode pas la page de départ.

        Décalage = somme des pages des parties précédentes du même catalogue. Inconnu (NULL) tant
        qu'une partie précédente n'est pas indexée.
        """
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, logical_key, part_index, start_page, page_count, status FROM files "
                "ORDER BY logical_key, part_index"
            ).fetchall()
            running: dict[str, int | None] = {}
            last_index: dict[str, int] = {}
            for row in rows:
                key, index = row["logical_key"], row["part_index"]
                if key not in running:
                    running[key], last_index[key] = 0, 0
                contiguous = index == last_index[key] + 1
                if row["start_page"] is not None:
                    offset: int | None = row["start_page"] - 1
                elif contiguous or index == 1:
                    offset = running[key]
                else:
                    offset = None  # partie précédente absente de l'index : numéro d'origine inconnu
                con.execute("UPDATE files SET page_offset=? WHERE id=?", (offset, row["id"]))
                if offset is not None and row["status"] != STATUS_ERROR:
                    running[key] = offset + row["page_count"]
                else:
                    running[key] = None
                last_index[key] = index
            con.execute(
                "UPDATE files SET part_total = MAX(part_total, "
                "(SELECT COUNT(*) FROM files f2 WHERE f2.logical_key = files.logical_key))"
            )

    # -- statistiques et filtres -----------------------------------------

    def stats(self) -> IndexStats:
        with self._connect() as con:
            row = con.execute(
                """SELECT COUNT(*) AS files,
                   SUM(status='indexed') AS indexed, SUM(status='needs_ocr') AS ocr, SUM(status='error') AS errors,
                   COALESCE(SUM(passages),0) AS passages, COUNT(DISTINCT supplier) AS suppliers FROM files"""
            ).fetchone()
        return IndexStats(
            files=row["files"] or 0, indexed=row["indexed"] or 0, needs_ocr=row["ocr"] or 0,
            errors=row["errors"] or 0, passages=row["passages"] or 0, suppliers=row["suppliers"] or 0,
            version=self.version(),
        )

    def suppliers(self) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT supplier FROM files WHERE status='indexed' ORDER BY supplier COLLATE NOCASE"
            ).fetchall()
        return [r["supplier"] for r in rows]


def _norm(text: str) -> str:
    from sourcing_textutils import normalize

    return normalize(text)
