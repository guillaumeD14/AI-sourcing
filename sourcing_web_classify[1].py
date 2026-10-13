"""Classification des résultats Tavily : page produit, preuve industrielle, contact, fiche technique...

Seules les pages du domaine candidat sont des preuves « officielles ». Les résultats d'autres
domaines servent uniquement à découvrir des candidats, jamais à confirmer un fabricant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sourcing_domains import is_homepage, registered_domain
from sourcing_models import Level
from sourcing_textutils import QueryPlan, contains_phrase, extract_weights, normalize

_STRONG_INDUSTRIAL = re.compile(
    r"\busines?\b|\bfactor(?:y|ies)\b|\bproduction (?:sites?|plants?|facilit(?:y|ies)|lines?)\b|"
    r"\bsites? de production\b|\blignes? de production\b|\bproduction capacity\b|\bcapacit[eé] de production\b|"
    r"\b\d[\d\s.,]*\s*(?:tonnes?|tons|t/an|tonnes? per year)\b|\bproduktionsstandort|\bproduktionswerk|\bfabriek\b|"
    r"\bf[aá]brica\b|\bstabilimento\b|\bmanufacturing (?:sites?|plants?|facilit(?:y|ies))\b",
    re.IGNORECASE,
)
_WEAK_INDUSTRIAL = re.compile(
    r"\bmanufactur\w*|\bfabricant\b|\bfabrication\b|\bindustriel\w*|\bindustrial\b|\bhersteller\b|\bfabricante\b|\bproduttore\b",
    re.IGNORECASE,
)
_DISTRIBUTOR = re.compile(
    r"\bdistributeur\b|\bdistributor\b|\bwholesale\w*|\bgrossiste\b|\be-?shop\b|\badd to cart\b|"
    r"\bajouter au panier\b|\bboutique en ligne\b|\bonline shop\b|\brevendeur\b|\breseller\b|\bimporte\w*\b|\bimportateur\b",
    re.IGNORECASE,
)
_PRIVATE_LABEL = re.compile(
    r"private[\s-]?label|marques? de distributeurs?|marque distributeur|\bMDD\b|co-?manufactur\w*|contract manufactur\w*|"
    r"fa[cç]onnage|eigenmarke|handelsmarke|marca blanca|marca del distribuidor|own[\s-]?label|retailer brands?",
    re.IGNORECASE,
)
_FOODSERVICE = re.compile(
    r"food[\s-]?service|restauration hors domicile|\bRHD\b|\bhoreca\b|restauration collective|out[\s-]of[\s-]home|"
    r"\bb2b\b|professionnels?\b|cash\s*(?:&|and)\s*carry|gastronomie|catering|traiteur",
    re.IGNORECASE,
)
_CERTIFICATIONS = re.compile(r"\bIFS(?:\s*food)?\b|\bBRC(?:GS)?\b|\bFSSC\s*22000\b|\bISO\s*22000\b|\bHACCP\b|\bSMETA\b", re.IGNORECASE)
_CAPACITY = re.compile(r"\b\d[\d\s.,]*\s*(?:tonnes?|tons|t/an|million(?:s)? (?:of )?(?:pieces|pi[eè]ces|units|unit[eé]s))\b", re.IGNORECASE)
_CONTACT_PATH = re.compile(r"contact|kontakt|contacto|contatti|nous-contacter|get-in-touch|enquir|devis|request-quote", re.IGNORECASE)
_NEGATIVE_PATH = re.compile(
    r"/(?:blog|news|actualit|recette|recipe|rezept|press|presse|careers?|jobs?|emploi|carrieres?|privacy|cookie|"
    r"legal|mentions|terms|cgv|sitemap|search|recherche|login|cart|panier|checkout|tag|events?|salons?)\b",
    re.IGNORECASE,
)
_DATASHEET_HINT = re.compile(
    r"fiche technique|data ?sheet|technical (?:data )?sheet|spec(?:ification)? sheet|catalogue|catalog|brochure|product sheet",
    re.IGNORECASE,
)


@dataclass
class ResultFacts:
    url: str
    title: str
    snippet: str
    official: bool
    kinds: set[str] = field(default_factory=set)
    product_level: Level = Level.NOT_FOUND
    industrial_strength: int = 0  # 0 aucun, 1 indice faible, 2 indice fort
    private_label: bool = False
    foodservice: bool = False
    distributor_hits: int = 0
    certifications: list[str] = field(default_factory=list)
    capacity: str | None = None
    weights_g: list[float] = field(default_factory=list)


def _snippet(content: str, limit: int = 220) -> str:
    return re.sub(r"\s+", " ", content).strip()[:limit]


def _family_relevance(plan: QueryPlan, title_url_norm: str, content_norm: str) -> Level:
    if plan.families:
        terms = [t for fam in plan.families for t in fam.terms]
        if any(contains_phrase(title_url_norm, t) for t in terms):
            return Level.CONFIRMED
        if any(contains_phrase(content_norm, t) for t in terms):
            return Level.PROBABLE
        return Level.NOT_FOUND
    if plan.tokens:
        in_title = sum(contains_phrase(title_url_norm, t) for t in plan.tokens) / len(plan.tokens)
        in_content = sum(contains_phrase(content_norm, t) for t in plan.tokens) / len(plan.tokens)
        if in_title >= 0.5:
            return Level.CONFIRMED
        if in_content >= 0.5:
            return Level.PROBABLE
    return Level.NOT_FOUND


def classify_result(result: dict[str, Any], domain: str, plan: QueryPlan) -> ResultFacts:
    url = str(result.get("url", ""))
    title = str(result.get("title", ""))
    content = str(result.get("content", "") or result.get("raw_content", ""))
    facts = ResultFacts(url=url, title=title, snippet=_snippet(content), official=registered_domain(url) == domain)
    if not facts.official:
        return facts
    path = urlparse(url).path
    blob = f"{title}\n{content}"
    facts.distributor_hits = len(_DISTRIBUTOR.findall(blob))
    if _STRONG_INDUSTRIAL.search(blob):
        facts.industrial_strength = 2
    elif _WEAK_INDUSTRIAL.search(blob):
        facts.industrial_strength = 1
    if facts.industrial_strength:
        facts.kinds.add("industriel")
    facts.private_label = bool(_PRIVATE_LABEL.search(blob))
    facts.foodservice = bool(_FOODSERVICE.search(blob))
    facts.certifications = sorted({m.upper().replace("  ", " ") for m in _CERTIFICATIONS.findall(blob)})
    capacity = _CAPACITY.search(blob)
    facts.capacity = capacity.group(0).strip() if capacity else None
    if _CONTACT_PATH.search(path) or contains_phrase(normalize(title), "contact"):
        facts.kinds.add("contact")
    if path.lower().endswith(".pdf") or _DATASHEET_HINT.search(title) or _DATASHEET_HINT.search(path):
        facts.kinds.add("fiche technique")
    deep = not is_homepage(url) and not _NEGATIVE_PATH.search(path) and "contact" not in facts.kinds
    if deep:
        level = _family_relevance(plan, normalize(f"{title} {path}"), normalize(content))
        if level is not Level.NOT_FOUND:
            facts.kinds.add("produit")
            facts.product_level = level
            facts.weights_g = extract_weights(blob)
    return facts
