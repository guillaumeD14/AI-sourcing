"""Normalisation, extraction (grammage, références, conditionnements) et synonymes.

Tout est local, sans modèle externe, et déterministe : les mêmes entrées donnent
toujours les mêmes scores, ce qui rend le classement explicable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from sourcing_domains import countries_in_request

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

STOPWORDS: frozenset[str] = frozenset(
    """
    le la les un une des du de d l et ou au aux en à a pour par avec sans sur dans the of and or for
    with without in on to a an der die das und mit fur el los las y con por para il lo gli di e
    van het een
    """.split()
)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Minuscules, sans accents, uniquement lettres/chiffres séparés par un espace."""
    lowered = strip_accents(text.lower()).replace("œ", "oe").replace("æ", "ae")
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    tokens = normalize(text).split()
    if keep_stopwords:
        return tokens
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def contains_phrase(norm_text: str, norm_phrase: str) -> bool:
    """Présence d'une expression normalisée sur frontières de mots."""
    if not norm_phrase:
        return False
    return f" {norm_phrase} " in f" {norm_text} "


# ---------------------------------------------------------------------------
# Grammages
# ---------------------------------------------------------------------------

_NUM = r"(\d{1,4}(?:[.,]\d{1,3})?)"
_UNIT = r"(kg|kgs|gr|grs|g|gramme|grammes|grams|gram)"
_WEIGHT_RE = re.compile(
    rf"(?<![\w.,])(?:(\d{{1,3}})\s*[x×*]\s*)?{_NUM}\s*{_UNIT}(?![a-zà-ÿ0-9])",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    rf"(?<![\w.,]){_NUM}\s*(?:-|–|à|a|to)\s*{_NUM}\s*{_UNIT}(?![a-zà-ÿ0-9])",
    re.IGNORECASE,
)
# Contexte nutritionnel : les « 12 g » d'un tableau de valeurs ne sont pas des grammages produit.
_NUTRITION_CTX = re.compile(
    r"(lipides|mati[eè]res? grasses?|graisses?|fat|saturated|satur[eé]s?|glucides|carbohydrates?|"
    r"sucres?|sugars?|prot[eé]ines?|proteins?|fibres?|fibers?|fibre|sel|salt|[eé]nergie|energy|"
    r"dont|of which|nutri|valeurs? moyennes?|pour 100|per 100|par 100)\s*[:\-]?\s*[\w\s,.()%/]{0,25}$",
    re.IGNORECASE,
)


def _to_float(raw: str) -> float:
    return float(raw.replace(",", "."))


def _to_grams(value: float, unit: str) -> float:
    return value * 1000.0 if unit.lower().startswith("kg") else value


