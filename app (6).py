import io
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import fitz
import numpy as np
import pandas as pd
import requests
import streamlit as st
from PIL import Image
from rapidfuzz import fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

st.set_page_config(page_title="AI Sourcing Délifrance", page_icon="🔎", layout="wide")
CATALOGUE_DIR = Path("catalogues")
TIMEOUT = 20

# --------------------------
# Configuration sécurisée
# --------------------------
def get_secret(name):
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""

OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
TAVILY_API_KEY = get_secret("TAVILY_API_KEY")
OPENAI_MODEL = get_secret("OPENAI_MODEL") or "gpt-4.1-mini"

# --------------------------
# Utilitaires
# --------------------------
def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Z0-9\s/.,'()+-]", " ", text.upper())
    return re.sub(r"\s+", " ", text).strip()


def safe_filename(path):
    return str(path).replace("\\", "/")


def detect_supplier(filename):
    stem = Path(filename).stem
    parts = [p for p in re.split(r"[\s_-]+", stem) if p]
    return parts[0].upper() if parts else "INCONNU"


def extract_weight(text):
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(KG|G)\b", normalize_text(text))
    if not match:
        return "Non communiqué"
    return f"{match.group(1).replace(',', '.')} {match.group(2).lower()}"


def extract_reference(text):
    match = re.search(r"\b(?:REF(?:ERENCE)?\s*[:#-]?\s*)?([A-Z0-9-]{5,14})\b", normalize_text(text))
    return match.group(1) if match else "Non trouvée"


def extract_pdf_text(pdf_bytes):
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for index in range(document.page_count):
        page = document.load_page(index)
        pages.append({"page": index + 1, "text": page.get_text("text")})
    document.close()
    return pages


def render_pdf_page(pdf_bytes, page_number, zoom=1.15):
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = document.load_page(page_number - 1)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
    document.close()
    return image


def extract_reference_sheet_text(uploaded_pdf):
    if not uploaded_pdf:
        return ""
    pages = extract_pdf_text(uploaded_pdf.getvalue())
    return "\n".join(p["text"] for p in pages)[:15000]


