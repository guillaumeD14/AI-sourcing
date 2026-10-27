"""Synchronisation incrémentale Cloudinary -> index.

Pipeline : manifeste Cloudinary -> comparaison (version, taille, etag) -> téléchargement d'un seul PDF à la fois
-> extraction -> index. Les PDF découpés en parties sont regroupés sous leur nom logique et les numéros de page
d'origine sont reconstitués (intervalle lu dans le nom, sinon cumul des pages des parties précédentes).
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cloudinary_pdf_tools import CloudinaryClient, RemotePdf
from sourcing_errors import AuthError, PdfError, SourcingError
from sourcing_pdf_extract import extract_pdf
from sourcing_store import FileRecord, IndexStore
from sourcing_suppliers import SupplierDirectory

_RANGE = re.compile(r"^(?P<base>.+?)[_\-\s.]*p(?P<a>\d{1,5})[_\-\s.]*-[_\-\s.]*(?P<b>\d{1,5})$", re.I)
_PART = re.compile(r"^(?P<base>.+?)[_\-\s.]*(?:part(?:ie)?|pt)[_\-\s.]*(?P<i>\d{1,3})(?:[_\-\s.]*(?:of|sur|de|/)[_\-\s.]*(?P<n>\d{1,3}))?$", re.I)


@dataclass(frozen=True)
class PartInfo:
    logical_name: str
    part_index: int | None = None
    part_total: int | None = None
    first_page: int | None = None   # intervalle d'origine lu dans le nom, si présent


def parse_part_name(public_id: str) -> PartInfo:
    """``dossier/cat_part02of05_p0041-0080.pdf`` -> nom logique ``dossier/cat``, partie 2/5, pages 41 à 80."""
    stem = re.sub(r"\.pdf$", "", public_id, flags=re.I)
    folder, _, name = stem.rpartition("/")
    first_page = None
    match = _RANGE.match(name)
    if match:
        first_page, name = int(match["a"]), match["base"]
    part_index = part_total = None
    match = _PART.match(name)
    if match:
        part_index = int(match["i"])
        part_total = int(match["n"]) if match["n"] else None
        name = match["base"]
    logical = f"{folder}/{name}" if folder else name
    if part_index is None and first_page is not None:
        part_index = 0  # intervalle sans numéro de partie : l'ordre se déduit de la première page
    return PartInfo(logical, part_index, part_total, first_page)


def compute_offsets(files: list[FileRecord]) -> dict[str, int]:
    """Décalage de pages de chaque partie : ``page_originale = page_locale + décalage``.

    Priorité à l'intervalle inscrit dans le nom ; sinon cumul des pages des parties précédentes (ordre des numéros).
    """
    groups: dict[str, list[tuple[FileRecord, PartInfo]]] = {}
    for record in files:
        info = parse_part_name(record.public_id)
        groups.setdefault(info.logical_name, []).append((record, info))
    offsets: dict[str, int] = {}
    for members in groups.values():
        members.sort(key=lambda item: (item[1].first_page or 0, item[1].part_index or 0, item[0].public_id))
        running = 0
        for record, info in members:
            if info.first_page is not None:
                offsets[record.public_id] = info.first_page - 1
            else:
                offsets[record.public_id] = running
            running += record.page_count or 0
    return offsets


@dataclass
class SyncPlan:
    to_index: list[RemotePdf] = field(default_factory=list)
    to_remove: list[str] = field(default_factory=list)
    unchanged: int = 0


def plan_sync(store: IndexStore, remotes: list[RemotePdf], *, retry_errors: bool = False) -> SyncPlan:
    """Compare le manifeste Cloudinary à l'index : seuls les PDF nouveaux ou modifiés sont à traiter."""
    known = {f.public_id: f for f in store.list_files()}
    plan = SyncPlan()
    for remote in remotes:
        record = known.get(remote.public_id)
        changed = record is None or (record.version, record.size_bytes, record.etag or "") != (remote.version, remote.size_bytes, remote.etag)
        if changed or (retry_errors and record is not None and record.status == "error"):
            plan.to_index.append(remote)
        else:
            plan.unchanged += 1
    remote_ids = {r.public_id for r in remotes}
    plan.to_remove = [pid for pid in known if pid not in remote_ids]
    return plan


