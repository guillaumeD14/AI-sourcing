"""Recherche Internet de fabricants industriels européens (Tavily).

Phases, dans la limite d'un budget de requêtes (20 maximum) :
 1. compréhension du besoin et synonymes multilingues      (``textutils``)
 2. découverte de domaines fabricants                      (requêtes concurrentes, ``exclude_domains``)
 3. ciblage des domaines officiels (``include_domains``)   (page produit, preuve industrielle)
 4. contact et fiche technique des meilleurs candidats      (budget restant)
 5. qualification, score explicable et déduplication

Règle d'or : un fabricant n'est « confirmé » que si une page du *site officiel* prouve l'industrialisation ET une page
du même site montre le produit ou la gamme proche. Aucune URL ni information n'est inventée.
"""

from __future__ import annotations

import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable
from urllib.parse import urlparse

from sourcing_domains import (country_from_domain, exclude_list_for_tavily, exclusion_reason, in_europe, is_homepage,
                      is_non_european_domain, registered_domain)
from sourcing_errors import AuthError, ConfigError, QuotaError, SourcingError
from sourcing_http import new_session, request_with_retry
from sourcing_images import ImageDescriptor, compute_descriptor, fetch_remote_image, similarity_pair
from sourcing_manufacturers import VerifiedManufacturer, first_country_iso
from sourcing_models import (Evidence, Level, NOT_FOUND_PUBLIC, STATUS_CONFIRMED, STATUS_NOT_FOUND, STATUS_PROBABLE, STATUS_TO_CONFIRM,
                     SupplierCandidate)
from sourcing_suppliers import KnownSupplier, SupplierDirectory
from sourcing_textutils import (COUNTRY_LABELS, QueryPlan, contains_term, detect_countries, extract_weights, normalize)

TAVILY_URL = "https://api.tavily.com/search"
MAX_BUDGET = 20
MIN_BUDGET = 4
ANGLES_TO_CONFIRM = ["MOQ (quantité minimale)", "capacité de production", "certifications (IFS, BRC…)", "origine des matières premières",
                     "délai de livraison", "livraison en France"]

# ---------------------------------------------------------------------------
# Client Tavily
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebResult:
    url: str
    title: str
    content: str


@dataclass(frozen=True)
class WebResponse:
    results: list[WebResult]
    images: list[str] = field(default_factory=list)


class TavilyClient:
    """Client minimal : timeout, 2 tentatives sur erreur serveur, clé uniquement dans l'en-tête."""

    def __init__(self, api_key: str, session: Any = None) -> None:
        self._key = api_key
        self._session = session or new_session()

    def search(self, query: str, *, max_results: int = 8, include_domains: list[str] | None = None,
               exclude_domains: list[str] | None = None, include_images: bool = False) -> WebResponse:
        if not self._key:
            raise ConfigError("Clé Tavily absente : ajoutez TAVILY_API_KEY (commence par « tvly- ») dans les Secrets Streamlit.")
        body: dict[str, Any] = {"query": query[:390], "search_depth": "basic", "max_results": max_results,
                                "include_answer": False, "include_raw_content": False, "include_images": include_images}
        if include_domains:
            body["include_domains"] = include_domains[:20]
        if exclude_domains:
            body["exclude_domains"] = exclude_domains[:150]

        def explain(response: Any) -> None:
            if response.status_code == 400:
                raise SourcingError("Tavily a refusé la requête (HTTP 400) : requête trop longue ou paramètre invalide.")
            if response.status_code == 401:
                raise AuthError("Clé Tavily invalide (HTTP 401) : vérifiez TAVILY_API_KEY (doit commencer par « tvly- »).")

        response = request_with_retry(self._session, "POST", TAVILY_URL, service="Tavily", retries=2, json=body,
                                      headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                                      timeout=(6.0, 30.0), error_hook=explain)
        data = response.json()
        results = [WebResult(str(r.get("url", "")), str(r.get("title", "")), str(r.get("content", "")))
                   for r in data.get("results", []) if r.get("url")]
        images = [str(i.get("url", "")) if isinstance(i, dict) else str(i) for i in data.get("images", []) or []]
        return WebResponse(results, [i for i in images if i])


# ---------------------------------------------------------------------------
# Requêtes
# ---------------------------------------------------------------------------

_LANG_TEMPLATES = {
    "en": "{product} {weight}{mods} industrial bakery manufacturer foodservice private label factory Europe",
    "fr": "fabricant industriel {product} {weight}{mods} usine boulangerie viennoiserie industrielle foodservice marque distributeur Europe",
    "de": "{product} {weight}{mods} Hersteller industrielle Bäckerei Tiefkühl Foodservice Eigenmarke Werk",
    "it": "{product} {weight}{mods} produttore industriale pasticceria surgelata foodservice marchio del distributore stabilimento",
    "es": "{product} {weight}{mods} fabricante industrial bollería congelada foodservice marca blanca fábrica",
}
_COUNTRY_LANG = {"IT": "it", "DE": "de", "AT": "de", "CH": "de", "ES": "es", "PT": "es", "FR": "fr", "BE": "fr", "LU": "fr"}


def _names(plan: QueryPlan, lang: str) -> str:
    names = [f.names.get(lang) or f.names.get("en", "") for f in plan.specific_families[:2]]
    names = [n for n in names if n]
    return " ".join(dict.fromkeys(names)) or plan.product_text


