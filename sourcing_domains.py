"""Domaines : filtrage du bruit (marketplaces, retail, recettes, réseaux sociaux, annuaires, presse),
pays déduit du domaine, détection de page d'accueil. Pur et testable."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_SECOND_LEVEL = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.tr", "com.pl", "com.es", "com.pt", "co.at", "or.at", "com.br", "com.au",
    "co.nz", "co.za", "co.jp", "com.cn", "com.hk", "com.sg", "co.in", "com.mx", "com.ua", "com.ar",
})

NOISE_DOMAINS = frozenset("""
amazon.com ebay.com aliexpress.com alibaba.com temu.com etsy.com cdiscount.com rakuten.com fnac.com leboncoin.fr wish.com
walmart.com carrefour.fr auchan.fr e.leclerc leclerc.com intermarche.com lidl.fr lidl.de lidl.com aldi.com aldi.fr aldi.de
monoprix.fr franprix.fr picard.fr thiriet.com houra.fr ocado.com tesco.com sainsburys.co.uk asda.com edeka.de rewe.de
kaufland.de mercadona.es dia.es migros.ch coop.ch
marmiton.org cuisineaz.com 750g.com ricardocuisine.com allrecipes.com tasty.co bbcgoodfood.com epicurious.com chefkoch.de
giallozafferano.it cucchiaio.it recetasgratis.net directoalpaladar.com cuisine-actuelle.fr journaldesfemmes.fr doctissimo.fr
ptitchef.com lacuisinedegeraldine.fr recettes.de
facebook.com instagram.com linkedin.com twitter.com x.com tiktok.com pinterest.com pinterest.fr youtube.com youtu.be
reddit.com quora.com tumblr.com medium.com snapchat.com
wikipedia.org wikimedia.org wikidata.org britannica.com
europages.com europages.fr kompass.com pagesjaunes.fr yellowpages.com societe.com pappers.fr infogreffe.fr verif.com manageo.fr
lusha.com zoominfo.com dnb.com crunchbase.com glassdoor.com glassdoor.fr indeed.com indeed.fr trustpilot.com tripadvisor.com
tripadvisor.fr yelp.com made-in-china.com indiamart.com globalsources.com tradekey.com ec21.com exportersindia.com thomasnet.com
scribd.com slideshare.net issuu.com yumpu.com dokumen.pub pdfcoffee.com calameo.com
usinenouvelle.com lsa-conso.fr lejournaldesentreprises.com just-food.com foodnavigator.com foodnavigator-usa.com agra.fr
reussir.fr bakeryandsnacks.com bakeryinfo.com bakingbusiness.com bakeryandsnacks.com lesechos.fr lemonde.fr ouest-france.fr
lemondedudroit.fr hellowork.com welcometothejungle.com apec.fr pole-emploi.fr francetravail.fr
metro.fr metro.de transgourmet.fr pomona.fr sysco.com brakes.co.uk bidfood.co.uk hanos.nl makro.es davigel.fr promocash.com
mathon.fr matfer.com bragard.fr ikea.com leroymerlin.fr
""".split())
_NOISE_PATTERN = re.compile(r"(^|\.)(amazon|ebay|alibaba|aliexpress)\.[a-z.]+$")
_NOISE_PATH = re.compile(
    r"/(recettes?|recipes?|rezepte?|ricette?|recetas?|panier|cart|checkout|warenkorb|basket|boutique|shop|webshop|store)(/|$|\?)", re.I)
_RETAIL_SNIPPET = re.compile(r"\b(add to cart|ajouter au panier|in den warenkorb|buy now|acheter maintenant|livraison offerte|free delivery)\b", re.I)

EU_CCTLD = {
    "fr": "FR", "de": "DE", "it": "IT", "es": "ES", "nl": "NL", "be": "BE", "pl": "PL", "dk": "DK", "se": "SE", "at": "AT",
    "ie": "IE", "pt": "PT", "gr": "GR", "hu": "HU", "cz": "CZ", "ro": "RO", "hr": "HR", "si": "SI", "sk": "SK", "fi": "FI",
    "lt": "LT", "lv": "LV", "ee": "EE", "bg": "BG", "lu": "LU", "mt": "MT", "cy": "CY",
}
EUROPE_OTHER_CCTLD = {"uk": "GB", "ch": "CH", "no": "NO", "is": "IS", "li": "LI"}
EUROPE_EXTENDED_CCTLD = {"tr": "TR", "rs": "RS", "ua": "UA", "ba": "BA", "mk": "MK", "al": "AL", "me": "ME", "md": "MD"}
NON_EUROPE_CCTLD = frozenset("""us ca cn in au jp br mx kr tw hk sg nz za ar cl co pe ae sa il th vn id my ph pk bd eg ng ke ru by ir iq""".split())
EUROPE_CORE = frozenset(EU_CCTLD.values()) | frozenset(EUROPE_OTHER_CCTLD.values())
EUROPE_EXTENDED = EUROPE_CORE | frozenset(EUROPE_EXTENDED_CCTLD.values())


def host_of(url: str) -> str:
    host = (urlparse(url if "//" in url else f"//{url}").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def registered_domain(url_or_host: str) -> str:
    """``https://shop.example.co.uk/x`` -> ``example.co.uk`` (approximation sans liste des suffixes publics)."""
    host = host_of(url_or_host)
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def tld_of(domain: str) -> str:
    return domain.rsplit(".", 1)[-1] if "." in domain else ""


