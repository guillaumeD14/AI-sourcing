"""Base de fabricants (``verified_manufacturers.csv``).

C'est un filet de sécurité, jamais l'unique résultat : le fichier est lu à chaud, enrichissable à la main, et chaque
ligne porte sa source et sa date. Seule une URL de preuve *sur le domaine officiel* compte comme preuve officielle.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from sourcing_domains import registered_domain
from sourcing_models import Evidence
from sourcing_textutils import COUNTRIES, normalize


@dataclass(frozen=True)
class VerifiedManufacturer:
    name: str
    country_label: str
    country: str          # code ISO (vide si inconnu)
    site: str
    categories: tuple[str, ...]
    reliability: str
    product_url: str
    industrial_url: str
    note: str
    checked_on: str

    @property
    def domain(self) -> str:
        return registered_domain(self.site) if self.site else ""

    @property
    def is_lead(self) -> bool:
        return self.reliability.lower().startswith("piste")

    def official_url(self, url: str) -> bool:
        return bool(url) and bool(self.domain) and registered_domain(url) == self.domain

    def evidence(self) -> list[Evidence]:
        """Preuves portées par la base : uniquement celles hébergées sur le domaine officiel."""
        items = []
        if self.official_url(self.product_url):
            items.append(Evidence("produit", self.product_url, "base vérifiée", self.categories[0] if self.categories else "", self.checked_on))
        if self.official_url(self.industrial_url):
            items.append(Evidence("industrielle", self.industrial_url, "base vérifiée", "", self.checked_on))
        return items


def first_country_iso(label: str) -> str:
    """Pays cité en premier dans un libellé (« Allemagne (filiale ; groupe basé en Suisse) » -> DE)."""
    norm = normalize(label)
    best: tuple[int, str] | None = None
    for iso, names in COUNTRIES.items():
        for name in names:
            match = re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", norm)
            if match and (best is None or match.start() < best[0]):
                best = (match.start(), iso)
    return best[1] if best else ""


def load_verified_manufacturers(path: Path | str) -> list[VerifiedManufacturer]:
    path = Path(path)
    if not path.exists():
        return []
    result = []
    for row in csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"):
        name = (row.get("nom") or "").strip()
        if not name:
            continue
        country_label = (row.get("pays") or "").strip()
        result.append(VerifiedManufacturer(
            name=name, country_label=country_label, country=first_country_iso(country_label), site=(row.get("site_officiel") or "").strip(),
            categories=tuple(c.strip() for c in re.split(r"[;,]", row.get("categories") or "") if c.strip()),
            reliability=(row.get("fiabilite") or "").strip(), product_url=(row.get("preuve_produit_url") or "").strip(),
            industrial_url=(row.get("preuve_industrielle_url") or "").strip(), note=(row.get("note") or "").strip(),
            checked_on=(row.get("date_verification") or "").strip()))
    return result