def _mods(plan: QueryPlan, lang: str) -> str:
    key = "query_fr" if lang == "fr" else "query_en"
    words = [getattr(m, key) for m in plan.modifiers if getattr(m, key) and m.id not in ("private_label", "foodservice", "assortiment")]
    return (" " + " ".join(words[:3])) if words else ""


def build_discovery_queries(plan: QueryPlan, count: int) -> list[tuple[str, str]]:
    """Requêtes ciblées multilingues : anglais, français, puis langue des pays souhaités, puis angles complémentaires."""
    weight = f"{plan.weight_g:g}g " if plan.weight_g else ""
    order = ["en", "fr"]
    for iso in plan.countries:
        lang = _COUNTRY_LANG.get(iso)
        if lang and lang not in order:
            order.append(lang)
    country_langs = len(order) - 2
    order += [lang for lang in ("de", "it", "es") if lang not in order]
    queries = [(" ".join(_LANG_TEMPLATES[lang].format(product=_names(plan, lang), weight=weight, mods=_mods(plan, lang)).split()), lang)
               for lang in order]
    product_en = _names(plan, "en")
    queries.insert(2 + country_langs, (f"{product_en} {weight}contract manufacturing co-packing bakery factory production lines Europe".replace("  ", " "), "en"))
    queries.append((f"{product_en} {weight}frozen bakery catalogue technical data sheet manufacturer foodservice pdf".replace("  ", " "), "en"))
    return queries[:count]


# ---------------------------------------------------------------------------
# Classification des pages
# ---------------------------------------------------------------------------

_MANUFACTURER = ("manufactur", "fabricant", "fabrique", "hersteller", "produttore", "fabricante", "producer", "factory", "factories",
                 "usine", "werk", "stabilimento", "fabrica", "production site", "bakery group", "industrial bakery", "industrielle",
                 "industrial", "industriel", "boulangerie industrielle", "grossbackerei", "industriale")
_DISTRIBUTOR = ("distributor", "distributeur", "grossiste", "wholesaler", "importer", "importateur", "grosshandel", "grossista",
                "retailer", "reseller", "revendeur", "marketplace")
_B2B = ("foodservice", "food service", "b2b", "horeca", "rhd", "restauration hors domicile", "gastronomie", "professional",
        "professionnel", "catering", "out of home", "ristorazione", "hosteleria")
_PRIVATE_LABEL = ("private label", "mdd", "marque distributeur", "marque de distributeur", "eigenmarke", "handelsmarke", "marca blanca",
                  "co packing", "co-packing", "contract manufacturing", "white label", "marchio del distributore", "copacking")
_CERTS = ("ifs", "brc", "brcgs", "fssc", "iso 22000", "haccp")
_STRONG_INDUSTRIAL = re.compile(
    r"\b(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|un|deux|trois|quatre|cinq|six|sept|huit|neuf|dix)\s+"
    r"(?:\w+\s+)?(?:production\s+)?(?:sites?|plants?|factories|factory|usines?|werke?|stabilimenti|f[aá]bricas?)\b"
    r"|\b\d[\d.,\s]*\s*(?:tonnes?|tons|tonnen|tonnellate|toneladas)\b|\bproduction\s+(?:lines?|capacity)\b|\bcapacit[ée]\s+de\s+production\b",
    re.I)
_PATH_CONTACT = re.compile(r"(contact|kontakt|contatt|contacto|contactez|nous-contacter|get-in-touch|enquir|reach-us)", re.I)
_PATH_DATASHEET = re.compile(r"(datasheet|data-sheet|fiche-technique|technical|specification|brochure|catalog|katalog|catalogo|\.pdf)", re.I)
_PATH_PRODUCT = re.compile(r"(product|produit|produkt|prodott|producto|range|gamme|sortiment|assortiment|portfolio|catalog|collection|sweet|pastr|"
                           r"viennois|bakery|boulanger|backwar|danish|croissant|brioche|snack)", re.I)
_PATH_INDUSTRIAL = re.compile(r"(factory|factories|plant|production|manufactur|usine|werk|produktion|stabiliment|fabrica|capacity|about|company|"
                              r"unternehmen|azienda|entreprise|who-we-are|our-story|sites|locations|standorte|qualit)", re.I)
_PATH_NEWS = re.compile(r"/(news|press|presse|blog|article|actu|actualit|magazin|stampa|noticias|events?)(/|-|$)", re.I)


def snippet_around(text: str, keywords: list[str], limit: int = 200) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    lowered = flat.lower()
    positions = [lowered.find(k) for k in keywords if lowered.find(k) >= 0]
    start = max(0, (min(positions) if positions else 0) - 60)
    piece = flat[start:start + limit]
    return ("…" if start else "") + piece.strip() + ("…" if start + limit < len(flat) else "")


def _has(text_norm: str, terms: tuple[str, ...]) -> bool:
    return any(t in text_norm for t in terms)


@dataclass
class PageInfo:
    url: str
    title: str
    content: str
    kinds: set[str] = field(default_factory=set)
    weights: list[float] = field(default_factory=list)
    product_score: float = 0.0