def _line_prefix(text: str, start: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    return text[line_start:start]


def extract_weights(text: str) -> list[float]:
    """Retourne les grammages unitaires (en grammes) trouvés, sans doublon, ordre d'apparition.

    - ``40 g``, ``40g``, ``0,04 kg`` → 40.0
    - ``2 x 500 g`` → 500.0 (poids unitaire)
    - ``35-45 g`` → 35.0 et 45.0
    - Les valeurs en contexte nutritionnel (« Lipides 12 g », « pour 100 g ») sont ignorées.
    """
    found: list[float] = []

    def add(value: float) -> None:
        rounded = round(value, 2)
        if 0.5 <= rounded <= 25000 and rounded not in found:
            found.append(rounded)

    for match in _RANGE_RE.finditer(text):
        if _NUTRITION_CTX.search(_line_prefix(text, match.start())):
            continue
        unit = match.group(3)
        add(_to_grams(_to_float(match.group(1)), unit))
        add(_to_grams(_to_float(match.group(2)), unit))
    for match in _WEIGHT_RE.finditer(text):
        prefix = _line_prefix(text, match.start())
        if _NUTRITION_CTX.search(prefix):
            continue
        grams = _to_grams(_to_float(match.group(2)), match.group(3))
        if grams == 100 and re.search(r"(pour|per|par|for|/)\s*$", prefix, re.IGNORECASE):
            continue
        add(grams)
    return found


def strip_weights(text: str) -> str:
    """Retire les expressions de grammage (utile pour isoler le nom du produit)."""
    without_ranges = _RANGE_RE.sub(" ", text)
    return _WEIGHT_RE.sub(" ", without_ranges)


def weight_proximity(target_g: float, candidates: list[float], tolerance: float) -> float:
    """1.0 si égal (±2 %), décroît linéairement jusqu'à 0 à ``tolerance`` (relative)."""
    if target_g <= 0 or not candidates:
        return 0.0
    best = 0.0
    for value in candidates:
        gap = abs(value - target_g) / target_g
        if gap <= 0.02:
            return 1.0
        if gap < tolerance:
            best = max(best, 1.0 - gap / tolerance)
    return best


# ---------------------------------------------------------------------------
# Références et conditionnements
# ---------------------------------------------------------------------------

_LABELED_REF = re.compile(
    r"\b(?:r[ée]f(?:[ée]rence)?s?|code(?:\s+article)?|art(?:icle)?|sku|item|ean|gtin)\b\.?\s*[:#°n]*\s*"
    r"([A-Z0-9][A-Z0-9\-_./]{3,19})",
    re.IGNORECASE,
)
_EAN13 = re.compile(r"(?<!\d)(\d{13})(?!\d)")
_ALNUM_CODE = re.compile(r"\b([A-Z]{2,5}-?\d{3,8}[A-Z]?)\b")
_LINE_START_CODE = re.compile(r"^\s*(\d{5,8})\s+[A-Za-zÀ-ÿ]", re.MULTILINE)
_REF_PREFIX_BLACKLIST = ("ISO", "IFS", "BRC", "FSSC", "HACCP", "DIN", "EN", "NF", "IGP", "AOP")


def normalize_ref(ref: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", ref.upper())


def extract_references(text: str) -> list[str]:
    """Références article / EAN trouvées dans ``text`` (normalisées en majuscules)."""
    found: list[str] = []

    def add(raw: str) -> None:
        ref = normalize_ref(raw)
        if len(ref) >= 4 and any(ch.isdigit() for ch in ref) and ref not in found:
            found.append(ref)

    for match in _LABELED_REF.finditer(text):
        add(match.group(1))
    for match in _EAN13.finditer(text):
        add(match.group(1))
    for match in _ALNUM_CODE.finditer(text):
        code = match.group(1)
        if not code.upper().startswith(_REF_PREFIX_BLACKLIST):
            add(code)
    for match in _LINE_START_CODE.finditer(text):
        add(match.group(1))
    return found[:8]


_PACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:carton|colis|caisse|box|case|sachet|sac|bag|barquette|tray|pack|lot|palette|pallet)\s*"
        r"(?:de|of|:)?\s*\d{1,4}(?:\s*x\s*\d{1,4})?(?:\s*(?:pi[eè]ces?|pcs?|unit[eé]s?|portions?))?",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d{1,4}\s*(?:pi[eè]ces?|pcs?|pces?|unit[eé]s?|st[uü]ck|units?)\b", re.IGNORECASE),
    re.compile(r"\bpar\s+\d{2,4}\b", re.IGNORECASE),
)


def extract_packaging(text: str) -> list[str]:
    """Conditionnements lisibles (« carton de 120 », « 24 pièces »...)."""
    found: list[str] = []
    for pattern in _PACK_PATTERNS:
        for match in pattern.finditer(text):
            snippet = re.sub(r"\s+", " ", match.group(0).strip().lower())
            if snippet not in found:
                found.append(snippet)
    for match in _WEIGHT_RE.finditer(text):
        if match.group(1):
            snippet = f"{match.group(1)} x {match.group(2)} {match.group(3).lower()}"
            if snippet not in found:
                found.append(snippet)
    return found[:5]


def packaging_numbers(packaging: list[str]) -> set[int]:
    numbers: set[int] = set()
    for item in packaging:
        for raw in re.findall(r"\d{1,4}", item):
            numbers.add(int(raw))
    return numbers


# ---------------------------------------------------------------------------
# Familles produit multilingues
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Family:
    key: str
    label_fr: str
    label_en: str
    category: str  # viennoiserie | patisserie | pain | autre
    terms: tuple[str, ...]  # expressions déjà « humaines » ; normalisées au chargement


def _fam(key: str, fr: str, en: str, category: str, *terms: str) -> Family:
    normalized = tuple(dict.fromkeys(normalize(t) for t in (key, fr, en, *terms) if t))
    return Family(key, fr, en, category, normalized)


