"""Nommage des catalogues et de leurs parties (PDF découpés sous la limite Cloudinary).

Convention de l'application d'origine, conservée pour retrouver les catalogues déjà envoyés :

    <préfixe>/<sous-dossier>/<Nom>_part_02_pages_46-90.pdf

``pages_46-90`` = pages de la partie dans le PDF d'origine : le numéro de page original se
reconstitue sans état externe. Les noms de type ``Catalogue part 2.pdf`` sont aussi reconnus
(le décalage est alors recalculé à partir du nombre de pages des parties précédentes).

Fournisseur : comme dans l'application d'origine, il se déduit du début du nom de fichier
(premier mot en majuscules). Si une liste de fournisseurs référencés est fournie, le nom
officiel de la liste est utilisé lorsqu'il est identifié sans ambiguïté.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sourcing_suppliers import SupplierDirectory

_PART_RE = re.compile(r"^(?P<stem>.+?)_part_(?P<idx>\d+)_pages_(?P<start>\d+)-(?P<end>\d+)$", re.IGNORECASE)
_LEGACY_PART_RE = re.compile(
    r"^(?P<stem>.+?)[\s_.\-]*(?:part|partie)[\s_.\-]*(?P<idx>\d{1,3})"
    r"(?:[\s_.\-]*(?:of|sur|de|/)[\s_.\-]*(?P<total>\d{1,3}))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CatalogRef:
    public_id: str
    folder: str
    logical_name: str  # dossier + nom sans marqueur de partie ni extension
    logical_key: str  # clé de regroupement (minuscules)
    display_name: str
    supplier: str
    part_index: int = 1
    part_total: int = 1
    start_page: int | None = None  # 1-based, page d'origine de la 1re page de la partie
    end_page: int | None = None

    @property
    def page_offset(self) -> int | None:
        """Nombre de pages précédant cette partie dans le PDF d'origine, si connu."""
        if self.start_page is not None:
            return self.start_page - 1
        if self.part_index == 1:
            return 0
        return None


def safe_stem(name: str) -> str:
    """Nom de fichier sûr pour un public_id (lettres, chiffres, tiret, souligné)."""
    cleaned = re.sub(r"[^\w\-]+", "_", re.sub(r"\.pdf$", "", name.strip(), flags=re.IGNORECASE), flags=re.UNICODE)
    return cleaned.strip("_") or "catalogue"


def part_filename(stem: str, index: int, start_page: int, end_page: int) -> str:
    return f"{stem}_part_{index:02d}_pages_{start_page}-{end_page}.pdf"


def clean_label(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-.]+", " ", raw)).strip()


def guess_supplier(stem: str, directory: "SupplierDirectory | None" = None) -> str:
    """Nom officiel de la liste si identifiable, sinon premier mot du fichier en majuscules."""
    if directory is not None:
        official = directory.canonical_for_stem(stem)
        if official:
            return official
    first = re.split(r"[\s_\-]+", stem.strip())[0] if stem.strip() else ""
    return first.upper() or "INCONNU"


def parse_catalog_name(public_id: str, directory: "SupplierDirectory | None" = None) -> CatalogRef:
    folder, _, filename = public_id.rpartition("/")
    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    part_index, part_total, start_page, end_page = 1, 1, None, None
    logical_stem = stem
    match = _PART_RE.match(stem)
    if match:
        logical_stem = match.group("stem")
        part_index = int(match.group("idx"))
        part_total = part_index  # total inconnu : recalculé par l'index à partir des parties présentes
        start_page, end_page = int(match.group("start")), int(match.group("end"))
    else:
        legacy = _LEGACY_PART_RE.match(stem)
        if legacy:
            logical_stem = legacy.group("stem")
            part_index = int(legacy.group("idx"))
            part_total = int(legacy.group("total") or 0) or part_index
    logical_name = f"{folder}/{logical_stem}" if folder else logical_stem
    return CatalogRef(
        public_id=public_id, folder=folder, logical_name=logical_name, logical_key=logical_name.lower(),
        display_name=clean_label(logical_stem), supplier=guess_supplier(logical_stem, directory),
        part_index=part_index, part_total=part_total, start_page=start_page, end_page=end_page,
    )