def catalogue_signature():
    if not CATALOGUE_DIR.exists():
        return tuple()
    return tuple(
        (safe_filename(path.relative_to(CATALOGUE_DIR)), path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(CATALOGUE_DIR.rglob("*.pdf"))
    )


@st.cache_data(show_spinner=False)
def load_catalogue_library(signature):
    pages_index = []
    files = {}
    catalogue_stats = []
    for relative_name, _size, _modified in signature:
        path = CATALOGUE_DIR / relative_name
        raw = path.read_bytes()
        files[relative_name] = raw
        supplier = detect_supplier(relative_name)
        pages = extract_pdf_text(raw)
        catalogue_stats.append({
            "Fournisseur": supplier,
            "Catalogue": relative_name,
            "Pages": len(pages),
        })
        for item in pages:
            text = re.sub(r"\s+", " ", item["text"]).strip()
            if text:
                pages_index.append({
                    "Fournisseur": supplier,
                    "Catalogue": relative_name,
                    "Page": item["page"],
                    "Texte": text,
                    "Poids": extract_weight(text),
                    "Référence": extract_reference(text),
                })
    return pages_index, files, catalogue_stats


def build_need_text(product_name, weight, criteria, reference_text, image_description):
    parts = [product_name, weight, criteria, reference_text[:7000], image_description]
    return "\n".join(part for part in parts if part).strip()


def fallback_query_expansion(product_name, weight):
    base = normalize_text(product_name)
    terms = [product_name]
    groups = {
        "DANISH": ["mini Danish", "assorted Danish pastries", "Danish selection", "mini viennoiseries", "frozen laminated pastry"],
        "CROISSANT": ["frozen croissant", "laminated pastry", "viennoiserie industrielle"],
        "BEIGNET": ["industrial donut", "filled doughnut", "frozen beignet"],
        "BAGUETTE": ["industrial frozen baguette", "part baked baguette", "frozen bakery"],
        "TARTE": ["industrial tart", "frozen tart", "private label pastry"],
    }
    for keyword, alternatives in groups.items():
        if keyword in base:
            terms.extend(alternatives)
    if weight:
        terms = [f"{term} {weight}" for term in terms]
    return list(dict.fromkeys(terms))[:10]


def openai_chat(messages, response_format=None):
    if not OPENAI_API_KEY:
        return None
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": 0.1}
    if response_format:
        payload["response_format"] = response_format
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def image_to_data_url(uploaded_image):
    if not uploaded_image:
        return ""
    mime = uploaded_image.type or "image/jpeg"
    import base64
    encoded = base64.b64encode(uploaded_image.getvalue()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def understand_need(product_name, weight, criteria, reference_text, uploaded_image):
    image_description = ""
    search_terms = fallback_query_expansion(product_name, weight)
    profile = {
        "product_family": product_name,
        "key_features": criteria or "Non renseigné",
        "weight_range": weight or "Non renseigné",
        "search_terms": search_terms,
    }
    if not OPENAI_API_KEY:
        return profile, image_description

    content = [{
        "type": "text",
        "text": (
            "Tu assistes un acheteur négoce Délifrance. Analyse le besoin produit sans inventer. "
            "La fiche technique est seulement un exemple semblable. Retourne uniquement un JSON avec: "
            "product_family, visual_description, key_features, weight_range, search_terms_fr, search_terms_en.\n"
            f"Produit: {product_name}\nGrammage: {weight}\nCritères: {criteria}\n"
            f"Extrait FT: {reference_text[:6000]}"
        ),
    }]
    if uploaded_image:
        content.append({"type": "image_url", "image_url": {"url": image_to_data_url(uploaded_image)}})
    try:
        raw = openai_chat([{"role": "user", "content": content}], {"type": "json_object"})
        parsed = json.loads(raw)
        image_description = parsed.get("visual_description", "")
        search_terms = parsed.get("search_terms_fr", []) + parsed.get("search_terms_en", [])
        profile = {
            "product_family": parsed.get("product_family", product_name),
            "key_features": parsed.get("key_features", criteria or "Non renseigné"),
            "weight_range": parsed.get("weight_range", weight or "Non renseigné"),
            "search_terms": list(dict.fromkeys(search_terms))[:16],
        }
    except Exception as exc:
        st.warning(f"Analyse IA indisponible, mode de secours utilisé : {exc}")
    return profile, image_description


def search_internal(pages_index, need_text, limit):
    if not pages_index:
        return pd.DataFrame()
    documents = [normalize_text(row["Texte"]) for row in pages_index]
    query = normalize_text(need_text)
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)
    matrix = vectorizer.fit_transform(documents + [query])
    semantic_scores = cosine_similarity(matrix[-1], matrix[:-1]).flatten()
    rows = []
    for index, row in enumerate(pages_index):
        fuzzy = fuzz.token_set_ratio(query, documents[index]) / 100
        score = round(100 * (0.72 * semantic_scores[index] + 0.28 * fuzzy), 1)
        result = dict(row)
        result["Similarité texte"] = score
        result["Extrait"] = row["Texte"][:600]
        rows.append(result)
    return pd.DataFrame(rows).sort_values("Similarité texte", ascending=False).head(limit)


def tavily_search(query, max_results=8):
    if not TAVILY_API_KEY:
        return []
    response = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "advanced",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def classify_web_results(product_name, profile, raw_results):
    if not raw_results:
        return []
    compact = [{
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "content": item.get("content", "")[:1200],
    } for item in raw_results]

    if not OPENAI_API_KEY:
        output = []
        for item in compact:
            domain = urlparse(item["url"]).netloc
            output.append({
                "company": domain,
                "country": "À confirmer",
                "manufacturer_status": "À confirmer",
                "similar_product": item["title"],
                "product_url": item["url"],
                "technical_sheet_url": "",
                "contact_url": "",
                "private_label": "À confirmer",
                "evidence": item["content"],
                "relevance": "À vérifier",
            })
        return output

    prompt = (
        "Tu es un analyste sourcing industriel agroalimentaire pour Délifrance. "
        "À partir des résultats web, conserve uniquement les fabricants industriels, producteurs, "
        "co-manufacturers ou private-label plausibles. Écarte marketplaces, restaurants, artisans, "
        "supermarchés et simples distributeurs. N'invente aucune URL, usine, certification ou contact. "
        "Une URL seulement présente dans les données peut être retournée. Retourne uniquement un JSON "
        "avec une clé suppliers contenant une liste d'objets: company, country, manufacturer_status, "
        "similar_product, product_url, technical_sheet_url, contact_url, private_label, evidence, relevance. "
        "Si une information manque, écris 'À confirmer' ou une chaîne vide.\n"
        f"Besoin: {product_name}\nProfil: {json.dumps(profile, ensure_ascii=False)}\n"
        f"Résultats: {json.dumps(compact, ensure_ascii=False)}"
    )
    try:
        raw = openai_chat([{"role": "user", "content": prompt}], {"type": "json_object"})
        suppliers = json.loads(raw).get("suppliers", [])
        allowed_urls = {item["url"] for item in compact}
        for supplier in suppliers:
            for field in ("product_url", "technical_sheet_url", "contact_url"):
                url = supplier.get(field, "")
                if url and url not in allowed_urls:
                    supplier[field] = ""
        return suppliers
    except Exception as exc:
        st.warning(f"Qualification IA web indisponible : {exc}")
        return []


def search_web_industrials(product_name, profile):
    queries = []
    terms = profile.get("search_terms", []) or [product_name]
    for term in terms[:8]:
        queries.extend([
            f'{term} manufacturer private label frozen bakery',
            f'{term} industrial producer product',
        ])
    dedup = {}
    for query in queries[:12]:
        try:
            for result in tavily_search(query, max_results=5):
                url = result.get("url", "")
                if url:
                    dedup[url] = result
        except Exception as exc:
            st.warning(f"Recherche web interrompue pour une requête : {exc}")
    return classify_web_results(product_name, profile, list(dedup.values())[:40])


def link_or_status(url, label):
    if url:
        st.link_button(label, url, use_container_width=True)
    else:
        st.caption(f"{label} : non trouvé publiquement")

# --------------------------
# État et chargement bibliothèque
# --------------------------
for key, default in {
    "pages_index": [], "pdf_files": {}, "catalogue_stats": [],
    "loaded_signature": None, "internal_results": None,
    "need_profile": None, "need_text": "", "web_results": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

signature = catalogue_signature()

with st.sidebar:
    st.header("📁 Base catalogues")
    st.metric("PDF disponibles", len(signature))
    if not signature:
        st.error("Aucun PDF trouvé dans catalogues/.")
    else:
        if st.session_state.loaded_signature != signature:
            with st.spinner("Indexation de la bibliothèque..."):
                pages, files, stats = load_catalogue_library(signature)
                st.session_state.pages_index = pages
                st.session_state.pdf_files = files
                st.session_state.catalogue_stats = stats
                st.session_state.loaded_signature = signature
        st.success(f"{len(st.session_state.catalogue_stats)} catalogues chargés")
        st.metric("Pages indexées", len(st.session_state.pages_index))
        if st.button("Réindexer", use_container_width=True):
            st.cache_data.clear()
            st.session_state.loaded_signature = None
            st.rerun()

    st.divider()
    st.subheader("Connexions IA")
    st.write("✅ OpenAI" if OPENAI_API_KEY else "⚪ OpenAI non configuré")
    st.write("✅ Tavily" if TAVILY_API_KEY else "⚪ Tavily non configuré")

st.title("🔎 AI Sourcing Délifrance")
st.caption("1. Recherche interne multimodale · 2. Découverte d'industriels sur Internet")

input_tab, internal_tab, web_tab, library_tab = st.tabs([
    "🎯 Besoin produit", "📁 Résultats internes", "🌍 Industriels web", "📚 Bibliothèque"
])

with input_tab:
    st.header("Décrire le produit recherché")
    col1, col2 = st.columns([2, 1])
    with col1:
        product_name = st.text_input("Nom du produit", placeholder="Mini Danish assortiment")
        criteria = st.text_area("Critères complémentaires, facultatifs", placeholder="Surgelé, assortiment de parfums, private label...")
    with col2:
        weight = st.text_input("Grammage cible", placeholder="40 g")
        result_limit = st.slider("Résultats internes", 5, 50, 15, 5)

    image_file = st.file_uploader("🖼 Photo du produit", type=["jpg", "jpeg", "png", "webp"])
    technical_sheet = st.file_uploader("📄 Fiche technique d'un produit semblable", type=["pdf"])
    if image_file:
        st.image(image_file, width=320, caption="Produit recherché")

    if st.button("1️⃣ Analyser et rechercher dans les catalogues", type="primary", use_container_width=True):
        if not product_name:
            st.warning("Indique le nom du produit.")
        elif not st.session_state.pages_index:
            st.error("La bibliothèque catalogues est vide ou non chargée.")
        else:
            with st.spinner("Compréhension du besoin et recherche interne..."):
                reference_text = extract_reference_sheet_text(technical_sheet)
                profile, image_description = understand_need(
                    product_name, weight, criteria, reference_text, image_file
                )
                need_text = build_need_text(product_name, weight, criteria, reference_text, image_description)
                results = search_internal(st.session_state.pages_index, need_text, result_limit)
                st.session_state.need_profile = profile
                st.session_state.need_text = need_text
                st.session_state.internal_results = results
                st.session_state.web_results = None
            st.success("Recherche interne terminée. Consulte l'onglet Résultats internes.")

with internal_tab:
    st.header("Produits et pages proches dans la base interne")
    results = st.session_state.internal_results
    if results is None:
        st.info("Lance d'abord la recherche depuis l'onglet Besoin produit.")
    elif results.empty:
        st.warning("Aucun résultat interne trouvé.")
    else:
        st.dataframe(
            results[["Fournisseur", "Catalogue", "Page", "Poids", "Référence", "Similarité texte"]],
            use_container_width=True,
            hide_index=True,
        )
        for rank, (_, row) in enumerate(results.head(10).iterrows(), 1):
            with st.expander(f"{rank}. {row['Fournisseur']} · page {row['Page']} · {row['Similarité texte']} %"):
                left, right = st.columns([1, 1.25])
                with left:
                    st.write(f"**Catalogue :** {row['Catalogue']}")
                    st.write(f"**Poids détecté :** {row['Poids']}")
                    st.write(f"**Référence détectée :** {row['Référence']}")
                    st.write(f"**Extrait :** {row['Extrait']}")
                with right:
                    raw = st.session_state.pdf_files.get(row["Catalogue"])
                    if raw:
                        st.image(render_pdf_page(raw, int(row["Page"])), use_container_width=True)

        st.divider()
        st.subheader("Étape 2")
        st.write("La recherche web vise des fabricants industriels, pas de simples revendeurs.")
        if not TAVILY_API_KEY:
            st.warning("Ajoute TAVILY_API_KEY dans les Secrets Streamlit pour activer Internet.")
        if st.button("2️⃣ Rechercher de nouveaux industriels sur Internet", type="primary", use_container_width=True):
            if not TAVILY_API_KEY:
                st.error("Tavily n'est pas configuré.")
            else:
                with st.spinner("Recherche et qualification des industriels..."):
                    product = st.session_state.need_profile.get("product_family", "Produit recherché")
                    st.session_state.web_results = search_web_industrials(product, st.session_state.need_profile)
                st.success("Recherche web terminée. Consulte l'onglet Industriels web.")

with web_tab:
    st.header("Nouveaux industriels à contacter")
    suppliers = st.session_state.web_results
    if suppliers is None:
        st.info("Effectue d'abord la recherche interne, puis lance la recherche Internet.")
    elif not suppliers:
        st.warning("Aucun industriel suffisamment vérifiable n'a été retenu.")
    else:
        for rank, supplier in enumerate(suppliers, 1):
            company = supplier.get("company", "Entreprise à confirmer")
            with st.container(border=True):
                st.subheader(f"{rank}. {company}")
                c1, c2, c3 = st.columns(3)
                c1.write(f"**Pays :** {supplier.get('country', 'À confirmer')}")
                c2.write(f"**Statut :** {supplier.get('manufacturer_status', 'À confirmer')}")
                c3.write(f"**Private label :** {supplier.get('private_label', 'À confirmer')}")
                st.write(f"**Produit similaire :** {supplier.get('similar_product', 'À confirmer')}")
                st.write(f"**Pertinence :** {supplier.get('relevance', 'À vérifier')}")
                st.write(f"**Preuve :** {supplier.get('evidence', 'À confirmer')}")
                b1, b2, b3 = st.columns(3)
                with b1:
                    link_or_status(supplier.get("product_url", ""), "🔗 Page produit")
                with b2:
                    link_or_status(supplier.get("technical_sheet_url", ""), "📄 Fiche technique")
                with b3:
                    link_or_status(supplier.get("contact_url", ""), "✉️ Contact")
        st.download_button(
            "Exporter la shortlist CSV",
            pd.DataFrame(suppliers).to_csv(index=False).encode("utf-8-sig"),
            "shortlist_industriels.csv",
            "text/csv",
        )

with library_tab:
    st.header("État de la bibliothèque")
    if st.session_state.catalogue_stats:
        st.dataframe(pd.DataFrame(st.session_state.catalogue_stats), use_container_width=True, hide_index=True)
    else:
        st.info("Aucun catalogue chargé.")
    st.caption(
        "Les PDF scannés sans couche texte nécessiteront un module OCR. "
        "La recherche visuelle avancée nécessite une clé OpenAI et décrit l'image pour enrichir la recherche interne et web."
    )

st.divider()
st.caption(
    "Assistant de sourcing pour l'acheteur négoce Délifrance. "
    "Vérifie les preuves, le statut industriel, les capacités, certifications, MOQ et conditions avant tout contact."
)
