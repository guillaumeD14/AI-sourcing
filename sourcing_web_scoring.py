"""Agrégation des preuves d'un domaine, statut industriel et score explicable (en points)."""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable

from sourcing_domains import EUROPE_COUNTRIES, company_from_titles, country_from_domain, country_from_text
from sourcing_manufacturers import VerifiedManufacturer
from sourcing_models import Evidence, Level, SupplierCandidate
from sourcing_suppliers import SupplierDirectory
from sourcing_textutils import QueryPlan, normalize, weight_proximity
from sourcing_web_classify import ResultFacts

TO_CONFIRM_ALWAYS = ("MOQ", "origine des matières premières", "lead time", "livraison France")
MAX_POINTS = {"preuve industrielle": 30.0, "produit similaire": 25.0, "grammage": 10.0,
              "private label / foodservice": 15.0, "Europe": 10.0, "liens directs": 10.0}


def _best_url(facts: Iterable[ResultFacts], kind: str) -> ResultFacts | None:
    candidates = [f for f in facts if f.official and kind in f.kinds]
    if kind == "produit":
        candidates.sort(key=lambda f: (-f.product_level.rank, -len(f.url.rstrip("/").split("/"))))
        return candidates[0] if candidates else None
    candidates.sort(key=lambda f: (-f.url.lower().endswith(".pdf"), len(f.url)))
    return candidates[0] if candidates else None


def _evidence(fact: ResultFacts, kind: str, today: str) -> Evidence:
    return Evidence(kind=kind, url=fact.url, title=fact.title[:120], snippet=fact.snippet, checked_on=today)


def build_candidate(
    domain: str,
    facts: list[ResultFacts],
    plan: QueryPlan,
    *,
    seed: VerifiedManufacturer | None,
    discovery_hits: int,
    discovery_text: str,
    directory: SupplierDirectory,
    today: date,
    titles: Iterable[str] = (),
) -> SupplierCandidate:
    stamp = today.isoformat()
    all_titles = [*titles, *(f.title for f in facts if f.official and f.title)]
    company = seed.company if seed else company_from_titles(domain, all_titles)
    candidate = SupplierCandidate(domain=domain, company=company, checked_on=stamp, from_verified_base=seed is not None)

    # --- preuve industrielle (officielle uniquement) ---
    strong = [f for f in facts if f.official and f.industrial_strength == 2]
    weak = [f for f in facts if f.official and f.industrial_strength == 1]
    for fact in (strong or weak)[:3]:
        candidate.industrial_evidence.append(_evidence(fact, "industriel", stamp))
    if seed and seed.industrial_evidence_url:
        candidate.industrial_evidence.insert(0, Evidence("industriel", seed.industrial_evidence_url, seed.company,
                                                         seed.source_note, "base vérifiée", seed.verified_on))
    if strong or (seed and seed.industrial_evidence_url):
        candidate.industrial_level = Level.CONFIRMED
    elif weak:
        candidate.industrial_level = Level.PROBABLE
    elif discovery_hits >= 2:
        candidate.industrial_level = Level.TO_CONFIRM

    # --- produit similaire ---
    product = _best_url(facts, "produit")
    if product:
        candidate.product_level = product.product_level
        candidate.product_page_url = product.url
        candidate.similar_product = re.sub(r"\s+", " ", product.title).strip()[:110] or None
        candidate.product_evidence.append(_evidence(product, "produit", stamp))
        candidate.weights_g = product.weights_g
    if seed and seed.product_page_url:
        keywords = {normalize(k) for k in seed.product_keywords}
        plan_keys = {normalize(f.key) for f in plan.families}
        level = (Level.CONFIRMED if keywords & plan_keys
                 else Level.PROBABLE if set(seed.categories) & set(plan.categories) or not plan.families
                 else Level.TO_CONFIRM)
        if level.rank > candidate.product_level.rank or not candidate.product_page_url:
            candidate.product_level = level
            candidate.product_page_url = seed.product_page_url
            candidate.similar_product = candidate.similar_product or seed.company
        candidate.product_evidence.append(Evidence("produit", seed.product_page_url, seed.company, seed.source_note,
                                                   "base vérifiée", seed.verified_on))
    if candidate.product_level is Level.NOT_FOUND and discovery_hits:
        candidate.product_level = Level.TO_CONFIRM

    # --- liens ---
    sheet = _best_url(facts, "fiche technique")
    contact = _best_url(facts, "contact")
    candidate.datasheet_url = sheet.url if sheet else (seed.datasheet_url if seed and seed.datasheet_url else None)
    candidate.contact_url = contact.url if contact else (seed.contact_url if seed and seed.contact_url else None)

    # --- private label / foodservice ---
    official = [f for f in facts if f.official]
    candidate.private_label = (Level.CONFIRMED if any(f.private_label for f in official) or (seed and seed.private_label)
                               else Level.PROBABLE if re.search(r"private[\s-]?label|marque distributeur|co-?manufactur", discovery_text, re.I)
                               else Level.NOT_FOUND)
    candidate.foodservice = (Level.CONFIRMED if any(f.foodservice for f in official) or (seed and seed.foodservice)
                             else Level.PROBABLE if re.search(r"food[\s-]?service|b2b|rhd|horeca", discovery_text, re.I)
                             else Level.NOT_FOUND)

    # --- divers ---
    candidate.certifications = sorted({c for f in official for c in f.certifications})
    capacity = next((f.capacity for f in official if f.capacity), None)
    candidate.distributor_signals = sum(f.distributor_hits for f in official) >= 2 and candidate.industrial_level is not Level.CONFIRMED
    candidate.categories = list(seed.categories) if seed else list(plan.categories)
    candidate.country, candidate.country_source = _country(domain, seed, " ".join(f.snippet for f in official) + " " + discovery_text)
    candidate.to_confirm = ["MOQ"] + ([] if capacity else ["capacité industrielle"]) + (
        [] if candidate.certifications else ["certifications"]) + [x for x in TO_CONFIRM_ALWAYS if x != "MOQ"]
    known = directory.match([candidate.company], domain, " ".join(all_titles))
    candidate.known_supplier = known is not None
    candidate.known_as = known.supplier if known else None
    candidate.notes = []
    if capacity:
        candidate.notes.append(f"capacité citée : {capacity}")
    _score(candidate, plan)
    return candidate