FAMILIES: tuple[Family, ...] = (
    _fam("danish", "danish", "danish pastry", "viennoiserie",
         "danishes", "mini danish", "mini danishes", "pate danoise", "viennoiserie danoise",
         "pâtisserie danoise", "danoise", "danoises", "plunder", "plundergebäck", "kopenhagener",
         "pasteles daneses", "danese", "dolce danese", "deens gebak", "wienerbrod", "wienerbrød"),
    _fam("croissant", "croissant", "croissant", "viennoiserie",
         "croissants", "croissant au beurre", "butter croissant", "mini croissant", "hörnchen",
         "cornetto", "croissant pur beurre", "croissant surgelé"),
    _fam("pain au chocolat", "pain au chocolat", "chocolate croissant", "viennoiserie",
         "chocolatine", "pains au chocolat", "chocolate pastry", "schokoladencroissant",
         "napolitana de chocolate", "cornetto al cioccolato"),
    _fam("pain aux raisins", "pain aux raisins", "raisin swirl", "viennoiserie",
         "escargot aux raisins", "raisin danish", "pains aux raisins"),
    _fam("brioche", "brioche", "brioche", "viennoiserie",
         "brioches", "mini brioche", "brioche tressée", "butter brioche"),
    _fam("viennoiserie", "viennoiserie", "viennoiserie", "viennoiserie",
         "viennoiseries", "pain suisse", "pains suisses", "pastry", "pastries", "breakfast pastry", "laminated dough",
         "pâte feuilletée levée", "gebäck", "feinbackwaren", "bollería", "bolleria", "pasticceria da forno"),
    _fam("chausson", "chausson aux pommes", "apple turnover", "viennoiserie",
         "chaussons", "turnover", "apfeltasche", "tarta de manzana hojaldrada"),
    _fam("feuillete", "feuilleté", "puff pastry", "patisserie",
         "feuilletés", "pâte feuilletée", "blätterteig", "hojaldre", "pasta sfoglia", "bladerdeeg"),
    _fam("eclair", "éclair", "eclair", "patisserie", "éclairs", "choux", "profiterole", "windbeutel"),
    _fam("tarte", "tarte", "tart", "patisserie", "tartelette", "tartelettes", "tarts", "pie", "tarta", "torta"),
    _fam("muffin", "muffin", "muffin", "patisserie", "muffins", "cupcake", "cupcakes", "magdalena", "magdalenas"),
    _fam("cookie", "cookie", "cookie", "patisserie", "cookies", "biscuit", "biscuits", "keks", "galleta", "galletas"),
    _fam("cake", "cake", "cake", "patisserie", "cakes", "gâteau", "gateau", "gâteaux", "kuchen", "bizcocho", "torta"),
    _fam("donut", "beignet", "donut", "patisserie", "donuts", "doughnut", "doughnuts", "beignets", "berliner", "krapfen"),
    _fam("gaufre", "gaufre", "waffle", "patisserie", "gaufres", "waffles", "waffel", "waffeln", "gofre", "wafel"),
    _fam("macaron", "macaron", "macaron", "patisserie", "macarons"),
    _fam("baguette", "baguette", "baguette", "pain", "baguettes", "pain baguette", "demi baguette"),
    _fam("pain", "pain", "bread", "pain", "pains", "breads", "brot", "pan", "pane", "brood", "bake off", "bake-off"),
    _fam("pain burger", "pain burger", "burger bun", "pain",
         "pain hamburger", "buns", "burger buns", "hamburger bun", "hamburgerbrötchen", "pan de hamburguesa"),
    _fam("ciabatta", "ciabatta", "ciabatta", "pain", "ciabattas", "panini", "focaccia"),
    _fam("wrap", "wrap", "wrap", "pain", "wraps", "tortilla", "tortillas", "galette de blé"),
    _fam("pizza", "pizza", "pizza", "autre", "pizzas", "pâte à pizza", "pizza dough", "pizzateig", "masa de pizza"),
    _fam("sandwich", "sandwich", "sandwich", "autre", "sandwichs", "sandwiches", "snack", "snacking"),
)

MODIFIERS: dict[str, tuple[str, ...]] = {
    "mini": ("mini", "minis", "petit", "petite", "petits", "petites", "small", "bite size", "bitesize", "mignon", "mignons", "klein", "kleine", "piccolo"),
    "maxi": ("maxi", "grand", "grande", "large", "big", "xl"),
    "surgele": ("surgele", "surgeles", "surgelee", "frozen", "tiefgekuhlt", "congelado", "surgelato"),
    "precuit": ("precuit", "precuits", "pre cuit", "pre baked", "prebaked", "par baked", "parbaked", "bake off"),
    "pret a cuire": ("pret a cuire", "ready to bake", "rtb", "pret a enfourner", "ready to bake"),
    "pur beurre": ("pur beurre", "butter", "beurre", "all butter"),
    "bio": ("bio", "biologique", "organic", "organique", "oko"),
    "vegan": ("vegan", "vegetal", "vegetalien", "plant based"),
    "sans gluten": ("sans gluten", "gluten free", "glutenfrei", "sin gluten", "senza glutine"),
    "halal": ("halal",),
    "private label": ("private label", "mdd", "marque distributeur", "marque de distributeur", "marque blanche"),
    "assortiment": ("assortiment", "assorti", "assorted", "assortment", "selection"),
}

# Expression de chaque attribut dans les requêtes Internet : (anglais, français)
MODIFIER_QUERY: dict[str, tuple[str, str]] = {
    "surgele": ("frozen", "surgelé"), "precuit": ("par-baked", "précuit"), "pret a cuire": ("ready to bake", "prêt à cuire"),
    "pur beurre": ("all butter", "pur beurre"), "bio": ("organic", "bio"), "vegan": ("vegan", "végétal"),
    "sans gluten": ("gluten free", "sans gluten"), "halal": ("halal", "halal"),
    "private label": ("private label", "marque distributeur"), "assortiment": ("assorted mix", "assortiment"),
}

