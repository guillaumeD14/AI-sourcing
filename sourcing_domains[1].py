"""Domaines et pays : exclusion du bruit (marketplaces, recettes, réseaux sociaux...), pays par TLD."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_MULTI_SUFFIXES = frozenset(
    {"co.uk", "org.uk", "com.tr", "com.pl", "com.es", "com.pt", "com.br", "co.il", "com.au", "co.za", "com.ua"}
)

_AMAZON_TLDS = ("com", "fr", "de", "es", "it", "nl", "co.uk", "se", "pl", "be")

_NOISE_BASE: tuple[str, ...] = (
    # marketplaces / e-commerce
    "ebay.com", "ebay.fr", "ebay.de", "aliexpress.com", "alibaba.com", "temu.com", "etsy.com", "cdiscount.com",
    "fnac.com", "leboncoin.fr", "vinted.fr", "rakuten.com", "walmart.com", "wish.com", "shopify.com",
    "indiamart.com", "made-in-china.com", "tradekey.com", "ecplaza.net", "globalsources.com", "dhgate.com",
    # grande distribution et grossistes / distributeurs
    "carrefour.fr", "auchan.fr", "leclerc.com", "e.leclerc", "intermarche.com", "lidl.fr", "lidl.com", "aldi.fr",
    "monoprix.fr", "casino.fr", "franprix.fr", "picard.fr", "metro.fr", "metro.de", "promocash.com", "transgourmet.fr",
    "sysco.com", "brake.co.uk", "tesco.com", "ocado.com", "mercadona.es", "drive.leclerc",
    # recettes / contenus grand public
    "marmiton.org", "750g.com", "cuisineaz.com", "ricardocuisine.com", "allrecipes.com", "tasty.co", "jow.fr",
    "cuisine-actuelle.fr", "chefkoch.de", "giallozafferano.it", "recetasgratis.net", "bbcgoodfood.com", "epicurious.com",
    "pinterest.com", "pinterest.fr", "tasteofhome.com", "delish.com", "foodnetwork.com", "yummly.com",
    # réseaux sociaux, vidéo, forums, encyclopédies
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "tiktok.com",
    "reddit.com", "quora.com", "wikipedia.org", "wikimedia.org", "medium.com",
    # restaurants / avis / annuaires faibles / emploi
    "tripadvisor.com", "tripadvisor.fr", "yelp.com", "thefork.com", "ubereats.com", "deliveroo.fr", "just-eat.fr",
    "pagesjaunes.fr", "kompass.com", "europages.com", "europages.fr", "yellowpages.com", "glassdoor.com",
    "indeed.com", "openfoodfacts.org", "trustpilot.com", "societe.com", "pappers.fr",
)

EXCLUDE_DOMAINS: tuple[str, ...] = tuple(
    dict.fromkeys([*_NOISE_BASE, *(f"amazon.{tld}" for tld in _AMAZON_TLDS)])
)

_NOISE_PATTERN = re.compile(
    r"(^|\.)(amazon|ebay|aliexpress|alibaba|marmiton|pinterest|facebook|instagram|youtube|tiktok|tripadvisor|"
    r"wikipedia|linkedin|etsy|cdiscount)\.",
    re.IGNORECASE,
)

EUROPE_COUNTRIES: frozenset[str] = frozenset(
    {
        "France", "Belgique", "Espagne", "Italie", "Allemagne", "Pays-Bas", "Danemark", "Suède", "Pologne",
        "Portugal", "Royaume-Uni", "Suisse", "Autriche", "Irlande", "Finlande", "Norvège", "Roumanie", "Hongrie",
        "Tchéquie", "Grèce", "Luxembourg", "Bulgarie", "Slovaquie", "Slovénie", "Croatie", "Lituanie", "Lettonie",
        "Estonie", "Turquie",
    }
)

_TLD_COUNTRY: dict[str, str] = {
    "fr": "France", "be": "Belgique", "es": "Espagne", "it": "Italie", "de": "Allemagne", "nl": "Pays-Bas",
    "dk": "Danemark", "se": "Suède", "pl": "Pologne", "pt": "Portugal", "uk": "Royaume-Uni", "ch": "Suisse",
    "at": "Autriche", "ie": "Irlande", "fi": "Finlande", "no": "Norvège", "ro": "Roumanie", "hu": "Hongrie",
    "cz": "Tchéquie", "gr": "Grèce", "lu": "Luxembourg", "bg": "Bulgarie", "sk": "Slovaquie", "si": "Slovénie",
    "hr": "Croatie", "lt": "Lituanie", "lv": "Lettonie", "ee": "Estonie", "tr": "Turquie",
}

_NON_EUROPE_TLD = frozenset({"cn", "in", "us", "br", "jp", "kr", "vn", "th", "id", "mx", "ca", "au", "pk", "bd", "ru"})

_COUNTRY_WORDS: dict[str, tuple[str, ...]] = {
    "France": ("france", "français", "francais", "french"),
    "Belgique": ("belgique", "belgium", "belgië", "belgie", "belgien"),
    "Espagne": ("espagne", "spain", "españa", "espana"),
    "Italie": ("italie", "italy", "italia"),
    "Allemagne": ("allemagne", "germany", "deutschland"),
    "Pays-Bas": ("pays-bas", "netherlands", "nederland", "holland"),
    "Danemark": ("danemark", "denmark", "danmark"),
    "Suède": ("suède", "suede", "sweden", "sverige"),
    "Pologne": ("pologne", "poland", "polska"),
    "Portugal": ("portugal",),
    "Royaume-Uni": ("royaume-uni", "united kingdom"),
    "Suisse": ("suisse", "switzerland", "schweiz"),
    "Autriche": ("autriche", "austria", "österreich", "osterreich"),
    "Irlande": ("irlande", "ireland"),
    "Finlande": ("finlande", "finland", "suomi"),
    "Norvège": ("norvège", "norvege", "norway", "norge"),
    "Luxembourg": ("luxembourg", "luxemburg"),
}


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def registered_domain(url_or_host: str) -> str:
    host = host_of(url_or_host) if "/" in url_or_host or "://" in url_or_host else url_or_host.lower().removeprefix("www.")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    tail2 = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if tail2 in _MULTI_SUFFIXES else tail2


def is_noise_domain(url_or_host: str) -> bool:
    host = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower()
    reg = registered_domain(host)
    return reg in EXCLUDE_DOMAINS or bool(_NOISE_PATTERN.search(host + "."))


def is_homepage(url: str) -> bool:
    """Racine du site, éventuellement précédée d'un code langue (/fr, /en-gb) ou suivie de index.html."""
    segments = [seg for seg in urlparse(url).path.split("/") if seg]
    if segments and re.fullmatch(r"[a-z]{2}(?:[-_][a-z]{2})?", segments[0], re.IGNORECASE):
        segments = segments[1:]
    return not segments or (len(segments) == 1 and segments[0].lower() in {"index.html", "index.htm", "index.php", "home"})


