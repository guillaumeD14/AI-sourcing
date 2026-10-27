"""Extraction d'un PDF en passages courts (blocs produits) et en photos produit.

La segmentation (``segment_page``) est pure et testable sans PyMuPDF. Seule ``extract_pdf`` en dépend.
Un PDF est lu page par page : jamais plus d'un PDF ouvert, jamais la bibliothèque entière en mémoire.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sourcing_errors import PdfError
from sourcing_images import ImageDescriptor, compute_descriptor, is_usable_product_photo, load_image
from sourcing_textutils import content_hash, extract_packaging, extract_refs, extract_weights, normalize

MAX_UNIT_CHARS = 700
CHUNK_TARGET = 450
MIN_ALNUM = 12
GAP_NEW_UNIT = 22.0   # points PDF : au-delà, un bloc suivant des données produit ouvre un nouveau passage
TEXT_THRESHOLD = 30  # moins de caractères sur la page : on la considère sans texte (scan)


@dataclass(frozen=True)
class PageBlock:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class ExtractedPassage:
    local_page: int
    kind: str                      # "product" (données produit) ou "text"
    title: str
    text: str
    refs: list[str]
    weights_g: list[float]
    unit_weights_g: list[float]
    packaging: list[str]
    structured: bool
    bbox: tuple[float, float, float, float] | None
    content_hash: str


@dataclass
class ExtractedImage:
    local_page: int
    bbox: tuple[float, float, float, float]
    descriptor: ImageDescriptor
    xref: int
    passage_index: int | None = None


@dataclass
class ExtractionResult:
    page_count: int
    passages: list[ExtractedPassage] = field(default_factory=list)
    images: list[ExtractedImage] = field(default_factory=list)
    pages_without_text: int = 0
    ocr_used: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_ocr(self) -> bool:
        """Vrai si le PDF est majoritairement un scan sans texte exploitable."""
        return self.page_count > 0 and not self.passages and self.pages_without_text >= max(1, self.page_count // 2)


# ---------------------------------------------------------------------------
# Segmentation (pure)
# ---------------------------------------------------------------------------

_PAGE_NUMBER = re.compile(r"^(?:page|p\.?|seite|pagina)?\s*[-–]?\s*\d{1,4}\s*(?:/\s*\d{1,4})?\s*[-–]?$", re.I)
_BULLET = re.compile(r"^\s*[•\-–·*▪●]\s")
_NOT_TITLE = re.compile(
    r"^(carton|colis|caisse|box|karton|sachet|plateau|barquette|palette|pallet|packaging|conditionnement|colisage|dlc|dluo|ddm|ean|gtin|"
    r"poids|weight|gewicht|r[eé]f|art|code|ingr[eé]dients?|zutaten|allerg|stockage|storage|valeurs|nutrition|dimensions?|pcb|uvc)\b", re.I)


def clean_text(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u00ad", "")
    text = re.sub(r"-\n(?=[a-zà-ÿ])", "", text)  # mot coupé en fin de ligne
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _alnum(text: str) -> int:
    return sum(c.isalnum() for c in text)


def _is_noise(text: str) -> bool:
    flat = text.replace("\n", " ").strip()
    return _alnum(flat) < 3 or bool(_PAGE_NUMBER.match(flat))


def looks_like_title(line: str) -> bool:
    line = line.strip()
    letters = sum(c.isalpha() for c in line)
    if not (3 <= len(line) <= 90) or letters < 3 or _BULLET.match(line) or _NOT_TITLE.match(line) or line.endswith((".", ";", ",")):
        return False
    if letters / max(len(line), 1) < 0.55:
        return False
    return line.isupper() or (line[0].isupper() and len(line.split()) <= 10)


def _has_data(text: str) -> bool:
    weights, _ = extract_weights(text)
    return bool(weights or extract_refs(text) or extract_packaging(text))


def _split_long(block: PageBlock) -> list[PageBlock]:
    if len(block.text) <= MAX_UNIT_CHARS:
        return [block]
    chunks, current = [], ""
    for line in block.text.splitlines():
        if current and len(current) + len(line) > CHUNK_TARGET:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}".strip() if current else line
    if current:
        chunks.append(current)
    return [PageBlock(block.x0, block.y0, block.x1, block.y1, chunk) for chunk in chunks]


def _union(blocks: list[PageBlock]) -> tuple[float, float, float, float]:
    return (min(b.x0 for b in blocks), min(b.y0 for b in blocks), max(b.x1 for b in blocks), max(b.y1 for b in blocks))


def _build_passage(blocks: list[PageBlock], page: int) -> ExtractedPassage | None:
    text = "\n".join(b.text for b in blocks)
    if _alnum(text) < MIN_ALNUM:
        return None
    first_line = blocks[0].text.splitlines()[0]
    title = first_line.strip() if looks_like_title(first_line) else re.sub(r"\s+", " ", text)[:80].strip()
    weights, unit_weights = extract_weights(text)
    refs, packaging = extract_refs(text), extract_packaging(text)
    has_data = bool(weights or refs or packaging)
    return ExtractedPassage(
        local_page=page, kind="product" if has_data else "text", title=title[:120], text=text, refs=refs, weights_g=weights,
        unit_weights_g=unit_weights, packaging=packaging, structured=bool(weights and (refs or packaging) and looks_like_title(first_line)),
        bbox=_union(blocks), content_hash=content_hash(text),
    )


def segment_page(blocks: list[PageBlock], page: int) -> list[ExtractedPassage]:
    """Regroupe les blocs d'une page (ordre de lecture) en passages produit.

    Un nouveau passage commence quand une ligne ressemble à un titre alors que le passage en cours contient
    déjà des données (grammage, référence, conditionnement), ou quand il devient trop long.
    """
    expanded = [part for b in blocks if b.text and not _is_noise(b.text) for part in _split_long(b)]
    units: list[list[PageBlock]] = []
    current: list[PageBlock] = []
    for block in expanded:
        if current:
            current_text = "\n".join(b.text for b in current)
            first_line = block.text.splitlines()[0]
            gap = block.y0 - current[-1].y1
            starts_new = _has_data(current_text) and (looks_like_title(first_line) or gap > GAP_NEW_UNIT)
            if starts_new or len(current_text) + len(block.text) > MAX_UNIT_CHARS:
                units.append(current)
                current = []
        current.append(block)
    if current:
        units.append(current)
    return [p for unit in units if (p := _build_passage(unit, page)) is not None]


def drop_repeated_blocks(pages: list[list[PageBlock]]) -> list[list[PageBlock]]:
    """Supprime en-têtes / pieds de page : bloc court identique sur plus de 30 % des pages."""
    if len(pages) < 4:
        return pages
    counts: Counter[str] = Counter()
    for blocks in pages:
        counts.update({normalize(b.text) for b in blocks if len(b.text) < 80})
    limit = max(3, int(len(pages) * 0.3))
    repeated = {text for text, n in counts.items() if n > limit}
    return [[b for b in blocks if normalize(b.text) not in repeated] for blocks in pages]


def nearest_passage_index(image_bbox: tuple[float, float, float, float], page_passages: list[tuple[int, ExtractedPassage]]) -> int | None:
    """Rattache une photo au passage le plus proche sur la même page (distance entre rectangles)."""
    best, best_distance = None, float("inf")
    ix0, iy0, ix1, iy1 = image_bbox
    for index, passage in page_passages:
        if passage.bbox is None:
            continue
        px0, py0, px1, py1 = passage.bbox
        dx = max(px0 - ix1, ix0 - px1, 0.0)
        dy = max(py0 - iy1, iy0 - py1, 0.0)
        distance = dx + dy
        if distance < best_distance:
            best, best_distance = index, distance
    return best


# ---------------------------------------------------------------------------
# PyMuPDF
# ---------------------------------------------------------------------------


def _import_fitz() -> Any:
    try:
        import pymupdf as fitz  # type: ignore[import-not-found]
    except ImportError:
        try:
            import fitz  # type: ignore[import-not-found,no-redef]
        except ImportError as exc:
            raise PdfError("PyMuPDF n'est pas installé (pip install pymupdf).") from exc
    return fitz


def fitz_available() -> bool:
    try:
        _import_fitz()
    except PdfError:
        return False
    return True


def _page_blocks(page: Any, textpage: Any = None) -> list[PageBlock]:
    kwargs = {"textpage": textpage} if textpage is not None else {}
    result = []
    for raw in page.get_text("blocks", sort=True, **kwargs):
        x0, y0, x1, y1, text, _number, kind = raw[:7]
        cleaned = clean_text(text) if kind == 0 else ""
        if cleaned:
            result.append(PageBlock(float(x0), float(y0), float(x1), float(y1), cleaned))
    return result


def _image_bytes(doc: Any, fitz: Any, xref: int) -> bytes | None:
    try:
        return doc.extract_image(xref)["image"]
    except Exception:  # noqa: BLE001
        pass
    try:  # formats exotiques (CMYK, JPX...) : conversion RVB par PyMuPDF
        pix = fitz.Pixmap(doc, xref)
        if pix.n - pix.alpha >= 4:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        return pix.tobytes("png")
    except Exception:  # noqa: BLE001
        return None


def _page_images(doc: Any, fitz: Any, page: Any, page_number: int, budget: int) -> list[ExtractedImage]:
    found: list[ExtractedImage] = []
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    for info in page.get_images(full=True):
        if len(found) >= budget:
            break
        xref = int(info[0])
        try:
            rects = page.get_image_rects(xref)
        except Exception:  # noqa: BLE001
            continue
        if not rects:
            continue
        rect = rects[0]
        if rect.width < 40 or rect.height < 40 or rect.width * rect.height > 0.85 * page_area:
            continue
        data = _image_bytes(doc, fitz, xref)
        image = load_image(data) if data else None
        if image is None or not is_usable_product_photo(image):
            continue
        descriptor = compute_descriptor(image)
        if descriptor is not None:
            found.append(ExtractedImage(page_number, (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)), descriptor, xref))
    return found


def extract_pdf(
    path: Path, *, with_images: bool = True, ocr: bool = False, ocr_languages: str = "fra+eng",
    max_images: int = 400, max_images_per_page: int = 12, progress: Callable[[int, int], None] | None = None,
) -> ExtractionResult:
    """Lit un PDF (une partie < 10 Mo) et retourne passages + photos. Lève ``PdfError`` si illisible."""
    fitz = _import_fitz()
    try:
        doc = fitz.open(str(path))
    except Exception as exc:  # noqa: BLE001
        raise PdfError(f"PDF illisible : {type(exc).__name__}.") from None
    try:
        if getattr(doc, "needs_pass", False):
            raise PdfError("PDF protégé par mot de passe : impossible de l'indexer.")
        total = len(doc)
        result = ExtractionResult(page_count=total)
        pages: list[list[PageBlock]] = []
        for number in range(total):
            page = doc[number]
            blocks = _page_blocks(page)
            if sum(len(b.text) for b in blocks) < TEXT_THRESHOLD:
                result.pages_without_text += 1
                if ocr:
                    blocks = _ocr_blocks(page, ocr_languages, result) or blocks
            pages.append(blocks)
            if progress and number % 10 == 0:
                progress(number, total * 2)
        pages = drop_repeated_blocks(pages)
        xref_pages: Counter[int] = Counter()
        for number, blocks in enumerate(pages):
            local = number + 1
            first_index = len(result.passages)
            result.passages.extend(segment_page(blocks, local))
            if with_images and len(result.images) < max_images:
                found = _page_images(doc, fitz, doc[number], local, min(max_images_per_page, max_images - len(result.images)))
                page_passages = list(enumerate(result.passages[first_index:], start=first_index))
                for image in found:
                    xref_pages[image.xref] += 1
                    image.passage_index = nearest_passage_index(image.bbox, page_passages)
                result.images.extend(found)
            if progress and number % 10 == 0:
                progress(total + number, total * 2)
        result.images = [i for i in result.images if xref_pages[i.xref] <= 3]  # logos / fonds répétés
        _dedupe_passages(result)
        if result.needs_ocr:
            result.warnings.append("PDF scanné : aucun texte exploitable (OCR requis).")
        return result
    finally:
        doc.close()


def _dedupe_passages(result: ExtractionResult) -> None:
    """Supprime les passages identiques d'un même PDF en remappant les index des photos."""
    seen: dict[str, int] = {}
    mapping: dict[int, int] = {}
    kept: list[ExtractedPassage] = []
    for old_index, passage in enumerate(result.passages):
        key = f"{passage.local_page}:{passage.content_hash}" if passage.kind == "text" else passage.content_hash
        if key in seen:
            mapping[old_index] = seen[key]
            continue
        seen[key] = len(kept)
        mapping[old_index] = len(kept)
        kept.append(passage)
    result.passages = kept
    for image in result.images:
        image.passage_index = mapping.get(image.passage_index) if image.passage_index is not None else None


def _ocr_blocks(page: Any, languages: str, result: ExtractionResult) -> list[PageBlock]:
    """OCR facultatif (nécessite Tesseract sur le serveur). Échec = avertissement, jamais une erreur."""
    try:
        textpage = page.get_textpage_ocr(language=languages, dpi=150, full=True)
        blocks = _page_blocks(page, textpage)
        result.ocr_used = result.ocr_used or bool(blocks)
        return blocks
    except Exception:  # noqa: BLE001
        warning = "OCR indisponible (Tesseract non installé sur le serveur)."
        if warning not in result.warnings:
            result.warnings.append(warning)
        return []


def render_page_png(pdf_path: Path, page_number: int, zoom: float = 1.4) -> bytes:
    """Aperçu PNG d'une page (numéro local, base 1)."""
    fitz = _import_fitz()
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        raise PdfError(f"PDF illisible : {type(exc).__name__}.") from None
    try:
        if not 1 <= page_number <= len(doc):
            raise PdfError("Page introuvable dans ce PDF.")
        return doc[page_number - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
    finally:
        doc.close()