def classify_page(result: WebResult, plan: QueryPlan) -> PageInfo:
    """Rôle(s) d'une page du site officiel : produit, contact, fiche technique, preuve industrielle."""
    info = PageInfo(result.url, result.title, result.content)
    parsed = urlparse(result.url)
    path = parsed.path.lower()
    text_norm = normalize(f"{result.title} {result.content}")
    title_norm = normalize(f"{result.title} {path.replace('/', ' ').replace('-', ' ')}")
    news = bool(_PATH_NEWS.search(path))
    info.weights, unit = extract_weights(f"{result.title} {result.content}")
    info.weights = unit or info.weights
    terms = [t for f in plan.specific_families for t in f.terms] or list(plan.tokens)
    in_title = any(contains_term(title_norm, t) for t in terms)
    in_text = any(contains_term(text_norm, t) for t in terms)
    if _PATH_CONTACT.search(path) and not news:
        info.kinds.add("contact")
    if _PATH_DATASHEET.search(path) and not news:
        info.kinds.add("datasheet")
    if not is_homepage(result.url) and not news and (in_title or (in_text and _PATH_PRODUCT.search(path))):
        info.kinds.add("product")
        info.product_score = 3.0 * in_title + 2.0 * bool(_PATH_PRODUCT.search(path)) + 1.0 * in_text + 1.0 * bool(info.weights) \
            + (3.0 if plan.weight_g and any(abs(w - plan.weight_g) / plan.weight_g <= 0.15 for w in info.weights) else 0.0) - (1.0 if path.endswith(".pdf") else 0.0)
    if not news and (_STRONG_INDUSTRIAL.search(result.content) or (_PATH_INDUSTRIAL.search(path) and _has(text_norm, _MANUFACTURER))):
        info.kinds.add("industrial")
    return info


# ---------------------------------------------------------------------------
# Options, rapport
# ---------------------------------------------------------------------------


LIST_PRIORITY = "prioritaire"   # la liste de l'acheteur est qualifiée en premier (part du budget réservée)
LIST_FLAG_ONLY = "marquer"      # la liste sert seulement à repérer « connu / nouveau »
LIST_EXCLUDE = "exclure"        # les fournisseurs connus sont écartés (découverte pure)
LIST_MODES = (LIST_PRIORITY, LIST_FLAG_ONLY, LIST_EXCLUDE)


@dataclass(frozen=True)
class WebSearchOptions:
    max_queries: int = 12
    list_mode: str = LIST_PRIORITY
    list_share: float = 0.4          # part du budget consacrée à la liste en mode « prioritaire »
    europe_extended: bool = False
    use_base: bool = True
    max_candidates: int = 12

    @property
    def budget(self) -> int:
        return max(MIN_BUDGET, min(MAX_BUDGET, self.max_queries))


