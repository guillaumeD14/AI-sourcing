import base64
import io
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import fitz
import pandas as pd
import requests
import streamlit as st
from PIL import Image
from rapidfuzz import fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

st.set_page_config(page_title="AI Sourcing Delifrance", page_icon="🔎", layout="wide")

CATALOGUE_DIR = Path("catalogues")
HTTP_TIMEOUT = 30


def get_secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


OPENAI_API_KEY = ""  # Mode gratuit: OpenAI desactive
TAVILY_API_KEY = get_secret("TAVILY_API_KEY")
OPENAI_MODEL = ""


def key_is_configured(value, prefix=None):
    if not value or value.lower().startswith(("colle-", "votre-", "your-")):
        return False
    return not prefix or value.startswith(prefix)


def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Z0-9\s/.,'()+-]", " ", text.upper())
    return re.sub(r"\s+", " ", text).strip()


def detect_supplier(filename):
    stem = Path(filename).stem
    parts = [item for item in re.split(r"[\s_-]+", stem) if item]
    return parts[0].upper() if parts else "INCONNU"


def extract_weight(text):
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(KG|G)\b", normalize_text(text))
    if not match:
        return "Non communique"
    return f"{match.group(1).replace(',', '.')} {match.group(2).lower()}"


def extract_reference(text):
    normalized = normalize_text(text)
    explicit = re.search(r"\bREF(?:ERENCE)?\s*[:#-]?\s*([A-Z0-9-]{4,16})\b", normalized)
    if explicit:
        return explicit.group(1)
    numeric = re.search(r"\b(\d{6,10})\b", normalized)
    return numeric.group(1) if numeric else "Non trouvee"


def catalogue_signature():
    if not CATALOGUE_DIR.exists():
        return tuple()
    return tuple(
        (str(path.relative_to(CATALOGUE_DIR)).replace("\\", "/"), path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(CATALOGUE_DIR.rglob("*.pdf"))
    )


@st.cache_data(show_spinner=False, max_entries=1)
def build_library_index(signature):
    pages_index = []
    catalogue_stats = []

    for relative_name, _size, _modified in signature:
        path = CATALOGUE_DIR / relative_name
        supplier = detect_supplier(relative_name)
        page_count = 0
        indexed_count = 0

        try:
            document = fitz.open(path)
            page_count = document.page_count
            for page_index in range(document.page_count):
                text = re.sub(r"\s+", " ", document.load_page(page_index).get_text("text")).strip()
                if not text:
                    continue
                pages_index.append({
                    "Fournisseur": supplier,
                    "Catalogue": relative_name,
                    "Page": page_index + 1,
                    "Texte": text,
                    "Poids": extract_weight(text),
                    "Reference": extract_reference(text),
                })
                indexed_count += 1
            document.close()
        except Exception as exc:
            catalogue_stats.append({
                "Fournisseur": supplier,
                "Catalogue": relative_name,
                "Pages": page_count,
                "Pages indexees": indexed_count,
                "Statut": f"Erreur: {exc}",
            })
            continue

        catalogue_stats.append({
            "Fournisseur": supplier,
            "Catalogue": relative_name,
            "Pages": page_count,
            "Pages indexees": indexed_count,
            "Statut": "OK" if indexed_count else "OCR requis",
        })

    return pages_index, catalogue_stats


@st.cache_data(show_spinner=False, max_entries=4)
def load_pdf_page(catalogue_name, page_number):
    path = CATALOGUE_DIR / catalogue_name
    if not path.exists():
        return None
    document = fitz.open(path)
    page = document.load_page(page_number - 1)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.15, 1.15), alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
    document.close()
    return image


def extract_uploaded_pdf_text(uploaded_pdf, max_chars=12000):
    if not uploaded_pdf:
        return ""
    document = fitz.open(stream=uploaded_pdf.getvalue(), filetype="pdf")
    parts = []
    total = 0
    for page_index in range(document.page_count):
        text = document.load_page(page_index).get_text("text")
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    document.close()
    return "\n".join(parts)[:max_chars]


