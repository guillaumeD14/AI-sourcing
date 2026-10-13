"""Extraction PDF → blocs produits → index persistant, de façon incrémentale.

Pipeline :  Cloudinary → manifeste → téléchargement à la demande → extraction PDF →
blocs produits → index SQLite → recherche classée.

Un seul PDF est présent sur disque et en mémoire à la fois ; il est supprimé dès son
indexation. Les PDF dont l'empreinte n'a pas changé ne sont jamais retéléchargés.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Protocol, Sequence

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from sourcing_blocks import Block, build_blocks
from sourcing_catalog_names import parse_catalog_name
from sourcing_errors import SourcingError
from sourcing_store import STATUS_ERROR, STATUS_INDEXED, STATUS_NEEDS_OCR, IndexStore

if TYPE_CHECKING:
    from sourcing_suppliers import SupplierDirectory

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS_PER_PAGE = 25
OCR_EMPTY_PAGE_RATIO = 0.8


class RemoteLike(Protocol):
    public_id: str
    version: str
    bytes: int
    etag: str
    resource_type: str
    secure_url: str

    @property
    def fingerprint(self) -> str: ...


class ClientLike(Protocol):
    def download_to_file(self, remote: RemoteLike, dest: str | Path) -> int: ...


@dataclass
class SyncPlan:
    to_index: list[RemoteLike] = field(default_factory=list)
    unchanged: int = 0
    to_remove: list[str] = field(default_factory=list)


@dataclass
class SyncReport:
    indexed: int = 0
    needs_ocr: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    passages: int = 0

    def summary(self) -> str:
        parts = [f"{self.indexed} PDF indexé(s)", f"{self.unchanged} inchangé(s)"]
        if self.needs_ocr:
            parts.append(f"{self.needs_ocr} sans texte (OCR requis)")
        if self.removed:
            parts.append(f"{self.removed} retiré(s)")
        if self.errors:
            parts.append(f"{len(self.errors)} erreur(s)")
        return ", ".join(parts) + f" — {self.passages} passages ajoutés."


def plan_sync(store: IndexStore, remotes: Sequence[RemoteLike], *, retry_errors: bool = True) -> SyncPlan:
    """Compare la liste Cloudinary à l'index : seuls les PDF nouveaux ou modifiés sont à traiter."""
    known = store.fingerprints()
    plan = SyncPlan()
    remote_ids = set()
    for remote in remotes:
        remote_ids.add(remote.public_id)
        entry = known.get(remote.public_id)
        if entry is None or entry[0] != remote.fingerprint or (retry_errors and entry[1] == STATUS_ERROR):
            plan.to_index.append(remote)
        else:
            plan.unchanged += 1
    plan.to_remove = [public_id for public_id in known if public_id not in remote_ids]
    plan.to_index.sort(key=lambda r: (parse_catalog_name(r.public_id).logical_key,
                                      parse_catalog_name(r.public_id).part_index))
    return plan


def extract_pages(path: str | Path) -> Iterator[tuple[int, str | None, str | None]]:
    """Génère (numéro de page local 1-based, texte | None, erreur | None), page par page.

    Utilise PyMuPDF s'il est installé (rapide, déjà utilisé par l'application d'origine), sinon pypdf.
    """
    import sourcing_pdf_render as pdf_render

    if pdf_render.preferred_engine() == "fitz":
        yield from pdf_render.extract_pages_fitz(path)
        return
    yield from _extract_pages_pypdf(path)


def _extract_pages_pypdf(path: str | Path) -> Iterator[tuple[int, str | None, str | None]]:
    with Path(path).open("rb") as handle:
        reader = PdfReader(handle)
        if reader.is_encrypted and not reader.decrypt(""):
            raise SourcingError("PDF protégé par mot de passe : texte inaccessible.")
        for number, page in enumerate(reader.pages, start=1):
            try:
                yield number, page.extract_text() or "", None
            except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                yield number, None, type(exc).__name__


