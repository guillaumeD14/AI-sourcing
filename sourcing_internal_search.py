"""Recherche interne dans l'index des catalogues, avec score explicable.

1. Récupération des candidats : FTS5/BM25 (ou repli LIKE + BM25 Python si FTS5 est absent).
2. Re-classement transparent : chaque composante est calculée séparément et affichée.

Le score est un nombre de points sur un maximum affiché (``score / max_score``), jamais un
pourcentage présenté comme une certitude.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from sourcing_store import IndexStore
from sourcing_textutils import (
    QueryPlan,
    build_query_plan,
    contains_phrase,
    families_in_text,
    normalize,
    normalize_ref,
    packaging_numbers,
    weight_proximity,
)


@dataclass(frozen=True)
class ScoringConfig:
    max_exact: float = 30.0
    max_bm25: float = 25.0
    max_family: float = 20.0
    max_weight: float = 15.0
    max_reference: float = 10.0
    max_packaging: float = 5.0
    max_structure: float = 5.0
    penalty_no_clues: float = 10.0
    penalty_boilerplate: float = 10.0
    penalty_short: float = 5.0
    penalty_weight_conflict: float = 8.0
    weight_tolerance: float = 0.15  # écart relatif au-delà duquel le grammage ne compte plus
    candidate_limit: int = 400


@dataclass(frozen=True)
class SearchFilters:
    suppliers: tuple[str, ...] = ()
    min_weight_g: float | None = None
    max_weight_g: float | None = None
    only_structured: bool = False


@dataclass
class InternalHit:
    key: str
    supplier: str
    catalog: str
    page: int | None
    page_label: str
    title: str
    excerpt: str
    refs: list[str]
    weights_g: list[float]
    packaging: list[str]
    score: float
    max_score: float
    components: dict[str, float]
    penalties: dict[str, float]
    notes: list[str]
    pdf_url: str | None
    public_id: str = ""
    local_page: int = 1
    duplicates: int = 0

    @property
    def score_label(self) -> str:
        return f"{self.score:.0f} / {self.max_score:.0f} pts"


@dataclass
class _Candidate:
    row: dict[str, Any]
    bm25: float = 0.0
    refs: list[str] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)
    packaging: list[str] = field(default_factory=list)


def _stem(token: str) -> str:
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


# ---------------------------------------------------------------------------
# Candidats
# ---------------------------------------------------------------------------


def _match_terms(plan: QueryPlan) -> list[str]:
    terms: list[str] = []
    for token in plan.tokens:
        terms.extend({token, _stem(token)})
    terms.extend(plan.synonyms[:40])
    terms.extend(r.lower() for r in plan.references)
    return list(dict.fromkeys(t for t in terms if t))


def _fts_expression(terms: Sequence[str]) -> str:
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms)


_SELECT = """SELECT p.id AS pid, p.text, p.norm_text, p.title, p.title_norm, p.refs, p.weights, p.packaging,
       p.structured, p.boilerplate, p.local_page, f.supplier, f.display_name, f.logical_key, f.part_index,
       f.part_total, f.page_offset, f.secure_url, f.public_id"""


def _filter_sql(filters: SearchFilters) -> tuple[str, list[Any]]:
    clause, args = " AND f.status='indexed'", []
    if filters.suppliers:
        clause += " AND f.supplier IN (%s)" % ",".join("?" * len(filters.suppliers))
        args.extend(filters.suppliers)
    if filters.only_structured:
        clause += " AND p.structured=1"
    return clause, args


def _fetch_fts(store: IndexStore, terms: list[str], filters: SearchFilters, limit: int) -> list[_Candidate]:
    clause, args = _filter_sql(filters)
    sql = (
        f"{_SELECT}, -bm25(passages_fts, 1.0, 3.0) AS rank FROM passages_fts "
        "JOIN passages p ON p.id = passages_fts.rowid JOIN files f ON f.id = p.file_id "
        f"WHERE passages_fts MATCH ?{clause} ORDER BY rank DESC LIMIT ?"
    )
    with store.connect() as con:
        rows = con.execute(sql, [_fts_expression(terms), *args, limit]).fetchall()
    return [_to_candidate(dict(r), float(r["rank"])) for r in rows]


def _fetch_like(store: IndexStore, terms: list[str], filters: SearchFilters, limit: int) -> list[_Candidate]:
    clause, args = _filter_sql(filters)
    conditions = " OR ".join("p.norm_text LIKE ?" for _ in terms)
    sql = (
        f"{_SELECT} FROM passages p JOIN files f ON f.id = p.file_id WHERE ({conditions}){clause} LIMIT ?"
    )
    with store.connect() as con:
        rows = con.execute(sql, [*(f"%{t}%" for t in terms), *args, limit * 5]).fetchall()
    candidates = [_to_candidate(dict(r), 0.0) for r in rows]
    _bm25_in_python(candidates, terms)
    candidates.sort(key=lambda c: -c.bm25)
    return candidates[:limit]


def _bm25_in_python(candidates: list[_Candidate], terms: list[str], k1: float = 1.5, b: float = 0.75) -> None:
    if not candidates:
        return
    lengths = [max(len(c.row["norm_text"].split()), 1) for c in candidates]
    avg = sum(lengths) / len(lengths)
    n_docs = len(candidates)
    for term in terms:
        df = sum(1 for c in candidates if contains_phrase(c.row["norm_text"], term))
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for cand, length in zip(candidates, lengths):
            tf = f" {cand.row['norm_text']} ".count(f" {term} ")
            if tf:
                cand.bm25 += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))


def _to_candidate(row: dict[str, Any], bm25: float) -> _Candidate:
    return _Candidate(
        row=row, bm25=bm25, refs=json.loads(row["refs"]), weights=json.loads(row["weights"]),
        packaging=json.loads(row["packaging"]),
    )


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


def _score(plan: QueryPlan, cand: _Candidate, cfg: ScoringConfig, bm25_max: float) -> InternalHit:
    row = cand.row
    norm, title_norm = row["norm_text"], row["title_norm"]
    text_stems = {_stem(t) for t in norm.split()}
    title_stems = {_stem(t) for t in title_norm.split()}
    components: dict[str, float] = {}
    maxima: dict[str, float] = {}
    notes: list[str] = []

    if plan.tokens:
        maxima["mots exacts"] = cfg.max_exact
        per_token = [1.0 if _stem(t) in title_stems else 0.7 if _stem(t) in text_stems else 0.0 for t in plan.tokens]
        exact = sum(per_token) / len(per_token)
        if len(plan.tokens) > 1 and contains_phrase(norm, plan.norm_phrase):
            exact = 1.0
        components["mots exacts"] = exact * cfg.max_exact
        hit_words = [t for t, v in zip(plan.tokens, per_token) if v]
        notes.append(f"mots exacts : {len(hit_words)}/{len(plan.tokens)} ({', '.join(hit_words) or 'aucun'})")

    terms_present = [t for t in plan.tokens if _stem(t) in text_stems] + [
        r for r in plan.references if normalize_ref(r) in normalize_ref(row["text"])
    ]
    total_terms = len(plan.tokens) + len(plan.references)
    coverage = len(terms_present) / total_terms if total_terms else 1.0
    maxima["proximité BM25"] = cfg.max_bm25
    components["proximité BM25"] = (cand.bm25 / bm25_max if bm25_max > 0 else 0.0) * coverage * cfg.max_bm25
    notes.append(f"BM25 relatif {cand.bm25 / bm25_max if bm25_max > 0 else 0:.2f} × couverture {coverage:.2f}")

    if plan.families:
        maxima["famille / synonymes"] = cfg.max_family
        passage_families = families_in_text(norm)
        passage_keys = {f.key for f in passage_families}
        same = [f.label_fr for f in plan.families if f.key in passage_keys]
        category = any(f.category in {pf.category for pf in passage_families} for f in plan.families)
        base = 1.0 if same else 0.4 if category else 0.0
        if plan.modifiers and base:
            from sourcing_textutils import MODIFIERS

            found = sum(
                any(contains_phrase(norm, normalize(v)) for v in MODIFIERS[m]) for m in plan.modifiers
            )
            base *= 0.75 + 0.25 * found / len(plan.modifiers)
        components["famille / synonymes"] = base * cfg.max_family
        notes.append("famille : " + (", ".join(same) if same else "même catégorie seulement" if category else "aucune"))

    penalties: dict[str, float] = {}
    if plan.weight_g:
        maxima["grammage"] = cfg.max_weight
        closeness = weight_proximity(plan.weight_g, cand.weights, cfg.weight_tolerance)
        components["grammage"] = closeness * cfg.max_weight
        if cand.weights:
            notes.append(f"grammage : {', '.join(f'{w:g} g' for w in cand.weights)} (cible {plan.weight_g:g} g)")
            if closeness == 0.0:
                penalties["grammage incompatible"] = cfg.penalty_weight_conflict
        else:
            notes.append("grammage : non indiqué dans ce passage")

    if plan.references:
        maxima["référence"] = cfg.max_reference
        wanted = {normalize_ref(r) for r in plan.references}
        have = {normalize_ref(r) for r in cand.refs}
        components["référence"] = cfg.max_reference if wanted & have else (
            0.7 * cfg.max_reference if any(w in normalize_ref(row["text"]) for w in wanted) else 0.0
        )
        notes.append("référence : " + ("trouvée" if components["référence"] else "absente"))

    if plan.packaging_numbers:
        maxima["conditionnement"] = cfg.max_packaging
        match = set(plan.packaging_numbers) & packaging_numbers(cand.packaging)
        components["conditionnement"] = cfg.max_packaging if match else 0.0
        notes.append("conditionnement : " + (", ".join(cand.packaging) if match else "différent ou absent"))

    clue_count = sum(bool(x) for x in (cand.weights, cand.refs, cand.packaging))
    maxima["bloc produit structuré"] = cfg.max_structure
    structure = 1.0 if row["structured"] and clue_count >= 2 else 0.6 if row["structured"] else 0.3 if clue_count else 0.0
    components["bloc produit structuré"] = structure * cfg.max_structure

    if clue_count == 0 and not row["structured"]:
        penalties["indices produit insuffisants"] = cfg.penalty_no_clues
    if row["boilerplate"]:
        penalties["texte non produit (sommaire, mentions)"] = cfg.penalty_boilerplate
    if len(norm) < 30:
        penalties["passage très court"] = cfg.penalty_short

    total = max(0.0, sum(components.values()) - sum(penalties.values()))
    page = (row["page_offset"] + row["local_page"]) if row["page_offset"] is not None else None
    part = f"partie {row['part_index']}/{row['part_total']}, " if row["part_total"] > 1 else ""
    page_label = f"p. {page}" if page is not None else f"p. ? ({part}p. {row['local_page']} du fichier)"
    if page is not None and part:
        page_label += f" ({part}p. {row['local_page']} du fichier)"
    url = f"{row['secure_url']}#page={row['local_page']}" if row.get("secure_url") else None
    excerpt = re.sub(r"\s*\n\s*", " | ", row["text"])[:300]
    key = hashlib.sha1(f"{row['logical_key']}|{page}|{row['local_page']}|{row['title_norm']}|{row['pid']}".encode()).hexdigest()[:12]
    return InternalHit(
        key=key, supplier=row["supplier"], catalog=row["display_name"], page=page, page_label=page_label,
        title=row["title"] or "(sans titre)", excerpt=excerpt, refs=cand.refs, weights_g=cand.weights,
        packaging=cand.packaging, score=round(total, 1), max_score=round(sum(maxima.values()), 1),
        components={k: round(v, 1) for k, v in components.items()}, penalties=penalties, notes=notes, pdf_url=url,
        public_id=row["public_id"], local_page=row["local_page"],
    )


def _dedupe(hits: list[InternalHit]) -> list[InternalHit]:
    best: dict[str, InternalHit] = {}
    for hit in hits:
        ident = hit.refs[0] if hit.refs else normalize(hit.title) + "|" + ",".join(f"{w:g}" for w in hit.weights_g)
        dedupe_key = f"{hit.supplier.lower()}|{ident}"
        kept = best.get(dedupe_key)
        if kept is None:
            best[dedupe_key] = hit
        else:
            winner, loser = (hit, kept) if hit.score > kept.score else (kept, hit)
            winner.duplicates = kept.duplicates + hit.duplicates + 1
            best[dedupe_key] = winner
    return list(best.values())


def _passes_weight_filter(hit: InternalHit, filters: SearchFilters) -> bool:
    if filters.min_weight_g is None and filters.max_weight_g is None:
        return True
    low = filters.min_weight_g if filters.min_weight_g is not None else 0.0
    high = filters.max_weight_g if filters.max_weight_g is not None else float("inf")
    return any(low <= w <= high for w in hit.weights_g)


def search_internal(
    store: IndexStore,
    query: str | QueryPlan,
    filters: SearchFilters | None = None,
    config: ScoringConfig | None = None,
    *,
    top_k: int = 30,
    force_fallback: bool = False,
) -> list[InternalHit]:
    """Cherche ``query`` (ex. « Mini Danish 40 g ») dans l'index et retourne les meilleurs passages."""
    cfg = config or ScoringConfig()
    flt = filters or SearchFilters()
    plan = query if isinstance(query, QueryPlan) else build_query_plan(query)
    terms = _match_terms(plan)
    if plan.is_empty or not terms:
        return []
    use_fts = store.fts_available and not force_fallback
    candidates = (_fetch_fts if use_fts else _fetch_like)(store, terms, flt, cfg.candidate_limit)
    if not candidates:
        return []
    bm25_max = max(c.bm25 for c in candidates)
    hits = [_score(plan, c, cfg, bm25_max) for c in candidates]
    hits = [h for h in hits if _passes_weight_filter(h, flt)]
    hits = _dedupe(hits)
    hits.sort(key=lambda h: (-h.score, h.supplier, h.page or 0))
    return hits[:top_k]
