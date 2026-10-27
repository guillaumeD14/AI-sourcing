"""Habillage visuel (direction artistique Délifrance). Uniquement de la forme : aucune logique métier ici.

Règles du guide appliquées :
- palette fermée : anthracite, blanc, crème, gris perle, brun cacao (+ survol), caramel décoratif ; aucune autre teinte ;
- le brun cacao est réservé aux actions (boutons, liens, contrôles) ; le caramel n'est jamais utilisé pour du texte ;
- deux familles de polices : Playfair Display (titres) et Montserrat (tout le reste), avec Georgia / Arial en secours ;
- ni dégradé, ni ombre portée, un seul arrondi (2 px), focus clavier visible ;
- le logo officiel n'est jamais redessiné ni retapé : il est affiché seulement si le fichier est présent.

Ce module n'importe pas Streamlit : il fournit du texte (CSS, HTML) que ``app.py`` injecte.
"""

from __future__ import annotations

import base64
import io
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent

INK = "#333333"          # principale : logo, titres, texte courant
WHITE = "#FFFFFF"        # fond de page et de cartes
CREAM = "#F1ECE0"        # secondaire : fonds de sections
PEARL = "#E6E5E4"        # neutre : filets, bordures
COCOA = "#745A4A"        # accent : boutons, liens
COCOA_HOVER = "#4F2D23"  # accent au survol
CARAMEL = "#A78160"      # accent doux : filets décoratifs, jamais du texte
ALLOWED_COLORS = frozenset({INK, WHITE, CREAM, PEARL, COCOA, COCOA_HOVER, CARAMEL})

TITLE_FONT = "'Playfair Display', Georgia, serif"
TEXT_FONT = "'Montserrat', Arial, sans-serif"
FONT_IMPORT = ("https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600"
               "&family=Playfair+Display:ital,wght@0,600;1,600&display=swap")
LOGO_NAMES = ("logo_delifrance.svg", "logo_delifrance.png")

# Statut -> (classe CSS, libellé). Les libellés sont ceux de ``sourcing_models`` ; la couleur ne porte jamais seule l'information.
_BADGES = {
    "fabricant confirmé": "dl-badge--confirmed",
    "fabricant probable": "dl-badge--probable",
    "à confirmer": "dl-badge--toconfirm",
    "non trouvé": "dl-badge--notfound",
}


def find_logo(root: Path | None = None) -> Path | None:
    """Logo officiel déposé à la racine (``logo_delifrance.svg`` ou ``.png``), sinon ``None``."""
    base = root or ROOT
    for name in LOGO_NAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


LOGO_ALT = "Délifrance, Live Everyday Delicious"
LOGO_DISPLAY_WIDTH = 320  # px : en dessous, la signature du logo devient illisible