def index_pdf_file(
    store: IndexStore, remote: RemoteLike, pdf_path: str | Path, *, extractor: Callable = extract_pages,
    directory: "SupplierDirectory | None" = None,
) -> tuple[str, int]:
    """Indexe un PDF local. Retourne (statut, passages). Ne lève pas pour un PDF scanné."""
    ref = parse_catalog_name(remote.public_id, directory)
    pages_blocks: list[tuple[int, list[Block]]] = []
    empty_pages = total_pages = 0
    page_errors = 0
    for number, text, error in extractor(pdf_path):
        total_pages += 1
        if error:
            page_errors += 1
            empty_pages += 1
            continue
        if len((text or "").strip()) < MIN_TEXT_CHARS_PER_PAGE:
            empty_pages += 1
        pages_blocks.append((number, build_blocks(text or "")))
    needs_ocr = total_pages > 0 and empty_pages / total_pages >= OCR_EMPTY_PAGE_RATIO
    status = STATUS_NEEDS_OCR if needs_ocr else STATUS_INDEXED
    detail = None
    if needs_ocr:
        detail = f"{empty_pages}/{total_pages} pages sans texte extractible : PDF scanné, OCR requis."
    elif page_errors:
        detail = f"{page_errors} page(s) illisible(s) ignorée(s)."
    inserted = store.replace_file(
        ref,
        fingerprint=remote.fingerprint, version=remote.version, size=remote.bytes, etag=remote.etag,
        secure_url=remote.secure_url, resource_type=remote.resource_type, page_count=total_pages,
        status=status, status_detail=detail, pages_blocks=pages_blocks,
    )
    return status, inserted


def _record_error(store: IndexStore, remote: RemoteLike, message: str, directory: "SupplierDirectory | None" = None) -> None:
    store.replace_file(
        parse_catalog_name(remote.public_id, directory),
        fingerprint=remote.fingerprint, version=remote.version, size=remote.bytes, etag=remote.etag,
        secure_url=remote.secure_url, resource_type=remote.resource_type, page_count=0,
        status=STATUS_ERROR, status_detail=message[:300], pages_blocks=[],
    )


def sync_index(
    store: IndexStore,
    client: ClientLike,
    remotes: Sequence[RemoteLike],
    *,
    max_files: int | None = None,
    retry_errors: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
    work_dir: str | Path | None = None,
    directory: "SupplierDirectory | None" = None,
) -> SyncReport:
    """Met l'index à jour : télécharge puis indexe uniquement les PDF nouveaux ou modifiés.

    Une erreur sur un PDF est enregistrée (statut ``error``) et n'interrompt pas les autres.
    """
    plan = plan_sync(store, remotes, retry_errors=retry_errors)
    report = SyncReport(unchanged=plan.unchanged)
    if plan.to_remove:
        store.remove_files(plan.to_remove)
        report.removed = len(plan.to_remove)
    queue = plan.to_index[:max_files] if max_files else plan.to_index
    with tempfile.TemporaryDirectory(prefix="sourcing_", dir=str(work_dir) if work_dir else None) as tmp:
        for position, remote in enumerate(queue, start=1):
            if progress:
                progress(position - 1, len(queue), remote.public_id)
            target = Path(tmp) / "current.pdf"
            try:
                client.download_to_file(remote, target)
                status, inserted = index_pdf_file(store, remote, target, directory=directory)
            except SourcingError as exc:
                report.errors.append((remote.public_id, str(exc)))
                _record_error(store, remote, str(exc), directory)
            except (PyPdfError, OSError, ValueError) as exc:
                message = f"PDF illisible ({type(exc).__name__})."
                report.errors.append((remote.public_id, message))
                _record_error(store, remote, message, directory)
            else:
                report.passages += inserted
                if status == STATUS_NEEDS_OCR:
                    report.needs_ocr += 1
                else:
                    report.indexed += 1
            finally:
                target.unlink(missing_ok=True)
    if progress:
        progress(len(queue), len(queue), "terminé")
    store.resolve_offsets()
    return report