def country_from_domain(url_or_host: str) -> str | None:
    tld = registered_domain(url_or_host).rsplit(".", 1)[-1]
    return _TLD_COUNTRY.get(tld)


def is_non_european_tld(url_or_host: str) -> bool:
    return registered_domain(url_or_host).rsplit(".", 1)[-1] in _NON_EUROPE_TLD


def country_from_text(text: str) -> str | None:
    """Pays le plus cité dans ``text`` (au moins 2 mentions), sinon None."""
    lowered = text.lower()
    counts = {
        country: sum(len(re.findall(rf"\b{re.escape(word)}\b", lowered)) for word in words)
        for country, words in _COUNTRY_WORDS.items()
    }
    country, count = max(counts.items(), key=lambda kv: kv[1])
    return country if count >= 2 else None


def company_from_domain(domain: str) -> str:
    label = registered_domain(domain).split(".")[0]
    return re.sub(r"[-_]+", " ", label).title()


# --- pays cités dans la demande de l'acheteur (« pizza surgelée Italie 350 g ») ---------------------

_REQUEST_COUNTRIES: dict[str, tuple[str, ...]] = {
    "France": ("france",), "Belgique": ("belgique", "belgium", "belgie"), "Espagne": ("espagne", "spain", "espana"),
    "Italie": ("italie", "italy", "italia"), "Allemagne": ("allemagne", "germany", "deutschland"),
    "Pays-Bas": ("pays bas", "netherlands", "nederland", "hollande", "holland"),
    "Danemark": ("danemark", "denmark", "danmark"), "Suède": ("suede", "sweden", "sverige"),
    "Pologne": ("pologne", "poland", "polska"), "Portugal": ("portugal",),
    "Royaume-Uni": ("royaume uni", "united kingdom", "angleterre", "england"),
    "Suisse": ("suisse", "switzerland", "schweiz"), "Autriche": ("autriche", "austria", "osterreich"),
    "Irlande": ("irlande", "ireland"), "Finlande": ("finlande", "finland"), "Norvège": ("norvege", "norway"),
    "Luxembourg": ("luxembourg",),
}
_COUNTRY_PREPOSITIONS = frozenset({"en", "au", "aux", "in", "from", "de", "du", "d", "origine", "made"})
_AMBIGUOUS_COUNTRY_WORDS = frozenset({"suisse"})  # « pain suisse » est une viennoiserie, pas un pays