def image_data_url(uploaded_image):
    mime = uploaded_image.type or "image/jpeg"
    encoded = base64.b64encode(uploaded_image.getvalue()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def openai_chat(messages, response_format=None):
    if not key_is_configured(OPENAI_API_KEY):
        raise RuntimeError("OPENAI_API_KEY absente ou invalide dans les Secrets Streamlit.")

    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": 0.1}
    if response_format:
        payload["response_format"] = response_format

    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code == 401:
        raise RuntimeError("Cle OpenAI invalide ou non autorisee.")
    if response.status_code == 429:
        raise RuntimeError("Quota OpenAI insuffisant ou limite atteinte.")
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def fallback_terms(product_name, weight):
    base = normalize_text(product_name)
    terms = [product_name]
    groups = {
        "DANISH": ["mini Danish", "assorted Danish pastries", "Danish selection", "mini viennoiseries", "frozen laminated pastry"],
        "CROISSANT": ["frozen croissant", "laminated pastry", "industrial viennoiserie"],
        "BEIGNET": ["industrial donut", "filled doughnut", "frozen beignet"],
        "BAGUETTE": ["industrial frozen baguette", "part baked baguette", "frozen bakery"],
        "TARTE": ["industrial tart", "frozen tart", "private label pastry"],
    }
    for keyword, alternatives in groups.items():
        if keyword in base:
            terms.extend(alternatives)
    if weight:
        terms = [f"{term} {weight}" for term in terms]
    return list(dict.fromkeys(terms))[:12]


def understand_need(product_name, weight, criteria, reference_text, uploaded_image):
    profile = {
        "product_family": product_name,
        "key_features": criteria or "Non renseigne",
        "weight_range": weight or "Non renseigne",
        "search_terms": fallback_terms(product_name, weight),
        "visual_description": "",
    }
    if not key_is_configured(OPENAI_API_KEY):
        return profile

    content = [{
        "type": "text",
        "text": (
            "Tu assistes un acheteur negoce Delifrance. Analyse le produit sans inventer. "
            "La fiche technique est seulement un exemple semblable. Retourne uniquement un objet JSON avec: "
            "product_family, visual_description, key_features, weight_range, search_terms_fr, search_terms_en.\n"
            f"Produit: {product_name}\nGrammage: {weight}\nCriteres: {criteria}\n"
            f"Extrait fiche technique: {reference_text[:6000]}"
        ),
    }]
    if uploaded_image:
        content.append({"type": "image_url", "image_url": {"url": image_data_url(uploaded_image)}})

    try:
        raw = openai_chat([{"role": "user", "content": content}], {"type": "json_object"})
        parsed = json.loads(raw)
        terms = parsed.get("search_terms_fr", []) + parsed.get("search_terms_en", [])
        profile.update({
            "product_family": parsed.get("product_family", product_name),
            "visual_description": parsed.get("visual_description", ""),
            "key_features": parsed.get("key_features", criteria or "Non renseigne"),
            "weight_range": parsed.get("weight_range", weight or "Non renseigne"),
            "search_terms": list(dict.fromkeys(terms))[:16] or profile["search_terms"],
        })
    except Exception as exc:
        st.warning(f"Mode gratuit actif. Detail: {exc}")
    return profile


def search_internal(pages_index, query_text, limit):
    if not pages_index:
        return pd.DataFrame()
    documents = [normalize_text(row["Texte"]) for row in pages_index]
    query = normalize_text(query_text)
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, max_features=30000)
    matrix = vectorizer.fit_transform(documents + [query])
    semantic = cosine_similarity(matrix[-1], matrix[:-1]).flatten()

    results = []
    for position, item in enumerate(pages_index):
        fuzzy_score = fuzz.token_set_ratio(query, documents[position]) / 100
        score = round(100 * (0.72 * semantic[position] + 0.28 * fuzzy_score), 1)
        row = dict(item)
        row["Similarite texte"] = score
        row["Extrait"] = item["Texte"][:650]
        results.append(row)

    return pd.DataFrame(results).sort_values("Similarite texte", ascending=False).head(limit)