@dataclass
class SyncReport:
    indexed: int = 0
    needs_ocr: int = 0
    removed: int = 0
    unchanged: int = 0
    remaining: int = 0
    passages: int = 0
    images: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.indexed or self.needs_ocr or self.removed or self.errors)

    def summary(self) -> str:
        parts = [f"{self.indexed} PDF indexé(s)", f"{self.passages} passage(s)", f"{self.unchanged} inchangé(s)"]
        if self.needs_ocr:
            parts.append(f"{self.needs_ocr} scanné(s) (OCR requis)")
        if self.removed:
            parts.append(f"{self.removed} supprimé(s)")
        if self.remaining:
            parts.append(f"{self.remaining} restant(s) : relancez pour continuer")
        return " · ".join(parts)


def refresh_grouping(store: IndexStore, directory: SupplierDirectory | None = None) -> None:
    """Recalcule noms logiques, fournisseurs et décalages de pages de tous les fichiers."""
    files = store.list_files()
    offsets = compute_offsets(files)
    updates = []
    for record in files:
        info = parse_part_name(record.public_id)
        supplier = (directory.infer_from_filename(info.logical_name) if directory else record.supplier) or record.supplier
        row = (info.logical_name, supplier, info.part_index, info.part_total, offsets.get(record.public_id, 0))
        if row != (record.logical_name, record.supplier, record.part_index, record.part_total, record.page_offset):
            updates.append((record.public_id, *row))  # seuls les fichiers dont le regroupement change sont mis à jour
    if updates:
        store.update_grouping(updates)


def sync_index(
    store: IndexStore, client: CloudinaryClient, remotes: list[RemotePdf], *, max_files: int | None = None,
    directory: SupplierDirectory | None = None, retry_errors: bool = False, with_images: bool = True, ocr: bool = False,
    ocr_languages: str = "fra+eng", progress: Callable[[int, int, str], None] | None = None,
    extractor: Callable[..., Any] = extract_pdf,
) -> SyncReport:
    """Indexe au plus ``max_files`` PDF. Une erreur sur un PDF est consignée sans arrêter les autres."""
    plan = plan_sync(store, remotes, retry_errors=retry_errors)
    report = SyncReport(unchanged=plan.unchanged)
    store.remove_files(plan.to_remove)
    report.removed = len(plan.to_remove)
    batch = plan.to_index[:max_files] if max_files else plan.to_index
    report.remaining = len(plan.to_index) - len(batch)
    for position, remote in enumerate(batch, start=1):
        if progress:
            progress(position - 1, len(batch), remote.public_id)
        info = parse_part_name(remote.public_id)
        record = FileRecord(
            public_id=remote.public_id, logical_name=info.logical_name, part_index=info.part_index, part_total=info.part_total,
            supplier=(directory.infer_from_filename(info.logical_name) if directory else ""), version=remote.version,
            size_bytes=remote.size_bytes, etag=remote.etag, resource_type=remote.resource_type, secure_url=remote.secure_url)
        try:
            _index_one(store, client, remote, record, extractor, report, with_images, ocr, ocr_languages)
        except AuthError:
            raise  # identifiants / livraison PDF refusée : inutile d'insister sur les fichiers suivants
        except (SourcingError, OSError) as exc:
            record.status, record.status_detail = "error", str(exc)[:300]
            store.save_file_status(record)
            report.errors.append((remote.public_id, str(exc)))
    if progress:
        progress(len(batch), len(batch), "")
    refresh_grouping(store, directory)
    return report


def _index_one(store: IndexStore, client: CloudinaryClient, remote: RemotePdf, record: FileRecord, extractor: Callable[..., Any],
               report: SyncReport, with_images: bool, ocr: bool, ocr_languages: str) -> None:
    with tempfile.TemporaryDirectory(prefix="index_") as tmp:  # le PDF est supprimé dès l'extraction terminée
        path = Path(tmp) / "part.pdf"
        client.download_to_file(remote, path)
        try:
            result = extractor(path, with_images=with_images, ocr=ocr, ocr_languages=ocr_languages)
        except PdfError as exc:
            raise SourcingError(str(exc)) from None
    record.page_count = result.page_count
    if result.needs_ocr:
        record.status, record.status_detail = "needs_ocr", "PDF scanné : aucun texte exploitable (activez l'OCR ou fournissez un PDF texte)."
        store.save_file_status(record)
        report.needs_ocr += 1
        return
    record.status = "indexed"
    record.status_detail = "; ".join(result.warnings)[:300]
    store.replace_file(record, result.passages, result.images)
    report.indexed += 1
    report.passages += len(result.passages)
    report.images += len(result.images)