def exclusion_reason(url: str, title: str = "", snippet: str = "") -> str | None:
    """Motif d'exclusion lisible (marketplace, recette, réseau social…) ou ``None`` si la source est acceptable."""
    host = host_of(url)
    domain = registered_domain(host)
    if domain in NOISE_DOMAINS or host in NOISE_DOMAINS or _NOISE_PATTERN.search(host):
        return "site écarté (marketplace, retail, recettes, réseau social, annuaire ou presse)"
    if any(host.endswith("." + d) for d in NOISE_DOMAINS):
        return "site écarté (marketplace, retail, recettes, réseau social, annuaire ou presse)"
    path = urlparse(url).path or ""
    if _NOISE_PATH.search(path + "/"):
        return "page recette ou boutique"
    if _RETAIL_SNIPPET.search(f"{title} {snippet}"):
        return "page de vente au détail"
    return None


def country_from_domain(domain: str) -> str | None:
    tld = tld_of(domain)
    return EU_CCTLD.get(tld) or EUROPE_OTHER_CCTLD.get(tld) or EUROPE_EXTENDED_CCTLD.get(tld)


def is_non_european_domain(domain: str) -> bool:
    return tld_of(domain) in NON_EUROPE_CCTLD


def in_europe(country: str | None, *, extended: bool = False) -> bool | None:
    """Vrai/faux si le pays est connu, ``None`` sinon (à confirmer)."""
    if not country:
        return None
    return country in (EUROPE_EXTENDED if extended else EUROPE_CORE)


def is_homepage(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "/").strip("/")
    if parsed.query:
        return False
    return path == "" or re.fullmatch(r"(?:[a-z]{2}(?:[-_][a-z]{2})?|index(?:\.[a-z]+)?|home|accueil)", path, re.I) is not None


def exclude_list_for_tavily(limit: int = 120) -> list[str]:
    """Domaines transmis à Tavily (``exclude_domains``, 150 maximum) : les plus bruyants d'abord."""
    priority = ["amazon.com", "ebay.com", "alibaba.com", "aliexpress.com", "temu.com", "marmiton.org", "cuisineaz.com", "750g.com",
                "allrecipes.com", "chefkoch.de", "giallozafferano.it", "facebook.com", "instagram.com", "linkedin.com", "pinterest.com",
                "youtube.com", "tiktok.com", "reddit.com", "wikipedia.org", "europages.com", "kompass.com", "pagesjaunes.fr",
                "societe.com", "indiamart.com", "made-in-china.com", "carrefour.fr", "auchan.fr", "leclerc.com", "lidl.fr",
                "picard.fr", "cdiscount.com", "scribd.com", "yumpu.com", "issuu.com", "glassdoor.com", "indeed.com"]
    rest = sorted(d for d in NOISE_DOMAINS if d not in priority)
    return (priority + rest)[:limit]
