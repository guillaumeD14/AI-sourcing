"""Base de fabricants vérifiés (``data/verified_manufacturers.csv``), enrichissable sans toucher au code.

Chaque ligne doit citer ses sources (URL officielles) et la date de vérification. Une ligne sans
domaine ou sans date de vérification est ignorée : aucune donnée non sourcée n'entre dans les résultats.
La base sert de filet de sécurité : ses fabricants sont ajoutés aux candidats, mais leur statut
reste calculé à partir des preuves (de la base et/ou de la recherche web).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from sourcing_domains import registered_domain
from sourcing_textutils import QueryPlan, normalize

CSV_COLUMNS = (
    "company", "domain", "country", "categories", "product_keywords", "industrial_evidence_url",
    "product_page_url", "datasheet_url", "contact_url", "private_label", "foodservice", "source_note", "verified_on",
)


@dataclass(frozen=True)
class VerifiedManufacturer:
    company: str
    domain: str
    country: str = ""
    categories: tuple[str, ...] = ()
    product_keywords: tuple[str, ...] = ()
    industrial_evidence_url: str = ""
    product_page_url: str = ""
    datasheet_url: str = ""
    contact_url: str = ""
    private_label: bool = False
    foodservice: bool = False
    source_note: str = ""
    verified_on: str = ""
    extra: dict[str, str] = field(default_factory=dict, compare=False)


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in (value or "").split(";") if part.strip())


def _yes(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "oui", "yes", "true", "vrai", "x"}


def load_verified_manufacturers(path: str | Path) -> list[VerifiedManufacturer]:
    csv_path = Path(path)
    if not csv_path.exists():
        return []
    rows: list[VerifiedManufacturer] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        lines = (line for line in handle if line.strip() and not line.lstrip().startswith("#"))
        for raw in csv.DictReader(lines):
            domain = registered_domain((raw.get("domain") or "").strip())
            verified_on = (raw.get("verified_on") or "").strip()
            company = (raw.get("company") or "").strip()
            if not (domain and verified_on and company):
                continue
            rows.append(
                VerifiedManufacturer(
                    company=company, domain=domain, country=(raw.get("country") or "").strip(),
                    categories=_split(raw.get("categories", "")), product_keywords=_split(raw.get("product_keywords", "")),
                    industrial_evidence_url=(raw.get("industrial_evidence_url") or "").strip(),
                    product_page_url=(raw.get("product_page_url") or "").strip(),
                    datasheet_url=(raw.get("datasheet_url") or "").strip(),
                    contact_url=(raw.get("contact_url") or "").strip(),
                    private_label=_yes(raw.get("private_label", "")), foodservice=_yes(raw.get("foodservice", "")),
                    source_note=(raw.get("source_note") or "").strip(), verified_on=verified_on,
                )
            )
    return rows


def select_seeds(rows: list[VerifiedManufacturer], plan: QueryPlan, limit: int = 3) -> list[VerifiedManufacturer]:
    """Fabricants vérifiés pertinents pour le besoin (mots-clés produit d'abord, puis catégorie)."""
    plan_keys = {normalize(f.key) for f in plan.families}
    scored: list[tuple[int, VerifiedManufacturer]] = []
    for row in rows:
        keyword_hit = bool({normalize(k) for k in row.product_keywords} & plan_keys)
        category_hit = bool(set(row.categories) & set(plan.categories))
        if keyword_hit:
            scored.append((2, row))
        elif category_hit or not plan.categories:
            scored.append((1, row))
    scored.sort(key=lambda pair: (-pair[0], pair[1].company.lower()))
    return [row for _, row in scored[:limit]]
