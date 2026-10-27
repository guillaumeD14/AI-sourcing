"""Fournisseurs déjà connus de l'acheteur (``known_suppliers.csv``) + fournisseurs des catalogues indexés.

Sert à distinguer « connu » / « nouveau » et à déduire le fournisseur d'un nom de fichier PDF.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sourcing_domains import registered_domain
from sourcing_textutils import normalize

_LEGAL = frozenset("""sas sarl sa srl spa gmbh ag bv nv sl slu sau ltd ab as kg co ohg snc sc eurl inc llc gesmbh doo dd ou oy a s s r l s p a
con unico socio sociedad limitada""".split())
_WEAK = frozenset("""bakery bakeries bakers baker bakerei backerei backwaren panificio panaderia group groupe holding foods food europe
france italia italy deutschland germany espana spain nederland switzerland schweiz suisse industrie industries produktions
production produkte products surgeles surgelati tiefkuhl frozen company""".split())
_MARKERS = re.compile(r"(?:^|[_\-\s.])(catalogue|catalog|katalog|catalogo|tarif|tarifs|prix|prices|preislist\w*|produits?|products?|"
                      r"fiche\w*|2\d{3}|v\d+)(?:[_\-\s.]|$)", re.I)
_PART_SUFFIX = re.compile(r"[_\-\s.]*(?:part(?:ie)?[_\-\s.]*\d+(?:[_\-\s.]*(?:of|sur|de)[_\-\s.]*\d+)?|p\d{1,4}[_\-\s.]*-[_\-\s.]*\d{1,4})$", re.I)


@dataclass(frozen=True)
class KnownSupplier:
    name: str
    country: str = ""
    site: str = ""
    category: str = ""
    reliability: str = ""
    source: str = ""
    origin: str = "liste acheteur"

    @property
    def domain(self) -> str:
        return registered_domain(self.site) if self.site else ""

    @property
    def core_tokens(self) -> tuple[str, ...]:
        tokens = [t for t in normalize(self.name).split() if t not in _LEGAL and not t.isdigit() and len(t) > 1]
        strong = [t for t in tokens if t not in _WEAK]
        return tuple(strong or tokens)

    @property
    def is_generic(self) -> bool:
        """Nom trop générique pour être cherché seul (« Bakery SPA », « Baker & Baker »)."""
        core = self.core_tokens
        return not core or all(t in _WEAK for t in core)

    @property
    def search_name(self) -> str:
        """Nom utilisable dans une requête web : sans forme juridique (SAS, GmbH, S.L.…) ni codes numériques."""
        words = [w for w in re.sub(r"[&,.()/]", " ", self.name).split() if len(w) > 1]
        kept = [w for w in words if normalize(w) not in _LEGAL and not re.fullmatch(r"[\d\-]+", w)]
        return " ".join(kept) or self.name

    @property
    def verified(self) -> bool:
        return self.reliability.lower().startswith("vérifié") or self.reliability.lower().startswith("verifie")


def _token_matches(core: str, candidates: set[str]) -> bool:
    return core in candidates or (len(core) >= 6 and any(c.startswith(core) or (core.startswith(c) and len(c) >= 6) for c in candidates))


class SupplierDirectory:
    def __init__(self, suppliers: Iterable[KnownSupplier] = ()) -> None:
        self._suppliers: list[KnownSupplier] = []
        seen: set[str] = set()
        for supplier in suppliers:
            key = normalize(supplier.name)
            if key and key not in seen:
                seen.add(key)
                self._suppliers.append(supplier)

    @classmethod
    def from_file(cls, path: Path | str) -> "SupplierDirectory":
        path = Path(path)
        if not path.exists():
            return cls()
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        delimiter = ";" if text.splitlines()[0].count(";") >= text.splitlines()[0].count(",") else ","
        suppliers = []
        for row in csv.DictReader(text.splitlines(), delimiter=delimiter):
            lowered = {normalize(k or "").replace(" ", "_"): (v or "").strip() for k, v in row.items()}
            name = lowered.get("nom") or lowered.get("name") or lowered.get("fournisseur") or ""
            if name:
                suppliers.append(KnownSupplier(
                    name=name, country=lowered.get("pays", ""), site=lowered.get("site", "") or lowered.get("site_officiel", ""),
                    category=lowered.get("categorie", ""), reliability=lowered.get("fiabilite", ""), source=lowered.get("source", "")))
        return cls(suppliers)

    def with_names(self, names: Iterable[str]) -> "SupplierDirectory":
        """Ajoute les fournisseurs des catalogues indexés, sans doublon avec la liste de l'acheteur."""
        extra = []
        for name in names:
            if name and self.match_text(name) is None:
                extra.append(KnownSupplier(name=name, origin="catalogues indexés"))
        return SupplierDirectory([*self._suppliers, *extra])

    @property
    def suppliers(self) -> list[KnownSupplier]:
        return list(self._suppliers)

    def __len__(self) -> int:
        return len(self._suppliers)

    def match_text(self, text: str) -> KnownSupplier | None:
        """Fournisseur connu dont le nom (jetons distinctifs) apparaît dans ``text``."""
        tokens = set(normalize(text).split())
        best: tuple[int, KnownSupplier] | None = None
        for supplier in self._suppliers:
            core = supplier.core_tokens
            if not core or (len(core[0]) < 4 and len(core) == 1) or all(t in _WEAK for t in core):
                continue  # noms trop génériques (« Baker & Baker ») : jamais déduits d'un simple mot
            hits = sum(_token_matches(t, tokens) for t in core)
            if _token_matches(core[0], tokens) and hits / len(core) >= 0.5:
                score = hits * 10 + len(core[0])
                if best is None or score > best[0]:
                    best = (score, supplier)
        return best[1] if best else None

    def match_domain(self, domain: str) -> KnownSupplier | None:
        domain = registered_domain(domain)
        label = normalize(domain.split(".")[0]).replace(" ", "")
        for supplier in self._suppliers:
            if supplier.domain and supplier.domain == domain:
                return supplier
        for supplier in self._suppliers:
            core = supplier.core_tokens
            if core and len(core[0]) >= 4 and (label == core[0] or (len(label) > len(core[0]) and label.startswith(core[0]))):
                return supplier
        return None

    def infer_from_filename(self, filename: str) -> str:
        """Fournisseur d'un PDF : nom connu trouvé dans le fichier, sinon début du nom (avant « catalogue », « tarif »…)."""
        stem = re.sub(r"\.pdf$", "", filename.rsplit("/", 1)[-1], flags=re.I)
        stem = _PART_SUFFIX.sub("", stem)
        known = self.match_text(stem)
        if known:
            return known.name
        match = _MARKERS.search(stem)
        head = stem[: match.start()] if match and match.start() > 0 else re.split(r"[_]", stem, maxsplit=1)[0]
        cleaned = re.sub(r"[_\-]+", " ", head).strip()
        return cleaned or re.sub(r"[_\-]+", " ", stem).strip()