def logo_html(path: Path, display_width: int = LOGO_DISPLAY_WIDTH) -> str:
    """Balise ``<img>`` du logo officiel, en données intégrées (aucun fichier à servir).

    Le PNG est seulement réduit à 2x la largeur d'affichage (écrans Retina) en conservant ses proportions ; le SVG est
    repris tel quel. Le logo n'est ni recoloré, ni recadré, ni déformé : la hauteur est toujours calculée par le navigateur.
    """
    if path.suffix.lower() == ".svg":
        mime, data = "image/svg+xml", path.read_bytes()
    else:
        from PIL import Image  # déjà requis par l'application

        with Image.open(path) as image:
            image = image.convert("RGBA")
            target = min(image.width, display_width * 2)
            if target < image.width:
                image = image.resize((target, round(image.height * target / image.width)), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
        mime, data = "image/png", buffer.getvalue()
    source = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return f'<img class="dl-logo" src="{source}" alt="{escape(LOGO_ALT)}" width="{display_width}">'


def status_badge(status: str) -> str:
    """Pastille HTML d'un statut fournisseur (texte échappé ; classe inconnue = neutre)."""
    css = _BADGES.get(status, "dl-badge--notfound")
    return f'<span class="dl-badge {css}">{escape(status)}</span>'


def meta_line(*parts: str) -> str:
    """Ligne d'informations secondaires : chaque morceau est échappé (les données viennent d'Internet)."""
    return '<span class="dl-meta">' + " · ".join(escape(p) for p in parts if p) + "</span>"


def theme_css() -> str:
    """Feuille de style complète. Aucun sélecteur ne vise les icônes de Streamlit (leurs polices restent intactes)."""
    return f"""
@import url('{FONT_IMPORT}');
:root {{ --dl-ink:{INK}; --dl-white:{WHITE}; --dl-cream:{CREAM}; --dl-pearl:{PEARL};
        --dl-cocoa:{COCOA}; --dl-cocoa-hover:{COCOA_HOVER}; --dl-caramel:{CARAMEL}; }}

/* Typographie : Montserrat pour le texte, Playfair Display pour les titres */
.stApp, .stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea,
[data-testid="stMarkdownContainer"], [data-testid="stCaptionContainer"] {{ font-family:{TEXT_FONT}; color:var(--dl-ink); }}
.stApp p, .stApp li {{ font-size:16px; line-height:1.5; }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ font-size:14px; line-height:1.45; color:var(--dl-ink); }}
.stApp h1, .stApp h2, .stApp h3, .stApp h4 {{ font-family:{TITLE_FONT}; font-weight:600; color:var(--dl-ink); letter-spacing:0; }}
.stApp h1 {{ font-size:2.4rem; line-height:1.15; }}
.stApp h2 {{ font-size:1.7rem; line-height:1.2; }}
.stApp h3 {{ font-size:1.3rem; line-height:1.25; }}

/* Mise en page aérée, fonds blancs et crème */
.stApp {{ background:var(--dl-white); }}
.block-container {{ padding-top:2.5rem; padding-bottom:3rem; max-width:1280px; }}
[data-testid="stSidebar"] {{ background:var(--dl-cream); border-right:1px solid var(--dl-pearl); }}
footer {{ visibility:hidden; }}

/* Navigation par onglets : capitales espacées, soulignement anthracite */
.stTabs [data-baseweb="tab-list"] {{ gap:1.75rem; border-bottom:1px solid var(--dl-pearl); }}
.stTabs [data-baseweb="tab"], .stTabs [data-baseweb="tab"] p {{ font-family:{TEXT_FONT}; font-weight:600; font-size:.78rem;
    letter-spacing:.12em; text-transform:uppercase; color:var(--dl-ink); }}
.stTabs [data-baseweb="tab-highlight"] {{ background-color:var(--dl-ink); }}

/* Boutons : cacao pour l'action principale, contour anthracite pour l'action secondaire */
.stApp .stButton > button, .stApp .stDownloadButton > button,
.stApp [data-testid="stBaseButton-primary"], .stApp [data-testid="stBaseButton-secondary"] {{
    font-family:{TEXT_FONT}; font-weight:600; font-size:.78rem; letter-spacing:.08em; text-transform:uppercase;
    border-radius:2px; box-shadow:none; transition:none; }}
.stApp button p {{ color:inherit; font-size:inherit; font-weight:inherit; letter-spacing:inherit; text-transform:inherit; line-height:1.25; }}
.stApp button[kind="primary"], .stApp [data-testid="stBaseButton-primary"] {{
    background:var(--dl-cocoa); border:1px solid var(--dl-cocoa); color:var(--dl-white); }}
.stApp button[kind="primary"]:hover, .stApp [data-testid="stBaseButton-primary"]:hover {{
    background:var(--dl-cocoa-hover); border-color:var(--dl-cocoa-hover); color:var(--dl-white); }}
.stApp button[kind="secondary"], .stApp [data-testid="stBaseButton-secondary"] {{
    background:var(--dl-white); border:1px solid var(--dl-ink); color:var(--dl-ink); }}
.stApp button[kind="secondary"]:hover, .stApp [data-testid="stBaseButton-secondary"]:hover {{
    background:var(--dl-cream); border-color:var(--dl-ink); color:var(--dl-ink); }}
.stApp button:focus-visible {{ outline:2px solid var(--dl-ink); outline-offset:2px; }}

/* Liens : accent cacao */
.stApp a {{ color:var(--dl-cocoa); text-decoration:underline; }}
.stApp a:hover {{ color:var(--dl-cocoa-hover); }}

/* Indicateurs, panneaux dépliables, champs : mêmes filets gris perle et même arrondi */
[data-testid="stMetricLabel"] p {{ font-weight:600; font-size:.7rem; letter-spacing:.12em; text-transform:uppercase; }}
[data-testid="stMetricValue"] {{ font-family:{TITLE_FONT}; font-weight:600; color:var(--dl-ink); }}
[data-testid="stExpander"] details {{ border:1px solid var(--dl-pearl); border-radius:2px; }}
.stApp [data-baseweb="input"], .stApp [data-baseweb="textarea"], .stApp [data-baseweb="select"] > div {{ border-radius:2px; }}

/* Logo : proportions libres (hauteur automatique), zone de protection d'au moins une hauteur de lettre autour, jamais d'effet */
.dl-logo {{ display:block; width:{LOGO_DISPLAY_WIDTH}px; max-width:100%; height:auto; margin:.5rem 0 1.75rem 0; }}

/* Pastilles de statut : uniquement la palette ; le libellé porte le sens, pas la couleur seule */
.dl-badge {{ display:inline-block; padding:.15rem .6rem; border-radius:2px; font-family:{TEXT_FONT}; font-weight:600;
    font-size:.7rem; letter-spacing:.12em; text-transform:uppercase; border:1px solid var(--dl-ink); line-height:1.4; }}
.dl-badge--confirmed {{ background:var(--dl-ink); color:var(--dl-white); }}
.dl-badge--probable {{ background:var(--dl-cream); color:var(--dl-ink); }}
.dl-badge--toconfirm {{ background:var(--dl-white); color:var(--dl-ink); border:1px dashed var(--dl-caramel); }}
.dl-badge--notfound {{ background:var(--dl-pearl); color:var(--dl-ink); border-color:var(--dl-pearl); }}
.dl-meta {{ font-family:{TEXT_FONT}; font-size:14px; color:var(--dl-ink); margin-left:.5rem; }}
"""
