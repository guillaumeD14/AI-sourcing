"""Texte : normalisation, extraction (grammage, références, conditionnement), familles produit
multilingues et construction d'un plan de requête explicable.

Aucune dépendance externe : ce module est pur et entièrement testable.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, replace

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_SPECIAL = str.maketrans({"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae", "ß": "ss", "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "đ": "d"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Minuscules, sans accents, ponctuation -> espace. ``Plundergebäck`` -> ``plundergeback``."""
    text = unicodedata.normalize("NFKD", (text or "").translate(_SPECIAL))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return _NON_ALNUM.sub(" ", text).strip()


STOPWORDS = frozenset(
    """le la les l un une des de du d et ou en au aux a à pour par sur avec sans dans ce cette ces son sa ses
    the a an of and or for to in on with without by from at is are
    der die das den dem des ein eine und oder fur von mit ohne im am zu
    il lo la i gli le un uno una di da del della dei e o per con senza su
    el los las unos unas y o por con sin del al
    g gr kg kgs mg ml cl x pcs pc approx env environ max min""".split()
)
_UNIT_TOKENS = frozenset({"g", "gr", "grs", "kg", "kgs", "gramme", "grammes", "gram", "grams", "mg", "ml", "cl", "l"})
_REF_LABEL_WORDS = frozenset({"ref", "refs", "reference", "references", "art", "article", "artikel", "item", "sku", "code", "cod", "nr", "no"})
_NUMERIC_TOKEN = re.compile(r"\d+(?:g|gr|grs|kg|ml|cl|l|mg)?(?:x\d+)?|\d+x\d+(?:g|gr|kg)?")


def tokenize(text: str) -> list[str]:
    """Jetons normalisés, sans mots vides (les nombres sont conservés)."""
    return [t for t in normalize(text).split() if t not in STOPWORDS and len(t) > 1 or t.isdigit()]


