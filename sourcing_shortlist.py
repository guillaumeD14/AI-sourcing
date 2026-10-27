"""Décisions de l'acheteur (Pertinent / À étudier / Non pertinent) et export CSV exploitable en appel d'offres.

Le CSV est écrit en UTF-8 avec BOM et séparateur « ; » : il s'ouvre directement dans Excel en français.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from sourcing_internal_search import InternalHit
from sourcing_models import NOT_FOUND_PUBLIC, SupplierCandidate

DECISIONS = ("Pertinent", "À étudier", "Non pertinent")

COLUMNS = [
    "décision", "décidé par", "origine", "fournisseur", "pays", "statut", "produit", "grammage (g)", "référence", "conditionnement",
    "catalogue / source", "page", "lien page produit", "lien fiche technique / catalogue", "lien contact", "preuve industrielle",
    "preuve produit", "private label", "foodservice", "score (points)", "points à confirmer", "date de vérification",
]


def _g(values: list[float]) -> str:
    return ", ".join(f"{v:g}" for v in values)


def row_from_internal(hit: InternalHit, today: str) -> dict[str, str]:
    return {
        "origine": "catalogue interne", "fournisseur": hit.supplier, "pays": "", "statut": "base interne", "produit": hit.title,
        "grammage (g)": _g(hit.weights_g), "référence": ", ".join(hit.refs), "conditionnement": " ; ".join(hit.packaging),
        "catalogue / source": hit.catalog, "page": hit.page_label, "lien page produit": "", "lien fiche technique / catalogue": "",
        "lien contact": "", "preuve industrielle": "", "preuve produit": hit.excerpt[:200], "private label": "", "foodservice": "",
        "score (points)": hit.score_label, "points à confirmer": "", "date de vérification": today,
    }


def row_from_candidate(c: SupplierCandidate) -> dict[str, str]:
    def link(url: str | None) -> str:
        return url or NOT_FOUND_PUBLIC

    def proof(items: list[Any]) -> str:
        return " | ".join(e.url for e in items[:2]) or NOT_FOUND_PUBLIC

    return {
        "origine": "Internet" + (" (base vérifiée)" if c.from_verified_base else ""), "fournisseur": c.company, "pays": c.country,
        "statut": c.status, "produit": c.similar_product, "grammage (g)": _g(c.weights_g), "référence": "", "conditionnement": "",
        "catalogue / source": c.domain, "page": "", "lien page produit": link(c.product_page_url),
        "lien fiche technique / catalogue": link(c.datasheet_url), "lien contact": link(c.contact_url),
        "preuve industrielle": proof(c.industrial_evidence), "preuve produit": proof(c.product_evidence),
        "private label": c.private_label.value, "foodservice": c.foodservice.value, "score (points)": c.score_label,
        "points à confirmer": ", ".join(c.to_confirm), "date de vérification": c.checked_on,
    }


def to_csv_bytes(entries: list[tuple[str, dict[str, str]]]) -> bytes:
    """``entries`` : liste de ``(décision, ligne)``. Retourne un CSV Excel FR (UTF-8 BOM, « ; »)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, delimiter=";", lineterminator="\r\n", extrasaction="ignore")
    writer.writeheader()
    for decision, row in entries:
        writer.writerow({**{c: "" for c in COLUMNS}, **row, "décision": decision or row.get("décision", ""), "décidé par": row.get("décidé par", "")})
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def decisions_to_json(decisions: dict[str, tuple[str, dict[str, str]]]) -> bytes:
    payload = {key: {"decision": label, "row": row} for key, (label, row) in decisions.items()}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decisions_from_json(raw: bytes) -> dict[str, tuple[str, dict[str, str]]]:
    """Relit une shortlist sauvegardée ; toute entrée invalide est ignorée (jamais d'exception)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    result: dict[str, tuple[str, dict[str, str]]] = {}
    if isinstance(data, dict):
        for key, item in data.items():
            if isinstance(item, dict) and item.get("decision") in DECISIONS and isinstance(item.get("row"), dict):
                result[str(key)] = (item["decision"], {str(k): str(v) for k, v in item["row"].items()})
    return result


def merge_decisions(remote: dict[str, tuple[str, dict[str, str]]], local: dict[str, tuple[str, dict[str, str]]],
                    removed: set[str] = frozenset()) -> dict[str, tuple[str, dict[str, str]]]:  # type: ignore[assignment]
    """Fusion « dernier écrivain gagne » par clé : les décisions locales l'emportent, sauf celles annulées."""
    merged = {**remote, **local}
    for key in removed:
        merged.pop(key, None)
    return merged
