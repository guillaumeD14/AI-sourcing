"""Fonctions facultatives reposant sur PyMuPDF (``fitz``), déjà utilisé par l'application d'origine.

Sans PyMuPDF, l'application fonctionne avec ``pypdf`` : seuls l'aperçu de page, l'optimisation
avant envoi et la recompression d'une page seule trop lourde sont indisponibles.
Les appels PyMuPDF sont ceux de l'application d'origine (``fitz.open``, ``get_pixmap``,
``get_text("text")``, ``tobytes``).
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, Iterator

from sourcing_errors import SourcingError


def _fitz() -> Any | None:
    try:
        return importlib.import_module("fitz")
    except ImportError:
        return None


def fitz_available() -> bool:
    return _fitz() is not None


def preferred_engine() -> str:
    """``fitz`` si installé (plus rapide), sauf si SOURCING_PDF_ENGINE=pypdf."""
    if os.environ.get("SOURCING_PDF_ENGINE", "").lower() == "pypdf":
        return "pypdf"
    return "fitz" if fitz_available() else "pypdf"


def extract_pages_fitz(path: str | Path) -> Iterator[tuple[int, str | None, str | None]]:
    """Mêmes sorties que ``indexer.extract_pages`` : (page 1-based, texte | None, erreur | None)."""
    fitz = _fitz()
    if fitz is None:
        raise SourcingError("PyMuPDF n'est pas installé.")
    document = fitz.open(str(path))
    try:
        for number in range(document.page_count):
            try:
                yield number + 1, document.load_page(number).get_text("text") or "", None
            except Exception as exc:  # noqa: BLE001 - une page illisible ne doit pas arrêter le PDF
                yield number + 1, None, type(exc).__name__
    finally:
        document.close()


def render_page_png(path: str | Path, page_number: int, zoom: float = 1.1) -> bytes:
    """Aperçu PNG d'une page (1-based) d'un PDF local."""
    fitz = _fitz()
    if fitz is None:
        raise SourcingError("Aperçu indisponible : PyMuPDF n'est pas installé.")
    document = fitz.open(str(path))
    try:
        if not 1 <= page_number <= document.page_count:
            raise SourcingError(f"Page {page_number} absente de ce PDF ({document.page_count} pages).")
        pixmap = document.load_page(page_number - 1).get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pixmap.tobytes("png")
    finally:
        document.close()


def optimize_pdf(source: str | Path, target: str | Path) -> bool:
    """Réécriture compacte (``garbage=4, deflate``). Retourne False si indisponible ou en échec."""
    fitz = _fitz()
    if fitz is None:
        return False
    try:
        document = fitz.open(str(source))
        try:
            document.save(str(target), garbage=4, deflate=True, clean=True)
        finally:
            document.close()
        return Path(target).exists() and Path(target).stat().st_size > 0
    except Exception:  # noqa: BLE001 - l'optimisation est facultative : on garde le PDF d'origine
        return False


def rasterize_page_pdf(source: str | Path, page_index: int, max_bytes: int) -> bytes | None:
    """Page seule trop lourde -> PDF d'une image JPEG recompressée sous ``max_bytes`` (texte perdu)."""
    fitz = _fitz()
    if fitz is None:
        return None
    document = fitz.open(str(source))
    try:
        page = document.load_page(page_index)
        for zoom in (1.6, 1.3, 1.0, 0.8, 0.6):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            for quality in (80, 60, 40):
                jpeg = pixmap.tobytes("jpeg", jpg_quality=quality)
                output = fitz.open()
                try:
                    new_page = output.new_page(width=page.rect.width, height=page.rect.height)
                    new_page.insert_image(new_page.rect, stream=jpeg)
                    data = output.tobytes(garbage=4, deflate=True)
                finally:
                    output.close()
                if len(data) <= max_bytes:
                    return data
        return None
    finally:
        document.close()
