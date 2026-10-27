"""Recherche dans les catalogues indexés avec un score *explicable*.

Le score est une somme de composantes affichées une à une (jamais un pourcentage opaque). Il est exprimé en
« points / maximum applicable » : une composante non pertinente pour la requête (pas de grammage demandé, pas de
photo…) n'entre pas dans le maximum.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sourcing_images import ImageDescriptor
from sourcing_store import IndexStore, PassageRow
from sourcing_textutils import QueryPlan, contains_term, detect_families, matched_terms, normalize

MAX_POINTS = {
    "termes du besoin": 30.0,
    "pertinence texte (BM25)": 25.0,
    "famille produit / synonymes": 15.0,
    "grammage": 15.0,
    "référence / conditionnement": 5.0,
    "critères (surgelé, pur beurre…)": 5.0,
    "fiche produit structurée": 5.0,
    "similarité visuelle": 25.0,
}
COMMERCIAL_MODIFIERS = frozenset({"private_label", "foodservice", "assortiment"})


@dataclass(frozen=True)
class SearchFilters:
    suppliers: tuple[str, ...] = ()
    min_weight: float | None = None
    max_weight: float | None = None
    only_structured: bool = False


@dataclass(frozen=True)
class ScoringConfig:
    weight_tolerance: float = 0.15
    top_candidates: int = 400
    visual_candidates: int = 80


@dataclass
class InternalHit:
    key: str
    passage_id: int
    public_id: str
    supplier: str
    catalog: str
    page_label: str
    original_page: int
    local_page: int
    title: str
    excerpt: str
    refs: list[str]
    weights_g: list[float]
    packaging: list[str]
    structured: bool
    score: float
    max_score: float
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    duplicates: int = 0
    visual_similarity: float | None = None
    thumb: bytes | None = None
    content_hash: str = ""

    @property
    def score_label(self) -> str:
        return f"{self.score:.0f} / {self.max_score:.0f}"


def _token_present(token: str, tokens: set[str]) -> bool:
    return token in tokens or f"{token}s" in tokens or (token.endswith("s") and token[:-1] in tokens)


def page_label(row: PassageRow) -> str:
    if row.page_offset:
        part = f"partie {row.part_index}, " if row.part_index else ""
        return f"p. {row.original_page} ({part}p. {row.local_page} du fichier)"
    return f"p. {row.original_page}"


def score_passage(row: PassageRow, plan: QueryPlan, config: ScoringConfig, max_bm25: float, visual: float | None,
                  has_text_query: bool, has_image: bool) -> tuple[dict[str, float], dict[str, float], list[str], float]:
    """Retourne ``(composantes, pénalités, notes, maximum_applicable)``."""
    components: dict[str, float] = {}
    penalties: dict[str, float] = {}
    notes: list[str] = []
    applicable = 0.0
    pnorm = normalize(f"{row.title} {row.text}")
    ptokens = set(pnorm.split())
    ttokens = set(normalize(row.title).split())

    if plan.tokens:
        applicable += MAX_POINTS["termes du besoin"]
        found = [t for t in plan.tokens if _token_present(t, ptokens)]
        in_title = [t for t in found if _token_present(t, ttokens)]
        components["termes du besoin"] = round(30 * (0.75 * len(found) / len(plan.tokens) + 0.25 * len(in_title) / len(plan.tokens)), 1)
        missing = [t for t in plan.tokens if t not in found]
        notes.append(f"termes trouvés : {len(found)}/{len(plan.tokens)}" + (f" (absents : {', '.join(missing[:4])})" if missing else ""))

    if has_text_query:
        applicable += MAX_POINTS["pertinence texte (BM25)"]
        components["pertinence texte (BM25)"] = round(25 * row.bm25 / max_bm25, 1) if max_bm25 > 0 and row.bm25 > 0 else 0.0

    if plan.families:
        applicable += MAX_POINTS["famille produit / synonymes"]
        matched = [f for f in plan.specific_families if any(contains_term(pnorm, t) for t in f.terms)]
        if matched:
            components["famille produit / synonymes"] = 15.0
            literal = {t for f in matched for term in matched_terms(pnorm, f.terms) for t in term.split()} & set(plan.tokens)
            notes.append("famille « " + matched[0].label_fr + " » reconnue" + ("" if literal else " via un synonyme multilingue"))
        else:
            components["famille produit / synonymes"] = 0.0
            others = [f for f in detect_families(pnorm) if not f.generic and f not in plan.families]
            if others:
                penalties["autre famille produit (" + others[0].label_fr + ")"] = 6.0

    if plan.weight_g:
        applicable += MAX_POINTS["grammage"]
        weights = row.unit_weights_g or row.weights_g
        if not weights:
            components["grammage"] = 0.0
            notes.append("aucun grammage dans ce passage")
        else:
            target = plan.weight_g
            best = min(weights, key=lambda w: abs(w - target))
            gap = abs(best - target) / target
            tolerance = config.weight_tolerance
            if gap <= 0.01:
                points = 15.0
            elif gap <= tolerance:
                points = 15 * (1 - 0.6 * gap / tolerance)
            elif gap <= 2 * tolerance:
                points = 3.0
            else:
                points = 0.0
                if all(abs(w - target) / target > 1.5 for w in weights):
                    penalties["grammage incompatible"] = 4.0
            components["grammage"] = round(points, 1)
            notes.append(f"{best:g} g trouvé pour une cible de {target:g} g (écart {gap * 100:.0f} %)")

    if plan.references or plan.packaging:
        applicable += MAX_POINTS["référence / conditionnement"]
        points = 0.0
        if any(r.lower() in (x.lower() for x in row.refs) or r.lower() in pnorm.replace(" ", "") for r in plan.references):
            points += 5.0
            notes.append("référence exacte trouvée")
        wanted = {re.sub(r"\D", "", p) for p in plan.packaging if re.sub(r"\D", "", p)}
        have = {re.sub(r"\D", "", p) for p in row.packaging}
        if wanted & have:
            points += 3.0
            notes.append("conditionnement identique")
        components["référence / conditionnement"] = min(points, 5.0)

    content_modifiers = [m for m in plan.modifiers if m.id not in COMMERCIAL_MODIFIERS]
    if content_modifiers:
        applicable += MAX_POINTS["critères (surgelé, pur beurre…)"]
        hits = [m for m in content_modifiers if any(contains_term(pnorm, t) for t in m.terms)]
        components["critères (surgelé, pur beurre…)"] = round(5 * len(hits) / len(content_modifiers), 1)
        if hits:
            notes.append("critères présents : " + ", ".join(m.label_fr for m in hits))

    applicable += MAX_POINTS["fiche produit structurée"]
    components["fiche produit structurée"] = 5.0 if row.structured else (2.0 if row.kind == "product" else 0.0)

    if has_image:
        applicable += MAX_POINTS["similarité visuelle"]
        components["similarité visuelle"] = round(25 * visual, 1) if visual else 0.0
        if visual is None:
            notes.append("aucune photo rattachée à ce passage")

    letters = sum(c.isalpha() for c in row.text)
    if len(ptokens) < 6 or letters < 25:
        penalties["texte trop pauvre en indices produit"] = 5.0
    return components, penalties, notes, applicable


def _excerpt(text: str, limit: int = 280) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _passes_filters(row: PassageRow, filters: SearchFilters) -> bool:
    if filters.suppliers and row.supplier not in filters.suppliers:
        return False
    if filters.only_structured and not row.structured:
        return False
    if filters.min_weight or filters.max_weight:
        low, high = filters.min_weight or 0.0, filters.max_weight or float("inf")
        if not any(low <= w <= high for w in row.weights_g):
            return False
    return True


def search_internal(store: IndexStore, plan: QueryPlan, filters: SearchFilters = SearchFilters(), config: ScoringConfig = ScoringConfig(),
                    *, top_k: int = 30, image: ImageDescriptor | None = None) -> list[InternalHit]:
    """Recherche texte et/ou image. Retourne les ``top_k`` meilleurs passages dédupliqués, avec score détaillé."""
    has_text = not plan.is_empty
    if not has_text and image is None:
        return []
    candidates: dict[int, PassageRow] = {}
    if has_text:
        for row in store.search_candidates(plan.search_terms, suppliers=filters.suppliers, limit=config.top_candidates):
            candidates[row.id] = row
    visual: dict[int, tuple[float, int]] = {}
    if image is not None:
        for match in store.visual_matches(image, top_k=config.visual_candidates, suppliers=filters.suppliers):
            if match.passage_id is not None and match.similarity > visual.get(match.passage_id, (0.0, 0))[0]:
                visual[match.passage_id] = (match.similarity, match.image_id)
        missing = [pid for pid in visual if pid not in candidates]
        for row in store.get_passages(missing):
            candidates[row.id] = row
    max_bm25 = max((r.bm25 for r in candidates.values()), default=0.0)

    scored: list[tuple[float, InternalHit]] = []
    for row in candidates.values():
        if not _passes_filters(row, filters):
            continue
        sim, image_id = visual.get(row.id, (None, 0))
        components, penalties, notes, applicable = score_passage(row, plan, config, max_bm25, sim, has_text, image is not None)
        total = max(0.0, sum(components.values()) - sum(penalties.values()))
        hit = InternalHit(
            key=str(row.id), passage_id=row.id, public_id=row.public_id, supplier=row.supplier or "Fournisseur inconnu",
            catalog=row.logical_name.rsplit("/", 1)[-1], page_label=page_label(row), original_page=row.original_page,
            local_page=row.local_page, title=row.title, excerpt=_excerpt(row.text), refs=row.refs, weights_g=row.weights_g,
            packaging=row.packaging, structured=row.structured, score=round(total, 1), max_score=applicable, components=components,
            penalties=penalties, notes=notes, visual_similarity=sim, content_hash=row.content_hash)
        hit.thumb = store.image_thumb(image_id) if image_id else None
        scored.append((total + row.bm25 * 1e-6, hit))
    scored.sort(key=lambda item: -item[0])

    hits: list[InternalHit] = []
    by_hash: dict[str, InternalHit] = {}
    for _, hit in scored:
        original = by_hash.get(hit.content_hash)
        if original is not None:
            original.duplicates += 1
            continue
        by_hash[hit.content_hash] = hit
        hits.append(hit)
        if len(hits) >= top_k:
            break
    missing_thumbs = [h.passage_id for h in hits if h.thumb is None]
    thumbs = store.thumbs_for_passages(missing_thumbs)
    for hit in hits:
        if hit.thumb is None:
            hit.thumb = thumbs.get(hit.passage_id)
    return hits
