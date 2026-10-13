"""Décisions acheteur (Pertinent / À étudier / Non pertinent) et export CSV pour appel d'offres."""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable

from sourcing_internal_search import InternalHit
from sourcing_models import NOT_FOUND_PUBLIC, SupplierCandidate

DECISIONS = ("Pertinent", "À étudier", "Non pertinent")
CSV_FIELDS = (
    "décision", "origine", "fournisseur", "produit", "référence", "grammage", "pays", "statut", "score (points)",
    "page catalogue", "lien page produit", "lien fiche technique / catalogue", "lien contact",
    "preuve industrielle", "preuve produit", "private label", "foodservice", "à confirmer", "date de vérification",
)


def row_from_internal(hit: InternalHit, today: str) -> dict[str, str]:
    return {
        "origine": "catalogue interne", "fournisseur": hit.supplier, "produit": hit.title,
        "référence": ", ".join(hit.refs), "grammage": ", ".join(f"{w:g} g" for w in hit.weights_g),
        "pays": "", "statut": "base interne", "score (points)": hit.score_label,
        "page catalogue": f"{hit.catalog} — {hit.page_label}", "lien page produit": hit.pdf_url or NOT_FOUND_PUBLIC,
        "lien fiche technique / catalogue": hit.pdf_url or "", "lien contact": "",
        "preuve industrielle": "", "preuve produit": hit.excerpt, "private label": "", "foodservice": "",
        "à confirmer": "MOQ ; capacité ; certifications ; origine ; lead time ; livraison France",
        "date de vérification": today,
    }


def row_from_candidate(candidate: SupplierCandidate) -> dict[str, str]:
    def evidence(items: list[Any]) -> str:
        return " | ".join(f"{e.url} ({e.origin}, {e.checked_on})" for e in items)

    return {
        "origine": "recherche Internet", "fournisseur": candidate.company,
        "produit": candidate.similar_product or "", "référence": "",
        "grammage": ", ".join(f"{w:g} g" for w in candidate.weights_g),
        "pays": candidate.country or "à confirmer", "statut": candidate.status, "score (points)": candidate.score_label,
        "page catalogue": "", "lien page produit": candidate.product_page_url or NOT_FOUND_PUBLIC,
        "lien fiche technique / catalogue": candidate.datasheet_url or NOT_FOUND_PUBLIC,
        "lien contact": candidate.contact_url or NOT_FOUND_PUBLIC,
        "preuve industrielle": evidence(candidate.industrial_evidence) or NOT_FOUND_PUBLIC,
        "preuve produit": evidence(candidate.product_evidence) or NOT_FOUND_PUBLIC,
        "private label": candidate.private_label.value, "foodservice": candidate.foodservice.value,
        "à confirmer": " ; ".join(candidate.to_confirm), "date de vérification": candidate.checked_on,
    }


def to_csv_bytes(entries: Iterable[tuple[str, dict[str, str]]], *, delimiter: str = ";") -> bytes:
    """CSV UTF-8 avec BOM, séparateur « ; » : s'ouvre directement dans Excel en français."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, delimiter=delimiter, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for decision, row in entries:
        writer.writerow({**{f: "" for f in CSV_FIELDS}, **row, "décision": decision})
    return buffer.getvalue().encode("utf-8-sig")
