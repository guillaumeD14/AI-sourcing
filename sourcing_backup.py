"""Persistance sur Cloudinary : le disque de Streamlit Community Cloud est effacé à chaque redémarrage.

- L'index SQLite est compressé (gzip), découpé en parties < 8 Mo (limite gratuite 10 Mo) et restauré au démarrage,
  ce qui évite de re-télécharger et ré-analyser tous les catalogues.
- La shortlist (décisions de l'équipe) est un petit JSON partagé par toutes les personnes ayant le lien de l'application.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from cloudinary_pdf_tools import CloudinaryClient
from sourcing_errors import SourcingError
from sourcing_shortlist import decisions_from_json, decisions_to_json
from sourcing_store import SCHEMA_VERSION, IndexStore, utc_now

STATE_DIR = "_sourcing_state"
PART_BYTES = 8 * 1024 * 1024


class CloudinaryState:
    def __init__(self, client: CloudinaryClient, prefix: str = "") -> None:
        self.client = client
        self.prefix = prefix.strip("/ ")

    def _id(self, name: str) -> str:
        return f"{self.prefix}/{STATE_DIR}/{name}" if self.prefix else f"{STATE_DIR}/{name}"

    # -- shortlist ---------------------------------------------------------------

    def load_shortlist(self) -> dict[str, tuple[str, dict[str, str]]]:
        raw = self.client.download_bytes(self._id("shortlist.json"))
        return decisions_from_json(raw) if raw else {}

    def save_shortlist(self, decisions: dict[str, tuple[str, dict[str, str]]]) -> None:
        self.client.upload_bytes(decisions_to_json(decisions), self._id("shortlist.json"))

    # -- index ----------------------------------------------------------------------

    def save_index(self, store: IndexStore) -> str:
        """Envoie une copie compressée de l'index. Retourne un résumé lisible."""
        with tempfile.TemporaryDirectory(prefix="idxsave_") as tmp:
            snapshot, archive = Path(tmp) / "index.sqlite", Path(tmp) / "index.sqlite.gz"
            store.copy_to(snapshot)
            with snapshot.open("rb") as src, gzip.open(archive, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            snapshot.unlink()
            digest, count, total = hashlib.sha256(), 0, archive.stat().st_size
            with archive.open("rb") as handle:
                while chunk := handle.read(PART_BYTES):
                    count += 1
                    digest.update(chunk)
                    self.client.upload_bytes(chunk, self._id(f"index_part{count:03d}.dat"))
            manifest = {"created_at": utc_now(), "parts": count, "sha256": digest.hexdigest(), "schema": SCHEMA_VERSION, "bytes": total}
            self.client.upload_bytes(json.dumps(manifest).encode(), self._id("index_manifest.json"))
        return f"Index sauvegardé sur Cloudinary ({count} partie(s), {total / 1e6:.1f} Mo)."

    def restore_index(self, store: IndexStore) -> bool:
        """Restaure l'index sauvegardé. ``False`` s'il n'existe pas ou n'est plus compatible (reconstruction normale)."""
        raw = self.client.download_bytes(self._id("index_manifest.json"))
        if not raw:
            return False
        try:
            manifest: dict[str, Any] = json.loads(raw)
            if int(manifest["schema"]) != SCHEMA_VERSION:
                return False
            parts = int(manifest["parts"])
        except (ValueError, KeyError, TypeError):
            return False
        with tempfile.TemporaryDirectory(prefix="idxrestore_") as tmp:
            archive, database = Path(tmp) / "index.sqlite.gz", Path(tmp) / "index.sqlite"
            digest = hashlib.sha256()
            with archive.open("wb") as handle:
                for number in range(1, parts + 1):
                    chunk = self.client.download_bytes(self._id(f"index_part{number:03d}.dat"), max_bytes=PART_BYTES + 1024 * 1024)
                    if chunk is None:
                        raise SourcingError("Sauvegarde d'index incomplète sur Cloudinary : une synchronisation complète sera faite.")
                    digest.update(chunk)
                    handle.write(chunk)
            if digest.hexdigest() != manifest.get("sha256"):
                raise SourcingError("Sauvegarde d'index corrompue (empreinte différente) : une synchronisation complète sera faite.")
            with gzip.open(archive, "rb") as src, database.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            try:
                store.restore_from(database)
            except ValueError:
                return False
        return True