COUNTRY_EN: dict[str, str] = {
    "France": "France", "Belgique": "Belgium", "Espagne": "Spain", "Italie": "Italy", "Allemagne": "Germany",
    "Pays-Bas": "Netherlands", "Danemark": "Denmark", "Suède": "Sweden", "Pologne": "Poland", "Portugal": "Portugal",
    "Royaume-Uni": "United Kingdom", "Suisse": "Switzerland", "Autriche": "Austria", "Irlande": "Ireland",
    "Finlande": "Finland", "Norvège": "Norway", "Luxembourg": "Luxembourg",
}
COUNTRY_LANG: dict[str, str] = {
    "France": "fr", "Belgique": "fr", "Espagne": "es", "Italie": "it", "Allemagne": "de", "Pays-Bas": "nl",
    "Danemark": "da", "Suède": "sv", "Pologne": "pl", "Portugal": "pt", "Royaume-Uni": "en", "Suisse": "de",
    "Autriche": "de", "Irlande": "en", "Finlande": "en", "Norvège": "en", "Luxembourg": "fr",
}
MANUFACTURER_WORD: dict[str, str] = {
    "en": "manufacturer", "fr": "fabricant industriel", "de": "Hersteller", "es": "fabricante", "it": "produttore",
    "nl": "fabrikant", "pl": "producent", "sv": "tillverkare", "da": "producent", "pt": "fabricante",
}


def countries_in_request(norm_text: str) -> tuple[list[str], str]:
    """Pays cités dans un texte normalisé et texte sans ces mots. « suisse » exige une préposition."""
    tokens = norm_text.split()
    found: list[str] = []
    drop: set[int] = set()
    for country, names in _REQUEST_COUNTRIES.items():
        for name in names:
            words = name.split()
            for start in range(len(tokens) - len(words) + 1):
                if tokens[start : start + len(words)] != words:
                    continue
                if name in _AMBIGUOUS_COUNTRY_WORDS and not (start > 0 and tokens[start - 1] in _COUNTRY_PREPOSITIONS):
                    continue
                if country not in found:
                    found.append(country)
                drop.update(range(start, start + len(words)))
    return found, " ".join(t for i, t in enumerate(tokens) if i not in drop)


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower().encode("ascii", "ignore").decode() or text.lower())


def company_from_titles(domain: str, titles: list[str]) -> str:
    """Nom d'entreprise d'après les titres de pages (« Produit | NomEntreprise ») rapprochés du domaine."""
    label = _compact(registered_domain(domain).split(".")[0])
    best: str | None = None
    for title in titles:
        for segment in re.split(r"\s[|–—·•:-]\s|\s[|–—·•]\s", title):
            segment = segment.strip()
            compact = _compact(segment)
            if len(label) >= 4 and 3 <= len(segment) <= 40 and (label in compact or (len(compact) >= 4 and compact in label)):
                if best is None or len(segment) < len(best):
                    best = segment
    return best or company_from_domain(domain)