def content_hash(text: str) -> str:
    return hashlib.sha1(normalize(text).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Extraction : grammage, références, conditionnement
# ---------------------------------------------------------------------------

_NUM = r"(\d{1,5}(?:[.,]\d{1,3})?)"
_UNIT = r"(kg|kgs|g|gr|grs|gramme|grammes|grams|gram)"
_BEFORE = r"(?<![\d.,A-WYZa-wyz])"  # pas collé à un nombre ou un mot, mais « 24x40g » reste valide
_WEIGHT_RE = re.compile(rf"{_BEFORE}{_NUM}\s*{_UNIT}\b", re.I)
_MULT_BEFORE_RE = re.compile(rf"(?<![\d.,])(\d{{1,4}})\s*[x×*]\s*{_NUM}\s*{_UNIT}\b", re.I)
_MULT_AFTER_RE = re.compile(rf"{_BEFORE}{_NUM}\s*{_UNIT}\s*[x×*]\s*(\d{{1,4}})\b", re.I)
_PER_PIECE_RE = re.compile(
    rf"{_BEFORE}{_NUM}\s*{_UNIT}\s*(?:/|par|per|pro|each|la)\s*(?:pc|pce|pi[eè]ce|piece|stk|st[uü]ck|unit[eé]?|unit|pz)\b", re.I)
_NUTRITION_WORDS = re.compile(
    r"(sel|salt|sucre|sugar|sucres|graisse|graisses|fat|lipide|lipides|prot[eé]ine|protein|glucide|glucides|carbohydrate|"
    r"fibre|fibres|[eé]nergie|energy|kcal|kj|dont|of which|saturated|satur[eé]s|fett|zucker|eiwei[sß]|salz|davon|"
    r"pour 100|per 100|pro 100|para 100|per cento)\s*[:\-]?\s*$", re.I)
_PER_100_RE = re.compile(r"(?:pour|per|pro|par|para|por|x)\s*$", re.I)


def _to_grams(value: str, unit: str) -> float | None:
    number = float(value.replace(",", "."))
    grams = number * 1000 if unit.lower().startswith("kg") else number
    return round(grams, 2) if 5 <= grams <= 25000 else None


def _is_nutrition_context(text: str, start: int, grams: float) -> bool:
    before = text[max(0, start - 28):start]
    if _NUTRITION_WORDS.search(before):
        return True
    return grams == 100 and bool(_PER_100_RE.search(before))


def extract_weights(text: str) -> tuple[list[float], list[float]]:
    """Retourne ``(tous_les_poids_g, poids_unitaires_g)``.

    ``24 x 40 g`` donne le poids unitaire 40 g (et non le total). Les lignes de valeurs nutritionnelles
    (« sel : 1,2 g », « pour 100 g ») sont ignorées.
    """
    unit_weights: list[float] = []
    for regex, group in ((_MULT_BEFORE_RE, (2, 3)), (_MULT_AFTER_RE, (1, 2)), (_PER_PIECE_RE, (1, 2))):
        for match in regex.finditer(text):
            grams = _to_grams(match.group(group[0]), match.group(group[1]))
            if grams is not None and not _is_nutrition_context(text, match.start(), grams):
                unit_weights.append(grams)
    all_weights = list(unit_weights)
    for match in _WEIGHT_RE.finditer(text):
        grams = _to_grams(match.group(1), match.group(2))
        if grams is not None and not _is_nutrition_context(text, match.start(), grams):
            all_weights.append(grams)
    return _unique(all_weights), _unique(unit_weights)


def _unique(values: list[float]) -> list[float]:
    seen: list[float] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


_REF_LABEL_RE = re.compile(
    r"(?:\br[ée]f(?:[ée]rence)?s?\b|\bart(?:icle|ikel|\.)?(?:\s*-?\s*n[ro°.]*)?|\bitem\b|\bsku\b|\bcode(?:\s+produit)?\b|\bcod\.|"
    r"\bn[°o]\s*produit\b|\bproduktnr\.?)\s*[.:#°]*\s*([A-Z0-9][A-Z0-9\-./]{2,19})", re.I)
_CODE_RE = re.compile(r"\b([A-Z]{1,4}-?\d{3,8}[A-Z]?)\b")
_NOT_REF = re.compile(r"^(?:ISO|FSSC|BRC|IFS|HACCP|E\d{3}|EN\d|DIN|AOC|IGP|AOP|TVA|SIRET|IBAN)", re.I)


def extract_refs(text: str, *, labelled_only: bool = False) -> list[str]:
    """Références produit : après un libellé (« Réf. », « Art.-Nr. ») ou codes lettres+chiffres autonomes."""
    refs: list[str] = []
    for match in _REF_LABEL_RE.finditer(text):
        candidate = match.group(1).strip(".-/").upper()
        if any(c.isdigit() for c in candidate) and not _NOT_REF.match(candidate):
            refs.append(candidate)
    if not labelled_only:
        for match in _CODE_RE.finditer(text):
            candidate = match.group(1).upper()
            if not _NOT_REF.match(candidate):
                refs.append(candidate)
    return list(dict.fromkeys(refs))[:6]


_PACKAGING_RES = [
    re.compile(r"\b\d{1,4}\s*[x×*]\s*\d{1,5}(?:[.,]\d{1,3})?\s*(?:g|gr|kg|ml|cl|l)?\b", re.I),
    re.compile(r"\b(?:carton|colis|caisse|box|karton|cartone|caja|case)\s*(?:de|of|von|da|con)?\s*\d{1,4}\b", re.I),
    re.compile(r"\b\d{1,4}\s*(?:pcs|pc|pi[eè]ces?|pieces?|unit[eé]s?|units?|st[uü]ck|stk|pz|uds?)\b", re.I),
    re.compile(r"\b(?:sachet|plateau|barquette|tray|bag|palette|pallet)s?\s*(?:de|of)?\s*\d{1,4}\b", re.I),
]


def extract_packaging(text: str) -> list[str]:
    found: list[str] = []
    for regex in _PACKAGING_RES:
        for match in regex.finditer(text):
            value = re.sub(r"\s+", " ", match.group(0)).strip()
            if value.lower() not in (f.lower() for f in found):
                found.append(value)
    return found[:4]


def format_weight(grams: float) -> str:
    return f"{grams:g} g"


# ---------------------------------------------------------------------------
# Familles de produits et modificateurs (FR / EN / DE / IT / ES)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Family:
    id: str
    label_fr: str
    terms: tuple[str, ...]          # formes normalisées (pour la détection)
    names: dict[str, str]           # nom d'affichage par langue (pour les requêtes web)
    generic: bool = False


def _family(fid: str, label_fr: str, *, fr: str, en: str, de: str = "", it: str = "", es: str = "",
            more: tuple[str, ...] = (), generic: bool = False) -> Family:
    names = {k: v for k, v in {"fr": fr, "en": en, "de": de, "it": it, "es": es}.items() if v}
    terms = tuple(dict.fromkeys(t for t in (normalize(x) for x in (*names.values(), *more)) if t))
    return Family(fid, label_fr, terms, names, generic)


FAMILIES: tuple[Family, ...] = (
    _family("danish", "Danish / viennoiserie danoise", fr="viennoiserie danoise", en="danish pastry", de="Plundergebäck", it="pasta danese",
            es="pastel danés", more=("danish", "mini danish", "mini danoise", "danoise", "danois", "danese", "danesa", "kopenhagener", "plunder")),
    _family("croissant", "Croissant", fr="croissant", en="croissant", de="Buttercroissant", it="cornetto", es="croissant",
            more=("croissants", "hornchen", "cornetti", "mini croissant")),
    _family("pain_chocolat", "Pain au chocolat", fr="pain au chocolat", en="chocolate croissant", de="Schokocroissant",
            it="cornetto al cioccolato", es="napolitana de chocolate", more=("chocolatine", "napolitana", "pain choc")),
    _family("pain_raisin", "Pain aux raisins", fr="pain aux raisins", en="raisin swirl", de="Rosinenschnecke",
            more=("raisin roll", "raisin danish", "pan de pasas", "escargot raisin")),
    _family("brioche", "Brioche", fr="brioche", en="brioche", de="Brioche", it="brioche", es="brioche", more=("brioches", "brioche bun")),
    _family("chausson", "Chausson / feuilleté fourré", fr="chausson aux pommes", en="apple turnover", de="Apfeltasche",
            it="fagottino alle mele", es="empanadilla de manzana", more=("chausson", "turnover", "turnovers", "fagottino")),
    _family("feuillete", "Pâte feuilletée / feuilletés", fr="feuilleté", en="puff pastry", de="Blätterteig", it="pasta sfoglia",
            es="hojaldre", more=("feuilletee", "laminated dough", "sfoglia", "pate feuilletee", "laminated pastry")),
    _family("muffin", "Muffin / cupcake", fr="muffin", en="muffin", de="Muffin", it="muffin", es="magdalena", more=("muffins", "cupcake", "cupcakes")),
    _family("cookie", "Cookie / biscuit", fr="cookie", en="cookie", de="Keks", it="biscotto", es="galleta", more=("cookies", "biscuit", "biscuits")),
    _family("eclair", "Éclair / choux", fr="éclair", en="eclair", de="Windbeutel", it="bigné", es="eclair",
            more=("eclairs", "choux", "profiterole", "profiteroles", "choux pastry", "religieuse")),
    _family("millefeuille", "Mille-feuille", fr="mille-feuille", en="mille-feuille", de="Cremeschnitte", it="millefoglie",
            es="milhojas", more=("millefeuille", "napoleon", "napoleons")),
    _family("tarte", "Tarte / tartelette", fr="tartelette", en="tartlet", de="Törtchen", it="crostatina", es="tartaleta",
            more=("tarte", "tartes", "tartelettes", "tart", "tarts", "tartlets", "crostata", "tarta", "mini tarte")),
    _family("baguette", "Baguette", fr="baguette", en="baguette", de="Baguette", it="baguette", es="baguette", more=("baguettes", "demi baguette")),
    _family("burger_bun", "Pain burger / bun", fr="pain burger", en="burger bun", de="Burgerbrötchen", it="panino hamburger",
            es="pan de hamburguesa", more=("hamburger bun", "buns", "bun", "burger buns")),
    _family("bagel", "Bagel", fr="bagel", en="bagel", de="Bagel", it="bagel", es="bagel", more=("bagels",)),
    _family("donut", "Donut / beignet", fr="beignet", en="donut", de="Berliner", it="ciambella", es="donut",
            more=("donuts", "doughnut", "doughnuts", "beignets", "krapfen")),
    _family("gaufre", "Gaufre", fr="gaufre", en="waffle", de="Waffel", it="waffel", es="gofre", more=("gaufres", "waffles", "waffeln")),
    _family("madeleine", "Madeleine", fr="madeleine", en="madeleine", de="Madeleine", it="madeleine", es="magdalena francesa",
            more=("madeleines",)),
    _family("palmier", "Palmier", fr="palmier", en="palmier", de="Schweineohr", it="ventaglio", es="palmera",
            more=("palmiers", "elephant ear", "palmeras")),
    _family("kouign", "Kouign-amann", fr="kouign-amann", en="kouign amann", more=("kouign amann", "kouignamann")),
    _family("cinnamon", "Roulé à la cannelle", fr="roulé à la cannelle", en="cinnamon roll", de="Zimtschnecke",
            more=("cinnamon swirl", "cinnamon rolls", "roule cannelle", "zimtschnecken")),
    _family("cake", "Cake / quatre-quarts", fr="quatre-quarts", en="pound cake", de="Rührkuchen", it="plumcake",
            es="bizcocho", more=("cake", "cakes", "plum cake", "loaf cake", "gateau", "gateaux")),
    _family("quiche", "Quiche / salé feuilleté", fr="mini quiche", en="mini quiche", de="Quiche", it="quiche", es="quiche",
            more=("quiche", "quiches", "friand", "friands", "sausage roll", "feuillete salé", "feuillete sale")),
    _family("pizza", "Pizza / snacking salé", fr="pizza", en="pizza", de="Pizza", it="pizzetta", es="pizza",
            more=("pizzas", "pizzetta", "focaccia", "snacking")),
    _family("bretzel", "Bretzel", fr="bretzel", en="pretzel", de="Brezel", it="pretzel", es="pretzel", more=("bretzels", "laugen", "laugenbrezel")),
    _family("pain", "Pain / petits pains", fr="pain", en="bread", de="Brot", it="pane", es="pan",
            more=("petit pain", "petits pains", "bread roll", "rolls", "brotchen", "panino", "loaf"), generic=True),
    _family("viennoiserie", "Viennoiserie / pâtisserie (générique)", fr="viennoiserie", en="viennoiserie", de="Feingebäck",
            it="pasticceria", es="bollería", more=("viennoiseries", "pastry", "pastries", "sweet pastry", "gebäck", "bolleria", "patisserie", "patisseries"),
            generic=True),
)
FAMILY_BY_ID = {f.id: f for f in FAMILIES}


@dataclass(frozen=True)
class Modifier:
    id: str
    label_fr: str
    terms: tuple[str, ...]
    query_en: str = ""
    query_fr: str = ""


def _modifier(mid: str, label: str, terms: tuple[str, ...], en: str = "", fr: str = "") -> Modifier:
    return Modifier(mid, label, tuple(dict.fromkeys(normalize(t) for t in terms if normalize(t))), en, fr)


MODIFIERS: tuple[Modifier, ...] = (
    _modifier("surgele", "surgelé", ("surgelé", "surgelés", "congelé", "frozen", "tiefgekühlt", "tiefkühl", "tk", "surgelato", "congelato",
                                      "congelado", "deep frozen", "frozen bakery"), "frozen", "surgelé"),
    _modifier("precuit", "précuit", ("précuit", "pre baked", "prebaked", "par baked", "parbaked", "vorgebacken", "precotto", "precocido",
                                      "bake off", "bake-off"), "par-baked", "précuit"),
    _modifier("cru", "cru / prêt à cuire", ("cru", "prêt à cuire", "ready to bake", "rtb", "teigling", "teiglinge", "pronto da cuocere",
                                             "listo para hornear", "pret a cuire", "pousse"), "ready to bake", "prêt à cuire"),
    _modifier("pur_beurre", "pur beurre", ("pur beurre", "all butter", "100% butter", "butter", "beurre", "burro", "mantequilla"), "all butter", "pur beurre"),
    _modifier("vegan", "végan / végétal", ("vegan", "végan", "végétal", "plant based", "plant-based", "pflanzlich", "vegano"), "vegan", "végan"),
    _modifier("bio", "bio", ("bio", "organic", "biologique", "ökologisch", "biologico", "ecologico"), "organic", "bio"),
    _modifier("sans_gluten", "sans gluten", ("sans gluten", "gluten free", "glutenfrei", "senza glutine", "sin gluten"), "gluten free", "sans gluten"),
    _modifier("fourre", "fourré", ("fourré", "fourre", "filled", "gefüllt", "ripieno", "relleno", "garni"), "filled", "fourré"),
    _modifier("chocolat", "chocolat", ("chocolat", "chocolate", "schokolade", "cioccolato", "chocolate"), "chocolate", "chocolat"),
    _modifier("pomme", "pomme", ("pomme", "apple", "apfel", "mela", "manzana"), "apple", "pomme"),
    _modifier("framboise", "framboise", ("framboise", "raspberry", "himbeere", "lampone", "frambuesa"), "raspberry", "framboise"),
    _modifier("vanille", "vanille / crème", ("vanille", "vanilla", "creme patissiere", "custard", "pastry cream", "vanillecreme", "crema pasticcera"), "custard", "crème pâtissière"),
    _modifier("raisin", "raisin", ("raisin", "raisins", "sultana", "rosinen", "uvetta"), "raisin", "raisin"),
    _modifier("amande", "amande / noisette", ("amande", "almond", "mandel", "mandorla", "almendra", "noisette", "hazelnut", "haselnuss"), "almond", "amande"),
    _modifier("mini", "mini", ("mini", "minis", "petit format", "bite size", "bite-size"), "mini", "mini"),
    _modifier("assortiment", "assortiment", ("assortiment", "assorti", "assortis", "assorted", "mix", "assortment", "sortiment", "assortimento"), "assorted", "assortiment"),
    _modifier("private_label", "private label / MDD", ("private label", "mdd", "marque distributeur", "marque de distributeur", "eigenmarke",
                                                        "handelsmarke", "marca blanca", "marchio del distributore", "co packing", "co-packing",
                                                        "contract manufacturing", "white label"), "private label", "marque distributeur"),
    _modifier("foodservice", "foodservice / RHD", ("foodservice", "food service", "rhd", "restauration hors domicile", "gastronomie", "ristorazione",
                                                    "hosteleria", "horeca", "b2b", "catering"), "foodservice", "foodservice"),
)
MODIFIER_BY_ID = {m.id: m for m in MODIFIERS}

COUNTRIES: dict[str, tuple[str, ...]] = {
    "FR": ("france", "francais", "francaise"), "IT": ("italie", "italy", "italia", "italien", "italiano"),
    "DE": ("allemagne", "germany", "deutschland", "allemand"), "ES": ("espagne", "spain", "espana", "espagnol"),
    "NL": ("pays bas", "netherlands", "holland", "nederland", "hollande"), "BE": ("belgique", "belgium", "belgie", "belgien"),
    "PL": ("pologne", "poland", "polska"), "DK": ("danemark", "denmark", "danmark"), "SE": ("suede", "sweden", "sverige"),
    "CH": ("suisse", "switzerland", "schweiz", "svizzera"), "AT": ("autriche", "austria", "osterreich"),
    "GB": ("royaume uni", "united kingdom", "uk", "angleterre", "england", "britain", "scotland"), "IE": ("irlande", "ireland"),
    "PT": ("portugal",), "GR": ("grece", "greece"), "HU": ("hongrie", "hungary"), "CZ": ("tchequie", "czech", "republique tcheque"),
    "RO": ("roumanie", "romania"), "HR": ("croatie", "croatia"), "SI": ("slovenie", "slovenia"), "SK": ("slovaquie", "slovakia"),
    "NO": ("norvege", "norway"), "FI": ("finlande", "finland"), "LT": ("lituanie", "lithuania"), "LV": ("lettonie", "latvia"),
    "EE": ("estonie", "estonia"), "BG": ("bulgarie", "bulgaria"), "LU": ("luxembourg",),
}
COUNTRY_LABELS = {
    "FR": "France", "IT": "Italie", "DE": "Allemagne", "ES": "Espagne", "NL": "Pays-Bas", "BE": "Belgique", "PL": "Pologne",
    "DK": "Danemark", "SE": "Suède", "CH": "Suisse", "AT": "Autriche", "GB": "Royaume-Uni", "IE": "Irlande", "PT": "Portugal",
    "GR": "Grèce", "HU": "Hongrie", "CZ": "Tchéquie", "RO": "Roumanie", "HR": "Croatie", "SI": "Slovénie", "SK": "Slovaquie",
    "NO": "Norvège", "FI": "Finlande", "LT": "Lituanie", "LV": "Lettonie", "EE": "Estonie", "BG": "Bulgarie", "LU": "Luxembourg",
    "TR": "Turquie", "RS": "Serbie", "UA": "Ukraine",
}


def _contains(norm_text: str, term: str) -> bool:
    """Terme entier dans un texte normalisé (pluriels simples tolérés)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?:s|x|es|en|n)?(?![a-z0-9])", norm_text) is not None


contains_term = _contains


def detect_families(norm_text: str) -> list[Family]:
    return [f for f in FAMILIES if any(_contains(norm_text, t) for t in f.terms)]


def matched_terms(norm_text: str, terms: tuple[str, ...]) -> list[str]:
    return [t for t in terms if _contains(norm_text, t)]


def detect_modifiers(norm_text: str) -> list[Modifier]:
    return [m for m in MODIFIERS if any(_contains(norm_text, t) for t in m.terms)]


def detect_countries(norm_text: str) -> list[str]:
    return [iso for iso, names in COUNTRIES.items() if any(_contains(norm_text, n) for n in names)]


# ---------------------------------------------------------------------------
# Plan de requête
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryPlan:
    raw: str
    product_text: str
    weight_g: float | None
    references: tuple[str, ...]
    modifiers: tuple[Modifier, ...]
    countries: tuple[str, ...]
    families: tuple[Family, ...]
    tokens: tuple[str, ...]                       # jetons « produit » (hors poids, pays, modificateurs)
    search_terms: tuple[str, ...] = field(default=())  # termes élargis (synonymes) pour la recherche plein texte
    packaging: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.tokens or self.families or self.references)

    def cache_key(self) -> str:
        return hashlib.sha1(normalize(self.raw).encode()).hexdigest()[:16]

    @property
    def specific_families(self) -> tuple[Family, ...]:
        """Familles non génériques ; à défaut, toutes les familles détectées."""
        specific = tuple(f for f in self.families if not f.generic)
        return specific or self.families


def build_query_plan(query: str) -> QueryPlan:
    """Analyse un besoin libre (produit + description + grammage + critères) en plan structuré."""
    norm = normalize(query)
    weights, unit_weights = extract_weights(query)
    weight = unit_weights[0] if unit_weights else (weights[0] if weights else None)
    families = detect_families(norm)
    modifiers = detect_modifiers(norm)
    countries = detect_countries(norm)
    refs = extract_refs(query, labelled_only=True)

    skip: set[str] = set(_REF_LABEL_WORDS)
    for ref in refs:
        skip.update(normalize(ref).split())
    for modifier in modifiers:
        for term in matched_terms(norm, modifier.terms):
            skip.update(term.split())
    for iso in countries:
        for name in matched_terms(norm, COUNTRIES[iso]):
            skip.update(name.split())
    tokens: list[str] = []
    for token in tokenize(query):
        if _NUMERIC_TOKEN.fullmatch(token) or token in _UNIT_TOKENS or token in skip or token in tokens:
            continue
        tokens.append(token)
    # les jetons des familles non génériques passent en premier : ils portent le sens du produit
    family_tokens = [t for f in families if not f.generic for term in matched_terms(norm, f.terms) for t in term.split() if t not in STOPWORDS]
    ordered = list(dict.fromkeys([*family_tokens, *tokens]))[:15]

    longest = ""
    for family in families:
        for term in matched_terms(norm, family.terms):
            if len(term) > len(longest) and not family.generic:
                longest = term
    product_text = longest or " ".join(ordered[:6])
    packaging = tuple(extract_packaging(query))
    plan = QueryPlan(query, product_text, weight, tuple(refs), tuple(modifiers), tuple(countries), tuple(families),
                     tuple(ordered), (), packaging)
    return replace(plan, search_terms=tuple(expand_search_terms(plan)))


def expand_search_terms(plan: QueryPlan, limit: int = 40) -> list[str]:
    """Termes pour la recherche plein texte : jetons du besoin + synonymes multilingues des familles."""
    terms: list[str] = list(plan.tokens)
    for family in plan.specific_families:
        terms.extend(family.terms)
    for modifier in plan.modifiers:
        if modifier.id in {"private_label", "foodservice", "mini", "assortiment"}:
            continue  # critères commerciaux : ne servent pas à retrouver une fiche produit
        terms.extend(modifier.terms[:4])
    terms.extend(r.lower() for r in plan.references)
    return list(dict.fromkeys(t for t in terms if t))[:limit]


def fts_match_query(terms: list[str] | tuple[str, ...]) -> str:
    """Requête FTS5 (OU de termes). Les expressions sont mises entre guillemets, les mots longs en préfixe."""
    parts: list[str] = []
    for term in terms:
        clean = normalize(term)
        if not clean:
            continue
        parts.append(f'"{clean}"' if " " in clean else (f'"{clean}"*' if len(clean) >= 4 else f'"{clean}"'))
    return " OR ".join(parts)