def tavily_search(query, max_results=6):
    if not key_is_configured(TAVILY_API_KEY, "tvly-"):
        raise RuntimeError("TAVILY_API_KEY absente ou invalide dans les Secrets Streamlit.")

    response = requests.post(
        "https://api.tavily.com/search",
        headers={
            "Authorization": f"Bearer {TAVILY_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        },
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code == 401:
        raise RuntimeError("Cle Tavily invalide ou non autorisee.")
    if response.status_code == 429:
        raise RuntimeError("Quota Tavily atteint.")
    response.raise_for_status()
    return response.json().get("results", [])


def qualify_web_results(product_name, profile, raw_results):
    excluded_terms = {
        "amazon", "walmart", "carrefour", "tesco", "restaurant", "bakery shop",
        "deliveroo", "ubereats", "marketplace", "retail", "recipe", "pinterest",
        "instagram", "facebook", "youtube", "wikipedia", "tripadvisor",
    }
    industrial_terms = {
        "manufacturer", "manufacturing", "producer", "factory", "industrial",
        "private label", "foodservice", "wholesale", "b2b", "frozen bakery",
        "production facility", "co-manufacturer", "supplier", "export",
    }
    product_terms = set(normalize_text(product_name).lower().split())
    qualified = []

    for result in raw_results:
        title = result.get("title", "")
        url = result.get("url", "")
        content = result.get("content", "")
        combined = f"{title} {content} {url}".lower()

        if not url or any(term in combined for term in excluded_terms):
            continue

        industrial_hits = sum(1 for term in industrial_terms if term in combined)
        product_hits = sum(1 for term in product_terms if len(term) > 2 and term in combined)
        score = industrial_hits * 18 + product_hits * 8

        if industrial_hits >= 2:
            status = "Fabricant probable"
        elif industrial_hits == 1:
            status = "Statut industriel a confirmer"
        else:
            status = "A verifier"

        if score < 10:
            continue

        domain = urlparse(url).netloc.replace("www.", "")
        qualified.append({
            "company": domain,
            "country": "A confirmer",
            "manufacturer_status": status,
            "similar_product": title,
            "product_url": url,
            "technical_sheet_url": url if url.lower().endswith(".pdf") else "",
            "contact_url": "",
            "private_label": "Oui probable" if "private label" in combined else "A confirmer",
            "evidence": content[:550] or title,
            "relevance": min(score, 100),
        })

    qualified.sort(key=lambda item: item["relevance"], reverse=True)

    seen_domains = set()
    deduplicated = []
    for item in qualified:
        domain = item["company"]
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        deduplicated.append(item)

    return deduplicated[:15]

def search_industrials(product_name, profile):
    terms = (profile.get("search_terms") or [product_name])[:4]
    queries = [f"{term} manufacturer private label industrial food product" for term in terms]

    deduplicated = {}
    errors = []
    for query in queries[:4]:
        try:
            for result in tavily_search(query):
                url = result.get("url", "")
                if url:
                    deduplicated[url] = result
        except Exception as exc:
            errors.append(str(exc))
            break

    if errors:
        raise RuntimeError(errors[0])
    return qualify_web_results(product_name, profile, list(deduplicated.values())[:35])


def show_link(url, label):
    if url:
        st.link_button(label, url, width="stretch")
    else:
        st.caption(f"{label}: non trouve publiquement")


DEFAULT_STATE = {
    "pages_index": [],
    "catalogue_stats": [],
    "loaded_signature": None,
    "internal_results": None,
    "need_profile": None,
    "need_text": "",
    "web_results": None,
}
for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value

signature = catalogue_signature()

with st.sidebar:
    st.header("📁 Base catalogues")
    st.metric("PDF disponibles", len(signature))

    if signature and st.session_state.loaded_signature != signature:
        with st.spinner("Indexation legere des catalogues..."):
            pages_index, catalogue_stats = build_library_index(signature)
            st.session_state.pages_index = pages_index
            st.session_state.catalogue_stats = catalogue_stats
            st.session_state.loaded_signature = signature

    if not signature:
        st.error("Aucun PDF trouve dans le dossier catalogues/.")
    else:
        st.success(f"{len(st.session_state.catalogue_stats)} catalogues charges")
        st.metric("Pages indexees", len(st.session_state.pages_index))

    if st.button("Reindexer", width="stretch"):
        st.cache_data.clear()
        st.session_state.loaded_signature = None
        st.session_state.internal_results = None
        st.session_state.web_results = None
        st.rerun()

    st.divider()
    st.subheader("Connexions")
    st.write("✅ Tavily gratuit configure" if key_is_configured(TAVILY_API_KEY, "tvly-") else "⚪ Tavily non configure")
    st.caption("Mode gratuit sans OpenAI")

st.title("🔎 AI Sourcing Delifrance")
st.caption("1. Recherche dans les catalogues · 2. Recherche de nouveaux industriels sur Internet")

need_tab, internal_tab, web_tab, library_tab = st.tabs([
    "🎯 Besoin produit", "📁 Resultats internes", "🌍 Industriels web", "📚 Bibliotheque"
])

with need_tab:
    st.header("Decrire le produit recherche")
    col1, col2 = st.columns([2, 1])
    with col1:
        product_name = st.text_input("Nom du produit", placeholder="Mini Danish assortiment")
        criteria = st.text_area("Criteres complementaires", placeholder="Surgele, assortiment, private label...")
    with col2:
        weight = st.text_input("Grammage cible", placeholder="40 g")
        result_limit = st.slider("Nombre de resultats", 5, 40, 15, 5)

    uploaded_image = st.file_uploader("Photo du produit", type=["jpg", "jpeg", "png", "webp"])
    uploaded_sheet = st.file_uploader("Fiche technique d'un produit semblable", type=["pdf"])
    if uploaded_image:
        st.image(uploaded_image, width=320, caption="Produit recherche")

    if st.button("1. Rechercher dans les catalogues", type="primary", width="stretch"):
        if not product_name:
            st.warning("Indique le nom du produit.")
        elif not st.session_state.pages_index:
            st.error("Aucune page catalogue indexee. Certains PDF peuvent necessiter un OCR.")
        else:
            with st.spinner("Analyse du besoin et recherche interne..."):
                reference_text = extract_uploaded_pdf_text(uploaded_sheet)
                profile = understand_need(product_name, weight, criteria, reference_text, uploaded_image)
                need_text = "\n".join(filter(None, [
                    product_name, weight, criteria, reference_text[:6500], profile.get("visual_description", ""),
                    " ".join(profile.get("search_terms", [])),
                ]))
                st.session_state.need_profile = profile
                st.session_state.need_text = need_text
                st.session_state.internal_results = search_internal(
                    st.session_state.pages_index, need_text, result_limit
                )
                st.session_state.web_results = None
            st.success("Recherche interne terminee. Ouvre l'onglet Resultats internes.")

with internal_tab:
    st.header("Resultats dans la base catalogues")
    results = st.session_state.internal_results
    if results is None:
        st.info("Lance d'abord la recherche depuis l'onglet Besoin produit.")
    elif results.empty:
        st.warning("Aucun resultat interne trouve.")
    else:
        st.dataframe(
            results[["Fournisseur", "Catalogue", "Page", "Poids", "Reference", "Similarite texte"]],
            width="stretch",
            hide_index=True,
        )
        for rank, (_, row) in enumerate(results.head(8).iterrows(), start=1):
            title = f"{rank}. {row['Fournisseur']} · page {row['Page']} · {row['Similarite texte']} %"
            with st.expander(title):
                left, right = st.columns([1, 1.25])
                with left:
                    st.write(f"**Catalogue:** {row['Catalogue']}")
                    st.write(f"**Poids detecte:** {row['Poids']}")
                    st.write(f"**Reference:** {row['Reference']}")
                    st.write(f"**Extrait:** {row['Extrait']}")
                with right:
                    page_image = load_pdf_page(row["Catalogue"], int(row["Page"]))
                    if page_image is not None:
                        st.image(page_image, width="stretch")

        st.divider()
        st.subheader("Etape 2: trouver de nouveaux industriels")
        if not key_is_configured(TAVILY_API_KEY, "tvly-"):
            st.warning("Configure TAVILY_API_KEY dans les Secrets Streamlit.")
        if st.button("2. Rechercher des industriels sur Internet", type="primary", width="stretch"):
            if not st.session_state.need_profile:
                st.error("Relance d'abord la recherche interne.")
            elif not key_is_configured(TAVILY_API_KEY, "tvly-"):
                st.error("Cle Tavily absente ou invalide.")
            else:
                try:
                    with st.spinner("Recherche et qualification des industriels..."):
                        product = st.session_state.need_profile.get("product_family", "Produit recherche")
                        st.session_state.web_results = search_industrials(product, st.session_state.need_profile)
                    st.success("Recherche web terminee. Ouvre l'onglet Industriels web.")
                except Exception as exc:
                    st.error(f"Recherche Internet impossible: {exc}")

with web_tab:
    st.header("Industriels potentiels a contacter")
    suppliers = st.session_state.web_results
    if suppliers is None:
        st.info("Effectue la recherche interne puis lance la recherche Internet.")
    elif not suppliers:
        st.warning("Aucun industriel suffisamment verifiable n'a ete retenu.")
    else:
        for rank, supplier in enumerate(suppliers, start=1):
            with st.container(border=True):
                st.subheader(f"{rank}. {supplier.get('company', 'Entreprise a confirmer')}")
                c1, c2, c3 = st.columns(3)
                c1.write(f"**Pays:** {supplier.get('country', 'A confirmer')}")
                c2.write(f"**Statut industriel:** {supplier.get('manufacturer_status', 'A confirmer')}")
                c3.write(f"**Private label:** {supplier.get('private_label', 'A confirmer')}")
                st.write(f"**Produit similaire:** {supplier.get('similar_product', 'A confirmer')}")
                st.write(f"**Pertinence:** {supplier.get('relevance', 'A verifier')}")
                st.write(f"**Preuve:** {supplier.get('evidence', 'A confirmer')}")
                b1, b2, b3 = st.columns(3)
                with b1:
                    show_link(supplier.get("product_url", ""), "Page produit")
                with b2:
                    show_link(supplier.get("technical_sheet_url", ""), "Fiche technique")
                with b3:
                    show_link(supplier.get("contact_url", ""), "Contact")

        st.download_button(
            "Exporter la shortlist CSV",
            pd.DataFrame(suppliers).to_csv(index=False).encode("utf-8-sig"),
            "shortlist_industriels.csv",
            "text/csv",
            width="stretch",
        )

with library_tab:
    st.header("Etat de la bibliotheque")
    if st.session_state.catalogue_stats:
        st.dataframe(pd.DataFrame(st.session_state.catalogue_stats), width="stretch", hide_index=True)
    else:
        st.info("Aucun catalogue charge.")
    st.caption("Les catalogues marques OCR requis sont des PDF sans texte exploitable.")

st.divider()
st.caption(
    "Assistant de sourcing pour l'acheteur negoce Delifrance. "
    "Verifier les preuves, capacites, certifications, MOQ et conditions avant tout contact fournisseur."
)