def _build_family_index() -> list[tuple[str, tuple[Family, ...]]]:
    by_term: dict[str, list[Family]] = {}
    for fam in FAMILIES:
        for term in fam.terms:
            by_term.setdefault(term, []).append(fam)
    return sorted(((t, tuple(f)) for t, f in by_term.items()), key=lambda pair: -len(pair[0]))


_FAMILY_INDEX = _build_family_index()

CATEGORY_LABELS: dict[str, tuple[str, str]] = {
    "viennoiserie": ("viennoiserie", "viennoiserie pastry"),
    "patisserie": ("pâtisserie", "pastry bakery"),
    "pain": ("pain boulangerie", "bread bakery"),
    "autre": ("agroalimentaire", "food"),
}


def families_in_text(norm_text: str) -> list[Family]:
    """Familles dont au moins un terme apparaît dans ``norm_text`` (déjà normalisé).

    Les termes les plus longs sont testés d'abord et « consomment » leurs mots : « danish pastry »
    désigne la famille danish et ne déclenche pas en plus la famille générique « pastry ».
    """
    padded = f" {norm_text} "
    seen: dict[str, Family] = {}
    for term, families in _FAMILY_INDEX:
        marker = f" {term} "
        if marker in padded:
            for fam in families:
                seen.setdefault(fam.key, fam)
            padded = padded.replace(marker, " " + "#" * len(term) + " ")
    return list(seen.values())


# ---------------------------------------------------------------------------
# Analyse d'une requête acheteur
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryPlan:
    """Besoin acheteur structuré, partagé par la recherche interne et la recherche web."""

    raw: str
    product_text: str  # requête sans grammage ni références
    tokens: tuple[str, ...]  # tokens importants (sans mots vides)
    weights_g: tuple[float, ...]
    references: tuple[str, ...]
    packaging_numbers: tuple[int, ...]
    families: tuple[Family, ...]
    modifiers: tuple[str, ...]
    categories: tuple[str, ...]
    norm_phrase: str = ""
    synonyms: tuple[str, ...] = field(default_factory=tuple)
    countries: tuple[str, ...] = ()  # pays cités dans la demande (préférence, pas un filtre strict)

    @property
    def weight_g(self) -> float | None:
        return self.weights_g[0] if self.weights_g else None

    @property
    def is_empty(self) -> bool:
        return not (self.tokens or self.references)

    def cache_key(self) -> str:
        fam = ",".join(sorted(f.key for f in self.families))
        return f"{self.norm_phrase}|{self.weights_g}|{self.references}|{fam}|{','.join(self.countries)}"


def build_query_plan(query: str) -> QueryPlan:
    """Transforme « Mini Danish 40 g » en besoin structuré (tokens, grammage, familles...)."""
    raw = (query or "").strip()
    weights = tuple(extract_weights(raw))
    references = tuple(extract_references(raw))
    pack_numbers = tuple(sorted(packaging_numbers(extract_packaging(raw))))
    product_text = strip_weights(raw)
    for ref in references:
        product_text = re.sub(re.escape(ref), " ", product_text, flags=re.IGNORECASE)
    norm_phrase = normalize(product_text)
    countries, without_countries = countries_in_request(norm_phrase)
    if countries:  # « Italie » est une contrainte de pays, pas un mot à retrouver dans les catalogues
        norm_phrase, product_text = without_countries, without_countries
    tokens = tuple(dict.fromkeys(tokenize(product_text)))
    families = tuple(families_in_text(norm_phrase))
    modifiers = tuple(
        key for key, variants in MODIFIERS.items()
        if any(contains_phrase(norm_phrase, normalize(v)) for v in variants)
    )
    categories = tuple(dict.fromkeys(f.category for f in families))
    synonyms: list[str] = []
    for fam in families:
        for term in fam.terms:
            if term not in synonyms and term != norm_phrase:
                synonyms.append(term)
    for key in modifiers:
        for variant in MODIFIERS[key]:
            norm_variant = normalize(variant)
            if norm_variant not in synonyms:
                synonyms.append(norm_variant)
    return QueryPlan(
        raw=raw,
        product_text=re.sub(r"\s+", " ", product_text).strip(),
        tokens=tokens,
        weights_g=weights,
        references=references,
        packaging_numbers=pack_numbers,
        families=families,
        modifiers=modifiers,
        categories=categories,
        norm_phrase=norm_phrase,
        synonyms=tuple(synonyms),
        countries=tuple(countries),
    )
