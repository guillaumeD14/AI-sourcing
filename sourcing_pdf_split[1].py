"""Découpage rapide d'un gros PDF en parties de moins de 9 Mo (limite gratuite Cloudinary : 10 Mo).

Principe : pour chaque partie, on estime le nombre de pages tenant dans la limite à partir
du poids moyen par page, puis on affine par recherche dichotomique en mesurant la taille
réelle du PDF écrit. Le PDF source n'est jamais chargé plus d'une fois et chaque partie
est écrite sur disque immédiatement (mémoire bornée à une partie en cours).
"""

from __future__ import annotations

import io
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

from sourcing_catalog_names import part_filename, safe_stem
from sourcing_errors import SourcingError

MAX_PART_BYTES = 9 * 1024 * 1024
_SAFETY = 0.97


class PdfSplitError(SourcingError):
    """Le PDF ne peut pas être découpé."""


class PageTooLargeError(PdfSplitError):
    """Une seule page dépasse la limite : impossible de respecter la taille maximale."""


@dataclass(frozen=True)
class SplitPart:
    path: Path
    index: int
    total: int
    start_page: int  # 1-based, dans le PDF d'origine
    end_page: int
    size_bytes: int

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1


def _serialize(reader: PdfReader, start: int, count: int) -> bytes:
    writer = PdfWriter()
    for page_index in range(start, start + count):
        writer.add_page(reader.pages[page_index])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _fit_pages(reader: PdfReader, start: int, remaining: int, guess: int, limit: int) -> tuple[int, bytes]:
    """Plus grand nombre de pages (depuis ``start``) dont le PDF reste ≤ ``limit`` octets."""
    best_count, best_data = 0, b""
    too_big = remaining + 1
    count = max(1, min(guess, remaining))
    measured: dict[int, int] = {}
    while best_count + 1 < too_big:
        data = _serialize(reader, start, count)
        measured[count] = len(data)
        if len(data) <= limit:
            if count > best_count:
                best_count, best_data = count, data
            proposal = max(count + 1, int(count * limit * _SAFETY / max(len(data), 1)))
        else:
            too_big = min(too_big, count)
            proposal = min(count - 1, int(count * limit * _SAFETY / len(data)))
        count = max(best_count + 1, min(proposal, too_big - 1))
        if count in measured:  # sécurité anti-boucle
            break
    return best_count, best_data


def split_pdf(
    source: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    stem: str | None = None,
    max_bytes: int = MAX_PART_BYTES,
    progress: Callable[[int, int], None] | None = None,
) -> list[SplitPart]:
    """Découpe ``source`` en parties ≤ ``max_bytes``. Retourne les parties, dans l'ordre.

    Les parties sont nommées ``<stem>_part_01_pages_1-45.pdf`` (convention de l'application d'origine),
    y compris lorsque le fichier tient déjà dans la limite (copié tel quel, une seule partie).
    Une page seule trop lourde est rasterisée si PyMuPDF est installé, sinon l'erreur est explicite.
    """
    source_path = Path(source)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    base = safe_stem(stem or source_path.stem)
    size = source_path.stat().st_size
    try:
        with source_path.open("rb") as handle:
            reader = PdfReader(handle)
            if reader.is_encrypted and not reader.decrypt(""):
                raise PdfSplitError("Ce PDF est protégé par un mot de passe : retirez la protection avant l'envoi.")
            total_pages = len(reader.pages)
            if total_pages == 0:
                raise PdfSplitError("Ce PDF ne contient aucune page.")
            if size <= max_bytes:
                target = out_path / part_filename(base, 1, 1, total_pages)
                shutil.copyfile(source_path, target)
                return [SplitPart(target, 1, 1, 1, total_pages, size)]
            return _split_reader(reader, source_path, out_path, base, size, total_pages, max_bytes, progress)
    except PageTooLargeError:
        raise
    except PdfSplitError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError) as exc:
        raise PdfSplitError(f"PDF illisible ou corrompu ({type(exc).__name__}).") from exc


def _split_reader(
    reader: PdfReader,
    source_path: Path,
    out_path: Path,
    base: str,
    source_size: int,
    total_pages: int,
    max_bytes: int,
    progress: Callable[[int, int], None] | None,
) -> list[SplitPart]:
    bytes_per_page = source_size / total_pages
    drafts: list[tuple[Path, int, int, int]] = []  # (chemin temporaire, début, fin, taille)
    start = 0
    while start < total_pages:
        remaining = total_pages - start
        guess = int(max_bytes * 0.9 / max(bytes_per_page, 1))
        count, data = _fit_pages(reader, start, remaining, guess, max_bytes)
        if count == 0:
            data = _rasterized_page(source_path, start, max_bytes)
            count = 1
        temp = out_path / f".part_{len(drafts):03d}.tmp"
        temp.write_bytes(data)
        drafts.append((temp, start + 1, start + count, len(data)))
        bytes_per_page = len(data) / count
        start += count
        if progress:
            progress(start, total_pages)
    parts: list[SplitPart] = []
    total = len(drafts)
    for index, (temp, first, last, size) in enumerate(drafts, start=1):
        final = out_path / part_filename(base, index, first, last)
        os.replace(temp, final)
        parts.append(SplitPart(final, index, total, first, last, size))
    return parts


def _rasterized_page(source_path: Path, page_index: int, max_bytes: int) -> bytes:
    """Dernier recours pour une page seule trop lourde : image JPEG recompressée (texte non extractible)."""
    import sourcing_pdf_render as pdf_render

    if not pdf_render.fitz_available():
        raise PageTooLargeError(
            f"La page {page_index + 1} pèse à elle seule plus de {max_bytes // (1024 * 1024)} Mo : compressez ce PDF "
            "(images) avant l'envoi, ou installez PyMuPDF pour qu'elle soit recompressée automatiquement."
        )
    data = pdf_render.rasterize_page_pdf(source_path, page_index, max_bytes)
    if data is None:
        raise PageTooLargeError(
            f"La page {page_index + 1} reste trop lourde même recompressée : compressez ce PDF avant l'envoi."
        )
    return data
