"""Recherche Internet de fabricants industriels (Tavily), adaptée à chaque demande.

Aucun fournisseur n'est connu à l'avance : les requêtes sont composées à partir du besoin
(produit, grammage, attributs, pays demandés, langues locales), puis affinées avec les termes
qui reviennent dans les premiers résultats. Les fournisseurs déjà référencés (liste de
l'acheteur) sont écartés avant de dépenser du quota. Ce sont des heuristiques déterministes,
pas un modèle de langage : aucune API payante.

Phases : 1 besoin → 2 découverte de domaines (+ affinage par les résultats) → 3 mise à l'écart des
fournisseurs référencés → 4 recherche ciblée sur domaines officiels → 5 preuve industrielle →
6 page produit / fiche technique → 7 contact → 8 scoring et déduplication.

Quota gratuit : un budget de requêtes strict (``WebSearchOptions.max_queries``) est appliqué.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Protocol

import requests

from sourcing_domains import (
    COUNTRY_EN, COUNTRY_LANG, EXCLUDE_DOMAINS, EUROPE_COUNTRIES, MANUFACTURER_WORD, company_from_titles,
    country_from_domain, is_noise_domain, is_non_european_tld, registered_domain,
)
from sourcing_errors import AuthError, QuotaError, ServiceError, SourcingError, redact
from sourcing_http_utils import request_with_retry
from sourcing_manufacturers import VerifiedManufacturer, select_seeds
from sourcing_models import SupplierCandidate
from sourcing_suppliers import SupplierDirectory
from sourcing_textutils import CATEGORY_LABELS, MODIFIER_QUERY, STOPWORDS, QueryPlan, normalize
from sourcing_web_classify import ResultFacts, classify_result
from sourcing_web_scoring import build_candidate

logger = logging.getLogger(__name__)
TAVILY_URL = "https://api.tavily.com/search"
_MANUFACTURER_CUES = ("manufactur", "fabricant", "factory", "usine", "industrial", "industriel", "foodservice", "private label")


class Searcher(Protocol):
    def search(self, query: str, *, include_domains: list[str] | None = None,
               exclude_domains: list[str] | None = None, max_results: int = 8) -> list[dict[str, Any]]: ...


class TavilyClient:
    def __init__(self, api_key: str, *, session_factory: Callable[[], requests.Session] = requests.Session,
                 timeout: tuple[float, float] = (5.0, 25.0), retries: int = 2,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise AuthError("Clé Tavily absente : renseignez TAVILY_API_KEY dans les secrets.")
        self._key = api_key
        self._factory = session_factory
        self._local = threading.local()
        self._timeout, self._retries, self._sleep = timeout, retries, sleep

    def __repr__(self) -> str:
        return "TavilyClient(api_key=***)"

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "s"):
            self._local.s = self._factory()
        return self._local.s

    def search(self, query: str, *, include_domains: list[str] | None = None,
               exclude_domains: list[str] | None = None, max_results: int = 8) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"query": query[:380], "search_depth": "basic", "max_results": max_results,
                                   "include_answer": False, "include_raw_content": False}
        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains
        response = request_with_retry(
            self._session(), "POST", TAVILY_URL, service="Tavily", timeout=self._timeout, retries=self._retries,
            sleep=self._sleep, json=payload, headers={"Authorization": f"Bearer {self._key}"},
        )
        status = response.status_code
        if status in (401, 403):
            raise AuthError("Tavily refuse la clé API (401). Vérifiez TAVILY_API_KEY.")
        if status in (429, 432, 433):
            raise QuotaError("Quota Tavily atteint : attendez le renouvellement mensuel ou réduisez le nombre de recherches.")
        if status >= 400:
            raise ServiceError(redact(f"Tavily a répondu {status}.", [self._key]))
        try:
            results = response.json().get("results", [])
        except (ValueError, AttributeError) as exc:
            raise ServiceError("Réponse Tavily illisible.") from exc
        return [r for r in results if isinstance(r, dict) and r.get("url")]


@dataclass
class WebSearchOptions:
    max_queries: int = 18
    max_domains: int = 6
    max_seeds: int = 3
    discovery_queries: int = 4
    feedback_queries: int = 2
    discovery_results: int = 10
    domain_results: int = 6
    workers: int = 3
    include_known: bool = False  # analyser aussi les fournisseurs déjà référencés
    europe_only: bool = True
    exclude_domains: tuple[str, ...] = EXCLUDE_DOMAINS[:100]


@dataclass
class WebSearchReport:
    candidates: list[SupplierCandidate] = field(default_factory=list)
    queries_used: int = 0
    domains_examined: int = 0
    excluded_results: int = 0
    known_skipped: list[tuple[str, str, str]] = field(default_factory=list)  # (entreprise, domaine, fournisseur référencé)
    learned_terms: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_on: str = ""


class _Budget:
    def __init__(self, limit: int) -> None:
        self.left, self.used, self._lock = limit, 0, threading.Lock()

    def halt(self) -> None:
        """Stoppe les requêtes restantes (clé refusée ou quota atteint)."""
        with self._lock:
            self.left = 0

    def take(self) -> bool:
        with self._lock:
            if self.left <= 0:
                return False
            self.left -= 1
            self.used += 1
            return True


@dataclass
class _DomainAgg:
    hits: int = 0
    score: float = 0.0
    text: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Composition des requêtes à partir de la demande
# ---------------------------------------------------------------------------


def _modifier_words(plan: QueryPlan, index: int) -> str:
    """Attributs de la demande (surgelé, bio, private label...) dans la langue voulue (0 = EN, 1 = FR)."""
    present = normalize(plan.product_text)
    words = [MODIFIER_QUERY[m][index] for m in plan.modifiers if m in MODIFIER_QUERY]
    return " ".join(w for w in words if normalize(w) not in present)


def _regions(plan: QueryPlan) -> tuple[str, str]:
    if not plan.countries:
        return "Europe", "Europe"
    return ", ".join(COUNTRY_EN.get(c, c) for c in plan.countries), ", ".join(plan.countries)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_discovery_queries(plan: QueryPlan, limit: int = 4) -> list[str]:
    """Requêtes de découverte (anglais, français, catégorie, langues locales) adaptées à la demande."""
    product = plan.product_text or plan.raw
    weight = f" {plan.weight_g:g} g" if plan.weight_g else ""
    category = plan.categories[0] if plan.categories else "autre"
    cat_fr, cat_en = CATEGORY_LABELS.get(category, CATEGORY_LABELS["autre"])
    region_en, region_fr = _regions(plan)
    languages = [COUNTRY_LANG[c] for c in plan.countries if COUNTRY_LANG.get(c) not in (None, "en", "fr")]
    local_words = " ".join(dict.fromkeys(MANUFACTURER_WORD[lang] for lang in (languages or ["de", "es", "it"])))
    asked_pl = "private label" in plan.modifiers  # déjà dans la demande : ne pas le répéter
    queries = [
        f"{product}{weight} {_modifier_words(plan, 0)} manufacturer {region_en} factory foodservice {'' if asked_pl else 'private label'}",
        f"{product}{weight} {_modifier_words(plan, 1)} fabricant industriel {region_fr} usine foodservice {'' if asked_pl else 'marque distributeur'}",
        f"{cat_en} industrial manufacturer {region_en} {product} co-manufacturing B2B",
        f"{product}{weight} {local_words} industrie {region_en}",
    ]
    return list(dict.fromkeys(_squash(q) for q in queries))[:limit]


def build_feedback_queries(plan: QueryPlan, terms: list[str]) -> list[str]:
    weight = f" {plan.weight_g:g} g" if plan.weight_g else ""
    region_en, _ = _regions(plan)
    pl = "" if "private label" in plan.modifiers else "private label"
    return [_squash(f"{term}{weight} manufacturer {region_en} foodservice {pl}") for term in terms]


_NOISE_WORDS = frozenset(
    "manufacturer manufacturers fabricant producer products product produits produit official site home page range gamme "
    "catalogue catalog contact about news com www the our your all new best buy shop online price".split()
)


def learn_terms(results: Iterable[dict[str, Any]], plan: QueryPlan, limit: int = 2) -> list[str]:
    """Variantes du produit observées dans les premiers résultats (« danish pastries », « mini danish mix »...).

    Un n-gramme de 2 à 3 mots est retenu s'il contient un mot de la demande, apparaît dans au moins deux
    résultats différents et ne contient ni mot vide ni vocabulaire générique. Les variantes servent à
    relancer des requêtes plus précises ; elles ne sont jamais affichées comme des preuves.
    """
    if not plan.tokens:
        return []
    counts: dict[str, int] = {}
    request_tokens = set(plan.tokens)
    for result in results:
        text = f"{result.get('title', '')}. {str(result.get('content', ''))[:200]}"
        seen: set[str] = set()
        for segment in re.split(r"[|.;:!?\n–—]+|\s-\s", text):  # un n-gramme ne traverse ni titre/contenu ni phrase
            tokens = normalize(segment).split()
            for size in (2, 3):
                for i in range(len(tokens) - size + 1):
                    gram = tokens[i : i + size]
                    if not request_tokens & set(gram) or any(
                        t in STOPWORDS or t in _NOISE_WORDS or t.isdigit() or len(t) < 3 for t in gram
                    ):
                        continue
                    seen.add(" ".join(gram))
        for gram in seen:
            counts[gram] = counts.get(gram, 0) + 1
    known = plan.norm_phrase
    ranked = sorted(((c, g) for g, c in counts.items() if c >= 2 and g != known and g not in known), key=lambda cg: (-cg[0], len(cg[1])))
    chosen: list[str] = []
    for _, gram in ranked:
        if not any(gram in other or other in gram for other in chosen):
            chosen.append(gram)
        if len(chosen) == limit:
            break
    return chosen


def _domain_queries(plan: QueryPlan) -> tuple[str, str]:
    product = plan.product_text or plan.raw
    weight = f" {plan.weight_g:g} g" if plan.weight_g else ""
    return (
        _squash(f"{product}{weight} fiche technique datasheet catalogue produit"),
        "usines sites de production factory production sites Werk Produktion fábrica stabilimento private label foodservice contact",
    )


class WebSourcing:
    def __init__(self, searcher: Searcher, *, manufacturers: list[VerifiedManufacturer] | None = None,
                 known_suppliers: Iterable[str] = (), directory: SupplierDirectory | None = None,
                 options: WebSearchOptions | None = None, today: Callable[[], date] = date.today) -> None:
        self.searcher = searcher
        self.manufacturers = manufacturers or []
        self.directory = directory or SupplierDirectory(known_suppliers)
        self.options = options or WebSearchOptions()
        self._today = today

    # -- exécution d'une requête avec budget -----------------------------

    def _query(self, budget: _Budget, query: str, include: list[str] | None, max_results: int) -> list[dict[str, Any]] | None:
        if not budget.take():
            return None
        try:
            return self.searcher.search(
                query, include_domains=include, max_results=max_results,
                exclude_domains=None if include else list(self.options.exclude_domains),
            )
        except (AuthError, QuotaError):
            budget.halt()
            raise

    def _parallel(self, jobs: list[tuple[str, Any]], work: Callable[[Any], Any],
                  on_done: Callable[[], None], warnings: list[str]) -> list[tuple[str, Any]]:
        outcomes: list[tuple[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
            futures = {pool.submit(work, payload): label for label, payload in jobs}
            for future in as_completed(futures):
                label = futures[future]
                try:
                    outcomes.append((label, future.result()))
                except (AuthError, QuotaError) as exc:
                    warnings.append(str(exc))
                    outcomes.append((label, None))
                    raise
                except SourcingError as exc:
                    warnings.append(f"{label} : {exc}")
                    outcomes.append((label, None))
                on_done()
        return outcomes

    # -- pipeline ---------------------------------------------------------

    def run(self, plan: QueryPlan, progress: Callable[[str, float], None] | None = None) -> WebSearchReport:
        opts, today = self.options, self._today()
        report = WebSearchReport(checked_on=today.isoformat())
        budget = _Budget(opts.max_queries)
        notify = progress or (lambda label, fraction: None)
        if plan.is_empty:
            report.warnings.append("Besoin vide : saisissez un produit (ex. « Mini Danish 40 g »).")
            return report

        seeds = select_seeds(self.manufacturers, plan, opts.max_seeds)
        planned = opts.discovery_queries + opts.feedback_queries + 3 * opts.max_domains
        done = {"n": 0}

        def tick() -> None:
            done["n"] += 1
            notify("Recherche en cours…", min(0.95, done["n"] / max(planned, 1)))

        aggregates: dict[str, _DomainAgg] = {}
        discovery_results: list[dict[str, Any]] = []

        def absorb(outcomes: list[tuple[str, Any]]) -> None:
            for _, results in outcomes:
                for result in results or []:
                    discovery_results.append(result)
                    url = str(result.get("url", ""))
                    domain = registered_domain(url)
                    if is_noise_domain(url) or (opts.europe_only and is_non_european_tld(url)):
                        report.excluded_results += 1
                        continue
                    agg = aggregates.setdefault(domain, _DomainAgg())
                    text = f"{result.get('title', '')} {result.get('content', '')}"
                    agg.hits += 1
                    agg.score += float(result.get("score", 0) or 0)
                    agg.score += 0.3 * sum(cue in text.lower() for cue in _MANUFACTURER_CUES)
                    agg.score += 0.5 * any(t in normalize(text) for fam in plan.families for t in fam.terms)
                    agg.text.append(text[:600])
                    if result.get("title"):
                        agg.titles.append(str(result["title"]))

        # Phases 1-2 : besoin → découverte de domaines
        notify("Phase 1-2 : découverte de fabricants", 0.02)
        queries = build_discovery_queries(plan, opts.discovery_queries)
        report.queries.extend(queries)
        try:
            absorb(self._parallel([(q, q) for q in queries], lambda q: self._query(budget, q, None, opts.discovery_results),
                                  tick, report.warnings))
            # Affinage : variantes du produit observées dans les résultats, tant qu'il reste du budget pour la suite
            terms = learn_terms(discovery_results, plan, opts.feedback_queries)
            feedback = build_feedback_queries(plan, terms)
            if feedback and budget.left - len(feedback) >= 2 * opts.max_domains:
                report.learned_terms, _ = terms, report.queries.extend(feedback)
                absorb(self._parallel([(q, q) for q in feedback], lambda q: self._query(budget, q, None, opts.discovery_results),
                                      tick, report.warnings))
        except (AuthError, QuotaError):
            if not seeds:
                report.queries_used = budget.used
                return report

        # Phase 3 : fournisseurs déjà référencés mis à l'écart (aucune requête dépensée pour eux)
        def is_known(domain: str, titles: list[str]) -> str | None:
            company = company_from_titles(domain, titles)
            match = self.directory.match([company], domain, " ".join(titles))
            return match.supplier if match else None

        for domain, agg in list(aggregates.items()):
            known = is_known(domain, agg.titles)
            if known and not opts.include_known:
                report.known_skipped.append((company_from_titles(domain, agg.titles), domain, known))
                del aggregates[domain]
        seeds = [s for s in seeds if opts.include_known or not is_known(s.domain, [s.company])]

        seed_by_domain = {s.domain: s for s in seeds}
        ranked = sorted(aggregates.items(), key=lambda kv: -(kv[1].score + kv[1].hits))
        domains = list(seed_by_domain)
        domains += [d for d, _ in ranked if d not in seed_by_domain][: max(0, opts.max_domains - len(domains))]
        report.domains_examined = len(domains)

        # Phases 4-7 : recherche ciblée sur chaque domaine officiel
        notify("Phase 4-7 : preuves sur les sites officiels", 0.3)
        facts_by_domain: dict[str, list[ResultFacts]] = {d: [] for d in domains}

        def targeted(item: tuple[str, str]) -> tuple[str, list[dict[str, Any]] | None]:
            domain, query = item
            return domain, self._query(budget, query, [domain], opts.domain_results)

        q_product, q_industry = _domain_queries(plan)
        jobs = [(f"{d} · {label}", (d, q)) for d in domains for label, q in (("produit", q_product), ("industrie", q_industry))]
        try:
            for _, outcome in self._parallel(jobs, targeted, tick, report.warnings):
                if outcome:
                    domain, results = outcome
                    facts_by_domain[domain] += [classify_result(r, domain, plan) for r in results or []]
        except (AuthError, QuotaError):
            pass
        # Complément : contact manquant, si le budget le permet
        missing_contact = [d for d in domains if not any("contact" in f.kinds for f in facts_by_domain[d])]
        if missing_contact and budget.left > 0:
            def contact_work(domain: str) -> tuple[str, list[dict[str, Any]] | None]:
                return domain, self._query(budget, "contact nous contacter contact us sales B2B", [domain], 4)
            try:
                for _, outcome in self._parallel([(d, d) for d in missing_contact], contact_work, tick, report.warnings):
                    if outcome:
                        domain, results = outcome
                        facts_by_domain[domain] += [classify_result(r, domain, plan) for r in results or []]
            except (AuthError, QuotaError):
                pass

        # Phase 8 : scoring, déduplication
        notify("Phase 8 : scoring", 0.97)
        candidates: dict[str, SupplierCandidate] = {}
        for domain in domains:
            agg = aggregates.get(domain, _DomainAgg())
            candidate = build_candidate(
                domain, facts_by_domain[domain], plan, seed=seed_by_domain.get(domain), discovery_hits=agg.hits,
                discovery_text=" ".join(agg.text), directory=self.directory, today=today, titles=agg.titles,
            )
            if opts.europe_only:
                tld_country = country_from_domain(domain)
                if (candidate.country and candidate.country not in EUROPE_COUNTRIES) or (
                    tld_country and tld_country not in EUROPE_COUNTRIES
                ):
                    report.excluded_results += 1
                    continue
            candidates.setdefault(candidate.domain, candidate)
        report.candidates = sorted(candidates.values(), key=lambda c: (-c.score, c.company.lower()))
        report.queries_used = budget.used
        notify("Terminé", 1.0)
        return report