def _country(domain: str, seed: VerifiedManufacturer | None, text: str) -> tuple[str | None, str]:
    if seed and seed.country:
        return seed.country, "base vérifiée"
    tld_country = country_from_domain(domain)
    if tld_country:
        return tld_country, f"domaine .{domain.rsplit('.', 1)[-1]}"
    mentioned = country_from_text(text)
    return (mentioned, "contenu des pages") if mentioned else (None, "à confirmer")


def _country_points(country: str | None, plan: QueryPlan) -> float:
    """Pays demandé (10) > pays inconnu (4) > autre pays européen (2) ; sans pays demandé : Europe (10)."""
    if plan.countries:
        return 10.0 if country in plan.countries else 4.0 if country is None else 2.0 if country in EUROPE_COUNTRIES else 0.0
    return 10.0 if country in EUROPE_COUNTRIES else 4.0 if country is None else 0.0


def _score(candidate: SupplierCandidate, plan: QueryPlan) -> None:
    levels = {Level.CONFIRMED: 1.0, Level.PROBABLE: 0.5, Level.TO_CONFIRM: 0.15, Level.NOT_FOUND: 0.0}
    comp = {
        "preuve industrielle": MAX_POINTS["preuve industrielle"] * levels[candidate.industrial_level],
        "produit similaire": MAX_POINTS["produit similaire"] * levels[candidate.product_level],
        "private label / foodservice": 8.0 * levels[candidate.private_label] + 7.0 * levels[candidate.foodservice],
        "Europe" if not plan.countries else "pays demandé": _country_points(candidate.country, plan),
        "liens directs": 4.0 * bool(candidate.product_page_url) + 3.0 * bool(candidate.datasheet_url)
        + 3.0 * bool(candidate.contact_url),
    }
    maxima = dict(MAX_POINTS)
    if plan.countries:
        maxima["pays demandé"] = maxima.pop("Europe")
    if plan.weight_g:
        comp["grammage"] = MAX_POINTS["grammage"] * weight_proximity(plan.weight_g, candidate.weights_g, 0.15)
        candidate.notes.append(
            "grammage trouvé : " + (", ".join(f"{w:g} g" for w in candidate.weights_g) or "non indiqué")
            + f" (cible {plan.weight_g:g} g)"
        )
    else:
        maxima.pop("grammage")
    candidate.penalties = {}
    if candidate.distributor_signals:
        candidate.penalties["signaux de distributeur / revendeur"] = 15.0
    if not candidate.industrial_evidence:
        candidate.penalties["aucune preuve industrielle officielle"] = 10.0
    candidate.components = {k: round(v, 1) for k, v in comp.items()}
    candidate.max_score = sum(maxima.values())
    candidate.score = round(max(0.0, sum(comp.values()) - sum(candidate.penalties.values())), 1)