@dataclass
class WebSearchReport:
    candidates: list[SupplierCandidate] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    queries_used: int = 0
    excluded_results: int = 0
    exclusion_reasons: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    known_skipped: list[tuple[str, str, str]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    list_size: int = 0
    list_qualified: int = 0
    new_qualified: int = 0
    checked_on: str = ""


class _Budget:
    """Compteur de requêtes thread-safe : jamais plus que le budget demandé."""

    def __init__(self, total: int) -> None:
        self.total, self.used, self._lock = total, 0, threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.used >= self.total:
                return False
            self.used += 1
            return True

    @property
    def left(self) -> int:
        return self.total - self.used


@dataclass
class _Domain:
    domain: str
    results: list[WebResult] = field(default_factory=list)
    hits: int = 0
    priority: float = 0.0
    signals: int = 0              # indices de fabrication ou de B2B dans les extraits
    base: VerifiedManufacturer | None = None
    known: KnownSupplier | None = None
    in_list: bool = False          # issu de la liste de l'acheteur (et non de la découverte)
    list_rank: int | None = None
    images: list[str] = field(default_factory=list)
    targeted: list[WebResult] = field(default_factory=list)
    contact_results: list[WebResult] = field(default_factory=list)


def guess_company(domain: str, results: list[WebResult], known: KnownSupplier | None, base: VerifiedManufacturer | None) -> str:
    if base:
        return base.name
    if known:
        return known.name
    label = normalize(domain.split(".")[0]).replace(" ", "")
    for result in results:
        for segment in re.split(r"\s[|\-–—:·»]\s", result.title):
            seg = segment.strip()
            if label and label in normalize(seg).replace(" ", "") and 2 < len(seg) <= 50:
                return seg
    return domain.split(".")[0].replace("-", " ").title()


# ---------------------------------------------------------------------------
# Classement de la liste de l'acheteur
# ---------------------------------------------------------------------------

_CATEGORY_POINTS = (("boulang", 3.0), ("viennois", 3.0), ("patisser", 3.0), ("brioche", 3.0), ("surgel", 2.0), ("frozen", 2.0),
                    ("copack", 2.0), ("co packing", 2.0))


def rank_known_suppliers(plan: QueryPlan, suppliers: list[KnownSupplier], priority_names: tuple[str, ...] | list[str] = ()) -> list[KnownSupplier]:
    """Fournisseurs de la liste du plus au moins pertinent pour le besoin (catégorie, pays, vérification, site connu, déjà vus
    dans les catalogues). Les catégories clairement hors sujet (meunerie, glaces…) sont écartées."""
    families = {f.id for f in plan.specific_families}
    priority = [normalize(n) for n in priority_names if n]
    scored: list[tuple[float, str, KnownSupplier]] = []
    for supplier in suppliers:
        category = normalize(supplier.category)
        score = max((pts for key, pts in _CATEGORY_POINTS if key in category), default=0.0)
        if "non determinee" in category:
            score += 0.5
        if "meunerie" in category or "farine" in category:
            score -= 6
        if "glace" in category or "dessert" in category:
            score -= 1 if families & {"cake", "eclair", "millefeuille", "tarte"} else 4
        if "biscuit" in category:
            score += 2 if "cookie" in families else -2
        if "pizza" in category:
            score += 5 if families & {"pizza", "quiche"} else -3
        if supplier.origin == "catalogues indexés":
            score += 2
        if supplier.verified:
            score += 3
        if supplier.domain:
            score += 2
        iso = first_country_iso(supplier.country)
        if iso and plan.countries and iso in plan.countries:
            score += 2
        if iso and in_europe(iso, extended=True) is False:
            score -= 10
        name = normalize(supplier.name)
        if any(p and (p == name or p in name or name in p) for p in priority):
            score += 8  # a déjà fourni un produit proche dans les catalogues internes : signal très fort
        scored.append((score, supplier.name.lower(), supplier))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [supplier for score, _, supplier in scored if score > -3]


# ---------------------------------------------------------------------------
# Moteur
# ---------------------------------------------------------------------------


class WebSourcing:
    def __init__(self, client: Any, *, manufacturers: list[VerifiedManufacturer] | None = None,
                 directory: SupplierDirectory | None = None, options: WebSearchOptions | None = None,
                 image_session: Any = None, today: str | None = None) -> None:
        self.client = client
        self.manufacturers = manufacturers or []
        self.directory = directory or SupplierDirectory()
        self.options = options or WebSearchOptions()
        self._image_session = image_session
        self.today = today or date.today().isoformat()

    # -- orchestration ----------------------------------------------------------

    def run(self, plan: QueryPlan, progress: Callable[[str, float], None] | None = None,
            query_image: ImageDescriptor | None = None, priority_suppliers: tuple[str, ...] | list[str] = ()) -> WebSearchReport:
        """Découverte + liste de l'acheteur. En mode « prioritaire », une part du budget qualifie d'abord les fournisseurs de la
        liste (classés par pertinence ; ceux sans site sont résolus par lots de 3 noms par requête), le reste découvre de
        nouveaux industriels. ``priority_suppliers`` : fournisseurs déjà vus dans les résultats des catalogues internes."""
        if plan.is_empty:
            raise SourcingError("Décrivez d'abord le produit recherché (onglet Besoin).")
        report = WebSearchReport(checked_on=self.today, list_size=len(self.directory))
        budget = _Budget(self.options.budget)
        say = progress or (lambda label, fraction: None)
        exclude = exclude_list_for_tavily()
        stop = {"flag": False}
        search = self._searcher(report, budget, stop)
        mode = self.options.list_mode if self.options.list_mode in LIST_MODES else LIST_PRIORITY

        total = budget.total
        contact_reserve = max(1, round(total * 0.15))
        known_budget = 0
        if mode == LIST_PRIORITY:
            known_budget = max(0, min(round(total * self.options.list_share), total - contact_reserve - 2))
        ranked_list = rank_known_suppliers(plan, self.directory.suppliers, priority_suppliers) if known_budget else []
        to_resolve = [s for s in ranked_list if not s.domain and not s.is_generic]
        resolution_n = 0
        if to_resolve and known_budget > 1:
            resolution_n = min(math.ceil(len(to_resolve) / 3), max(1, round(known_budget * 0.3)), known_budget - 1)
        groups = [to_resolve[i * 3:(i + 1) * 3] for i in range(resolution_n)]
        discovery_n = max(2, min(7, round((total - known_budget - contact_reserve) * 0.45)))

        say("Découverte de fabricants et recherche des sites de votre liste…", 0.05)
        tasks: list[Callable[[], Any]] = [
            (lambda q=q: ("discovery", None, search(q, max_results=10, exclude_domains=exclude))) for q, _ in build_discovery_queries(plan, discovery_n)]
        tasks += [(lambda g=g: ("resolution", g, search(self._resolution_query(g), max_results=10, exclude_domains=exclude))) for g in groups]
        discovered: dict[str, _Domain] = {}
        resolved: dict[str, tuple[str, list[WebResult]]] = {}
        for outcome in self._parallel(tasks):
            if not outcome or outcome[2] is None:
                continue
            kind, group, response = outcome
            if kind == "discovery":
                self._ingest(response, discovered, report)
            else:
                self._match_resolution(group, response, resolved, report)
        for group in groups:
            report.unresolved.extend(s.name for s in group if s.name not in resolved and not stop["flag"])
        if stop["flag"] and not discovered and not resolved:  # clé refusée / quota épuisé dès le départ : rien d'exploitable
            raise SourcingError(report.warnings[0] if report.warnings else "Recherche Internet impossible.")

        # liste de l'acheteur + découverte dans un même ensemble de domaines
        combined = dict(discovered)
        for rank, supplier in enumerate(ranked_list):
            domain_name = supplier.domain or resolved.get(supplier.name, ("", []))[0]
            if not domain_name:
                continue
            entry = combined.setdefault(domain_name, _Domain(domain_name))
            if not entry.in_list:
                entry.in_list, entry.list_rank, entry.known = True, rank, supplier
            extra = resolved.get(supplier.name, ("", []))[1]
            entry.results.extend(r for r in extra if all(r.url != e.url for e in entry.results))
            entry.hits = max(entry.hits, len(entry.results))
        self._attach_leads(combined)
        seeds = self._seed_domains(plan, combined)
        for domain in combined.values():
            domain.known = domain.known or self.directory.match_domain(domain.domain)
            domain.priority += self._priority(domain)

        def list_order(d: _Domain) -> float:
            rank_bonus = 0.0 if d.list_rank is None else max(0.0, 12.0 - d.list_rank * 0.4)
            return (2.0 if d.base and not d.base.is_lead else 0.0) + rank_bonus + d.priority

        list_like = [d for d in combined.values() if d.known or d.base]
        if mode == LIST_EXCLUDE:
            for d in list_like:
                if d.known and not d.base:
                    report.known_skipped.append((d.known.name, d.domain, d.known.origin))
            list_like = [d for d in list_like if d.base and not d.known]
        new_pool = [d for d in combined.values() if d not in list_like and not (mode == LIST_EXCLUDE and d.known)]
        if mode == LIST_FLAG_ONLY:
            new_pool = [d for d in combined.values() if not d.base]
            list_like = [d for d in combined.values() if d.base]
        capacity = max(1, budget.left - contact_reserve)
        list_slots = max(known_budget - len(groups), 1) if mode == LIST_PRIORITY else min(len(seeds), max(1, round(total * 0.3)))
        list_selected = sorted(list_like, key=lambda d: -list_order(d))[: min(list_slots, capacity)]
        new_candidates = sorted((d for d in new_pool if d.priority > 0 and d.signals > 0), key=lambda d: -d.priority)
        if new_candidates and capacity - len(list_selected) < 1:
            list_selected = list_selected[: max(0, capacity - 1)]  # toujours au moins un créneau pour découvrir du nouveau
        new_selected = new_candidates[: max(0, capacity - len(list_selected))]
        selected = [*list_selected, *new_selected]
        if not selected:
            report.warnings.append("Aucun domaine de fabricant exploitable dans les résultats : reformulez le besoin ou augmentez le budget.")
        self._qualify(selected, plan, report, budget, search, say, query_image)
        report.list_qualified = sum(1 for c in report.candidates if c.known_supplier)
        report.new_qualified = len(report.candidates) - report.list_qualified
        return report

    @staticmethod
    def _resolution_query(group: list[KnownSupplier]) -> str:
        """Une seule requête pour retrouver le site officiel de 3 fournisseurs de la liste."""
        return " ".join(f'"{s.search_name}"' for s in group) + " official website bakery manufacturer"

    @staticmethod
    def _match_resolution(group: list[KnownSupplier], response: WebResponse, resolved: dict[str, tuple[str, list[WebResult]]],
                          report: WebSearchReport) -> None:
        """Associe chaque fournisseur au domaine dont le nom correspond (jamais d'URL devinée)."""
        for supplier in group:
            token = supplier.core_tokens[0] if supplier.core_tokens else ""
            if len(token) < 4 or supplier.is_generic:
                continue
            for result in response.results:
                domain = registered_domain(result.url)
                label = normalize(domain.split(".")[0]).replace(" ", "")
                if is_non_european_domain(domain) or exclusion_reason(result.url, result.title, result.content) is not None:
                    continue
                if label == token or label.startswith(token) or token in label:
                    resolved[supplier.name] = (domain, [r for r in response.results if registered_domain(r.url) == domain])
                    break

    def run_known(self, plan: QueryPlan, suppliers: list[KnownSupplier], progress: Callable[[str, float], None] | None = None,
                  query_image: ImageDescriptor | None = None) -> WebSearchReport:
        """Qualifie des fournisseurs de la liste de l'acheteur sur le produit recherché (1 à 2 requêtes chacun)."""
        if plan.is_empty:
            raise SourcingError("Décrivez d'abord le produit recherché (onglet Besoin).")
        if not suppliers:
            raise SourcingError("Sélectionnez au moins un fournisseur.")
        report = WebSearchReport(checked_on=self.today)
        budget = _Budget(self.options.budget)
        stop = {"flag": False}
        say = progress or (lambda label, fraction: None)
        search = self._searcher(report, budget, stop)
        chosen, cost = [], 0
        for supplier in suppliers:  # un fournisseur sans site coûte 2 requêtes (résolution du site, puis ciblage)
            price = 1 if supplier.domain else 2
            if cost + price > budget.total:
                report.warnings.append(f"Budget insuffisant : {len(suppliers) - len(chosen)} fournisseur(s) non traité(s). Augmentez le budget ou réduisez la sélection.")
                break
            chosen.append(supplier)
            cost += price
        selected: list[_Domain] = []
        to_resolve = []
        for supplier in chosen:
            if supplier.domain:
                selected.append(_Domain(supplier.domain, known=supplier))
            else:
                to_resolve.append(supplier)
        say("Recherche du site officiel des fournisseurs…", 0.1)
        exclude = exclude_list_for_tavily()
        tasks = [(lambda sup=sup: (sup, search(f'"{sup.name}" official website bakery manufacturer', max_results=6, exclude_domains=exclude)))
                 for sup in to_resolve]
        for outcome in self._parallel(tasks):
            if not outcome or outcome[1] is None:
                continue
            supplier, response = outcome
            token = supplier.core_tokens[0] if supplier.core_tokens else ""
            match = None
            for result in response.results:
                domain = registered_domain(result.url)
                label = normalize(domain.split(".")[0]).replace(" ", "")
                if exclusion_reason(result.url, result.title, result.content) is None and token and len(token) >= 4 \
                        and (label == token or label.startswith(token) or token in label):
                    match = (domain, result)
                    break
            if match is None:
                report.unresolved.append(supplier.name)
            else:
                entry = _Domain(match[0], results=[r for r in response.results if registered_domain(r.url) == match[0]], hits=1, known=supplier)
                selected.append(entry)
        for entry in selected:
            entry.base = next((m for m in self.manufacturers if m.domain and m.domain == entry.domain), None)
        if stop["flag"] and not selected:
            raise SourcingError(report.warnings[0] if report.warnings else "Recherche Internet impossible.")
        self._qualify(selected, plan, report, budget, search, say, query_image, keep_all=True)
        return report

    def _searcher(self, report: WebSearchReport, budget: _Budget, stop: dict[str, bool]) -> Callable[..., WebResponse | None]:
        def search(query: str, **kwargs: Any) -> WebResponse | None:
            if stop["flag"] or not budget.take():
                return None
            report.queries.append(query)
            try:
                return self.client.search(query, **kwargs)
            except (AuthError, QuotaError) as exc:
                stop["flag"] = True
                report.warnings.append(str(exc))
            except SourcingError as exc:
                report.warnings.append(str(exc))
            return None
        return search

    def _qualify(self, selected: list[_Domain], plan: QueryPlan, report: WebSearchReport, budget: _Budget,
                 search: Callable[..., WebResponse | None], say: Callable[[str, float], None],
                 query_image: ImageDescriptor | None, *, keep_all: bool = False) -> None:
        """Phases 3 à 5 : pages officielles, contact / fiche technique, qualification et score."""
        say("Recherche des pages officielles (produit, usines)…", 0.45)
        want_images = query_image is not None
        tasks = [(lambda d=d: (d, search(self._targeted_query(plan), max_results=8, include_domains=[d.domain],
                                         include_images=want_images))) for d in selected]
        for outcome in self._parallel(tasks):
            if outcome is None:
                continue
            domain, response = outcome
            if response is not None:
                domain.targeted = [r for r in response.results if registered_domain(r.url) == domain.domain]
                domain.images = [i for i in response.images if registered_domain(i) == domain.domain]

        say("Recherche des pages de contact…", 0.7)
        preliminary = [(self._build_candidate(d, plan), d) for d in selected]
        needing = [d for c, d in preliminary if c.status in (STATUS_CONFIRMED, STATUS_PROBABLE) and (not c.contact_url or not c.datasheet_url)]
        contact_tasks = [(lambda d=d: (d, search(f"{guess_company(d.domain, d.results, d.known, d.base)} contact sales foodservice private label",
                                                max_results=5, include_domains=[d.domain]))) for d in needing[: max(0, budget.left)]]
        for outcome in self._parallel(contact_tasks):
            if outcome and outcome[1] is not None:
                outcome[0].contact_results = [r for r in outcome[1].results if registered_domain(r.url) == outcome[0].domain]

        say("Qualification et score…", 0.85)
        kept: list[SupplierCandidate] = []
        for candidate in (self._build_candidate(d, plan) for d in selected):
            if in_europe(candidate.country_code, extended=self.options.europe_extended) is False:
                report.excluded_results += 1
                report.exclusion_reasons["hors Europe"] = report.exclusion_reasons.get("hors Europe", 0) + 1
                continue
            kept.append(candidate)
        if query_image is not None:
            self._compare_images(kept, {d.domain: d for d in selected}, query_image)
        kept.sort(key=lambda c: (STATUS_RANK[c.status], -c.score))
        report.candidates = [c for c in kept if keep_all or c.status != STATUS_NOT_FOUND or c.from_verified_base][: self.options.max_candidates]
        report.queries_used = budget.used
        say("Terminé", 1.0)

    # -- étapes ------------------------------------------------------------------

    @staticmethod
    def _parallel(tasks: list[Callable[[], Any]]) -> list[Any]:
        if not tasks:
            return []
        outputs: list[Any] = []
        with ThreadPoolExecutor(max_workers=min(3, len(tasks))) as pool:
            futures = [pool.submit(task) for task in tasks]
            for future in as_completed(futures):
                try:
                    outputs.append(future.result())
                except SourcingError:
                    outputs.append(None)
        return outputs

    def _ingest(self, response: WebResponse, domains: dict[str, _Domain], report: WebSearchReport) -> None:
        for result in response.results:
            reason = exclusion_reason(result.url, result.title, result.content)
            domain = registered_domain(result.url)
            if reason is None and is_non_european_domain(domain):
                reason = "hors Europe"
            if reason is not None:
                report.excluded_results += 1
                report.exclusion_reasons[reason] = report.exclusion_reasons.get(reason, 0) + 1
                continue
            entry = domains.setdefault(domain, _Domain(domain))
            entry.results.append(result)
            entry.hits += 1

    def _priority(self, domain: _Domain) -> float:
        text = normalize(" ".join(f"{r.title} {r.content}" for r in domain.results))
        manufacturer = sum(text.count(t) for t in _MANUFACTURER)
        distributor = sum(text.count(t) for t in _DISTRIBUTOR)
        b2b = sum(text.count(t) for t in _B2B)
        domain.signals = manufacturer + b2b
        score = domain.hits * 2.0 + min(manufacturer, 6) * 2.0 + min(b2b, 4) - min(distributor, 5) * 2.0
        if domain.known:
            score += 1.0
        if domain.base:
            score += 4.0
        return score

    def _attach_leads(self, domains: dict[str, _Domain]) -> None:
        """Rattache les pistes de la base *sans domaine* aux domaines découverts dont le nom correspond (ex. « Bridor »)."""
        for manufacturer in self.manufacturers:
            if manufacturer.domain:
                continue
            tokens = [t for t in normalize(manufacturer.name).split() if len(t) >= 4]
            if not tokens:
                continue
            for entry in domains.values():
                label = normalize(entry.domain.split(".")[0]).replace(" ", "")
                if entry.base is None and (label == tokens[0] or (label.startswith(tokens[0]) and len(tokens[0]) >= 5)):
                    entry.base = manufacturer
                    break

    def _seed_domains(self, plan: QueryPlan, domains: dict[str, _Domain]) -> list[_Domain]:
        """Injecte les fabricants de la base vérifiée qui ont un domaine officiel et une catégorie compatible."""
        if not self.options.use_base:
            return []
        seeds = []
        wanted = [normalize(t) for f in plan.specific_families for t in f.terms[:3]] + ["viennoiserie", "boulanger", "bakery", "pastry", "danish", "surgel", "frozen"]
        for manufacturer in self.manufacturers:
            if not manufacturer.domain:
                continue
            category_text = normalize(" ".join(manufacturer.categories))
            if wanted and not any(w and w in category_text for w in wanted):
                continue
            entry = domains.setdefault(manufacturer.domain, _Domain(manufacturer.domain))
            entry.base = manufacturer
            seeds.append(entry)
            if len(seeds) >= 4:
                break
        return seeds

    @staticmethod
    def _targeted_query(plan: QueryPlan) -> str:
        parts = [_names(plan, "en"), _names(plan, "fr")]
        weight = f"{plan.weight_g:g} g" if plan.weight_g else ""
        words = dict.fromkeys(" ".join(p for p in parts if p).split())
        return " ".join([*words, weight, "factory production foodservice private label"]).strip()

    # -- construction d'un candidat ---------------------------------------------------

    def _build_candidate(self, domain: _Domain, plan: QueryPlan) -> SupplierCandidate:
        pages = [classify_page(r, plan) for r in [*domain.results, *domain.targeted, *domain.contact_results]]
        seen, unique_pages = set(), []
        for page in pages:
            if page.url not in seen:
                seen.add(page.url)
                unique_pages.append(page)
        text_all = normalize(" ".join(f"{p.title} {p.content}" for p in unique_pages))
        known, base = domain.known, domain.base
        company = guess_company(domain.domain, domain.results + domain.targeted, known, base)
        candidate = SupplierCandidate(key=domain.domain, company=company, domain=domain.domain, checked_on=self.today,
                                      known_supplier=known is not None, known_as=known.name if known else "",
                                      from_verified_base=base is not None)

        # pays
        country = (base.country if base and base.country else "") or country_from_domain(domain.domain) or ""
        if not country and known and known.country:
            countries = detect_countries(normalize(known.country))
            country = countries[0] if countries else ""
        if not country:
            found = detect_countries(text_all)
            if len(found) == 1:
                country = found[0]
                candidate.notes.append("pays déduit du texte des pages : à confirmer")
        candidate.country_code = country
        candidate.country = COUNTRY_LABELS.get(country, country)
        candidate.categories = list(base.categories) if base else self._categories(text_all)

        # preuves officielles (toutes les pages viennent du domaine du candidat)
        industrial = sorted((p for p in unique_pages if "industrial" in p.kinds),
                            key=lambda p: (0 if _STRONG_INDUSTRIAL.search(p.content) else 1))
        products = sorted((p for p in unique_pages if "product" in p.kinds), key=lambda p: -p.product_score)
        for page in industrial[:2]:
            candidate.industrial_evidence.append(Evidence("industrielle", page.url, "site officiel",
                                                          snippet_around(page.content, ["factor", "usine", "production", "plant", "werk", "tonne"]), self.today))
        for page in products[:2]:
            candidate.product_evidence.append(Evidence("produit", page.url, "site officiel",
                                                       snippet_around(page.content, [t for f in plan.specific_families for t in f.terms] or list(plan.tokens)), self.today))
        if base:
            for evidence in base.evidence():
                bucket = candidate.industrial_evidence if evidence.kind == "industrielle" else candidate.product_evidence
                if all(e.url != evidence.url for e in bucket):
                    bucket.append(evidence)
            if base.note:
                candidate.notes.append("base : " + base.note)
        strong_industrial = any(_STRONG_INDUSTRIAL.search(p.content) for p in industrial[:2])
        candidate.industrial_level = Level.CONFIRMED if strong_industrial else (Level.PROBABLE if candidate.industrial_evidence else Level.NOT_FOUND)
        candidate.product_level = Level.CONFIRMED if products else (Level.PROBABLE if candidate.product_evidence else Level.NOT_FOUND)
        candidate.private_label = self._level(text_all, _PRIVATE_LABEL)
        candidate.foodservice = self._level(text_all, _B2B)

        # liens directs
        if products:
            candidate.product_page_url = products[0].url
            candidate.similar_product = (products[0].title or "").strip()[:120]
            candidate.weights_g = sorted({w for p in products[:3] for w in p.weights})
        elif base and base.official_url(base.product_url) and not is_homepage(base.product_url):
            candidate.product_page_url = base.product_url
        datasheets = [p for p in unique_pages if "datasheet" in p.kinds]
        contacts = [p for p in unique_pages if "contact" in p.kinds]
        candidate.datasheet_url = datasheets[0].url if datasheets else None
        candidate.contact_url = contacts[0].url if contacts else None

        # statut : confirmé seulement avec preuves officielles industrielle ET produit
        has_industrial = candidate.industrial_level in (Level.CONFIRMED, Level.PROBABLE) and bool(candidate.industrial_evidence)
        has_product = candidate.product_level in (Level.CONFIRMED, Level.PROBABLE) and bool(candidate.product_evidence)
        manufacturer_signals = _has(text_all, _MANUFACTURER)
        distributor_signals = sum(text_all.count(t) for t in _DISTRIBUTOR)
        if has_industrial and has_product and candidate.industrial_level == Level.CONFIRMED:
            candidate.status = STATUS_CONFIRMED
        elif (has_industrial or has_product) and manufacturer_signals:
            candidate.status = STATUS_PROBABLE
        elif manufacturer_signals or base:
            candidate.status = STATUS_TO_CONFIRM
        else:
            candidate.status = STATUS_NOT_FOUND
        if base and base.is_lead and not (candidate.industrial_evidence or candidate.product_evidence):
            candidate.status = STATUS_NOT_FOUND
            candidate.notes.append("piste de la base sans preuve officielle trouvée : " + NOT_FOUND_PUBLIC)
        self._score(candidate, plan, text_all, distributor_signals, manufacturer_signals)
        candidate.to_confirm = [a for a in ANGLES_TO_CONFIRM if not (a.startswith("certifications") and _has(text_all, _CERTS))]
        if _has(text_all, _CERTS):
            candidate.notes.append("certifications citées sur le site : à vérifier sur les certificats")
        if not candidate.country:
            candidate.to_confirm.insert(0, "pays")
        return candidate

    @staticmethod
    def _level(text_norm: str, terms: tuple[str, ...]) -> Level:
        return Level.PROBABLE if _has(text_norm, terms) else Level.NOT_FOUND

    @staticmethod
    def _categories(text_norm: str) -> list[str]:
        rules = {"viennoiserie": ("viennoiserie", "croissant", "danish", "pastry"), "boulangerie": ("boulanger", "bakery", "brot", "pane"),
                 "pâtisserie": ("patisserie", "dessert", "cake", "tart"), "surgelé": ("surgel", "frozen", "tiefk", "surgelat")}
        return [name for name, keys in rules.items() if _has(text_norm, keys)]

    def _score(self, c: SupplierCandidate, plan: QueryPlan, text_all: str, distributor: int, manufacturer: bool) -> None:
        comp: dict[str, float] = {}
        maxi = 0.0

        def add(name: str, points: float, maximum: float) -> None:
            nonlocal maxi
            comp[name] = round(points, 1)
            maxi += maximum

        add("preuve industrielle (site officiel)", {Level.CONFIRMED: 25, Level.PROBABLE: 12}.get(c.industrial_level, 0), 25)
        add("produit proche (site officiel)", {Level.CONFIRMED: 25, Level.PROBABLE: 12}.get(c.product_level, 0), 25)
        if plan.weight_g:
            gap = min((abs(w - plan.weight_g) / plan.weight_g for w in c.weights_g), default=None)
            add("grammage proche", 0 if gap is None else (15 if gap <= 0.05 else 10 if gap <= 0.15 else 4 if gap <= 0.30 else 0), 15)
        add("foodservice / B2B", 8 if c.foodservice != Level.NOT_FOUND else 0, 8)
        add("private label / co-manufacturing", 8 if c.private_label != Level.NOT_FOUND else 0, 8)
        capacity = bool(re.search(r"\b(export|countries|pays|tonnes?|tons|capacity|capacit)", text_all)) and manufacturer
        add("capacité / présence internationale", 6 if capacity else 0, 6)
        europe = in_europe(c.country_code, extended=self.options.europe_extended)
        add("implantation européenne", 5 if europe else 0, 5)
        add("lien direct vers la page produit", 4 if c.product_page_url else 0, 4)
        add("catalogue / fiche technique", 4 if c.datasheet_url else 0, 4)
        c.components, c.max_score = comp, maxi
        if distributor > 0 and not manufacturer:
            c.penalties["distributeur ou revendeur suspecté"] = 15.0
            c.notes.append("distributeur / revendeur suspecté : aucune preuve de fabrication")
        elif distributor > 2:
            c.penalties["vocabulaire de distribution"] = 5.0
        if c.product_page_url is None:
            c.notes.append(f"page produit : {NOT_FOUND_PUBLIC}")
        c.score = max(0.0, sum(comp.values()) - sum(c.penalties.values()))

    # -- images web ------------------------------------------------------------------------

    def _compare_images(self, candidates: list[SupplierCandidate], domains: dict[str, _Domain], query: ImageDescriptor) -> None:
        """Compare la photo cible aux images des pages officielles (3 par candidat, 15 au total)."""
        session = self._image_session or new_session()
        fetched = 0
        for candidate in sorted(candidates, key=lambda c: -c.score)[:8]:
            domain = domains.get(candidate.domain)
            if domain is None or not domain.images:
                continue
            best: tuple[float, str] | None = None
            for url in domain.images[:3]:
                if fetched >= 15:
                    break
                fetched += 1
                image = fetch_remote_image(session, url)
                descriptor = compute_descriptor(image) if image is not None else None
                if descriptor is not None:
                    similarity = similarity_pair(query, descriptor)
                    if best is None or similarity > best[0]:
                        best = (similarity, url)
            if best is not None:
                candidate.visual_similarity, candidate.image_url = best
                candidate.components["similarité visuelle"] = round(15 * best[0], 1)
                candidate.max_score += 15
                candidate.score = max(0.0, sum(candidate.components.values()) - sum(candidate.penalties.values()))


STATUS_RANK = {STATUS_CONFIRMED: 0, STATUS_PROBABLE: 1, STATUS_TO_CONFIRM: 2, STATUS_NOT_FOUND: 3}
