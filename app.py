import io
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import boto3
import fitz
import pandas as pd
import requests
import streamlit as st
from botocore.config import Config
from PIL import Image
from rapidfuzz import fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

st.set_page_config(page_title="AI Sourcing Delifrance", page_icon="🔎", layout="wide")

HTTP_TIMEOUT = 30
INDEX_CACHE_TTL = 3600


def get_secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


R2_ACCOUNT_ID = get_secret("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = get_secret("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = get_secret("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = get_secret("R2_BUCKET_NAME")
R2_PREFIX = get_secret("R2_PREFIX", "catalogues/").strip("/") + "/"
TAVILY_API_KEY = get_secret("TAVILY_API_KEY")


def configured(value, prefix=None):
    if not value or value.lower().startswith(("votre-", "your-", "colle-")):
        return False
    return not prefix or value.startswith(prefix)


def r2_is_configured():
    return all([
        configured(R2_ACCOUNT_ID),
        configured(R2_ACCESS_KEY_ID),
        configured(R2_SECRET_ACCESS_KEY),
        configured(R2_BUCKET_NAME),
    ])


@st.cache_resource
def get_r2_client(account_id, access_key_id, secret_access_key):
    return boto3.client(
        service_name="s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 4, "mode": "standard"},
            connect_timeout=15,
            read_timeout=60,
        ),
    )


def r2_client():
    if not r2_is_configured():
        raise RuntimeError("Configuration Cloudflare R2 incomplete dans les Secrets Streamlit.")
    return get_r2_client(R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY)


def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^A-Z0-9\s/.,'()+-]", " ", text.upper())
    return re.sub(r"\s+", " ", text).strip()


def supplier_from_key(key):
    stem = Path(key).stem
    parts = [part for part in re.split(r"[\s_-]+", stem) if part]
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


def list_r2_pdfs():
    client = r2_client()
    paginator = client.get_paginator("list_objects_v2")
    objects = []
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=R2_PREFIX):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.lower().endswith(".pdf"):
                objects.append({
                    "Key": key,
                    "Size": int(item.get("Size", 0)),
                    "ETag": str(item.get("ETag", "")).strip('"'),
                    "LastModified": item.get("LastModified"),
                })
    return sorted(objects, key=lambda item: item["Key"].lower())


def library_signature(objects):
    return tuple((item["Key"], item["Size"], item["ETag"]) for item in objects)


def get_r2_bytes(key):
    response = r2_client().get_object(Bucket=R2_BUCKET_NAME, Key=key)
    return response["Body"].read()


@st.cache_data(show_spinner=False, ttl=INDEX_CACHE_TTL, max_entries=1)
def build_library_index(signature):
    pages_index = []
    catalogue_stats = []

    for key, size, etag in signature:
        supplier = supplier_from_key(key)
        page_count = 0
        indexed_pages = 0
        status = "OK"
        try:
            pdf_bytes = get_r2_bytes(key)
            document = fitz.open(stream=pdf_bytes, filetype="pdf")
            page_count = document.page_count
            for page_index in range(page_count):
                text = re.sub(r"\s+", " ", document.load_page(page_index).get_text("text")).strip()
                if not text:
                    continue
                pages_index.append({
                    "Fournisseur": supplier,
                    "Catalogue": key,
                    "Page": page_index + 1,
                    "Texte": text,
                    "Poids": extract_weight(text),
                    "Reference": extract_reference(text),
                })
                indexed_pages += 1
            document.close()
            del pdf_bytes
            if indexed_pages == 0:
                status = "OCR requis"
        except Exception as exc:
            status = f"Erreur: {str(exc)[:140]}"

        catalogue_stats.append({
            "Fournisseur": supplier,
            "Catalogue": key,
            "Taille Mo": round(size / 1024 / 1024, 2),
            "Pages": page_count,
            "Pages indexees": indexed_pages,
            "Statut": status,
            "ETag": etag,
        })

    return pages_index, catalogue_stats


@st.cache_data(show_spinner=False, ttl=1800, max_entries=4)
def load_r2_pdf_page(key, etag, page_number):
    pdf_bytes = get_r2_bytes(key)
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = document.load_page(page_number - 1)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.15, 1.15), alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
    document.close()
    return image


def upload_catalogues(files, folder):
    client = r2_client()
    uploaded = []
    safe_folder = re.sub(r"[^A-Za-z0-9/_-]", "_", folder.strip("/"))
    base = R2_PREFIX + (safe_folder + "/" if safe_folder else "")
    for uploaded_file in files:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", uploaded_file.name)
        key = base + safe_name
        client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=key,
            Body=uploaded_file.getvalue(),
            ContentType="application/pdf",
            Metadata={"uploaded-by": "ai-sourcing-streamlit"},
        )
        uploaded.append(key)
    return uploaded


def extract_uploaded_pdf_text(uploaded_pdf, max_chars=12000):
    if not uploaded_pdf:
        return ""
    document = fitz.open(stream=uploaded_pdf.getvalue(), filetype="pdf")
    parts = []
    length = 0
    for page_index in range(document.page_count):
        text = document.load_page(page_index).get_text("text")
        parts.append(text)
        length += len(text)
        if length >= max_chars:
            break
    document.close()
    return "\n".join(parts)[:max_chars]


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


def search_internal(pages_index, query_text, limit):
    if not pages_index:
        return pd.DataFrame()
    documents = [normalize_text(row["Texte"]) for row in pages_index]
    query = normalize_text(query_text)
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, max_features=30000)
    matrix = vectorizer.fit_transform(documents + [query])
    semantic_scores = cosine_similarity(matrix[-1], matrix[:-1]).flatten()

    rows = []
    for index, item in enumerate(pages_index):
        fuzzy_score = fuzz.token_set_ratio(query, documents[index]) / 100
        score = round(100 * (0.72 * semantic_scores[index] + 0.28 * fuzzy_score), 1)
        row = dict(item)
        row["Similarite texte"] = score
        row["Extrait"] = item["Texte"][:650]
        rows.append(row)
    return pd.DataFrame(rows).sort_values("Similarite texte", ascending=False).head(limit)


def tavily_search(query, max_results=8):
    if not configured(TAVILY_API_KEY, "tvly-"):
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


def qualify_web_results(product_name, raw_results):
    excluded = {
        "amazon", "walmart", "carrefour", "tesco", "restaurant", "bakery shop",
        "deliveroo", "ubereats", "marketplace", "retail", "recipe", "pinterest",
        "instagram", "facebook", "youtube", "wikipedia", "tripadvisor",
    }
    industrial = {
        "manufacturer", "manufacturing", "producer", "factory", "industrial",
        "private label", "foodservice", "wholesale", "b2b", "frozen bakery",
        "production facility", "co-manufacturer", "supplier", "export",
    }
    product_terms = {term.lower() for term in normalize_text(product_name).split() if len(term) > 2}
    qualified = []

    for result in raw_results:
        title = result.get("title", "")
        url = result.get("url", "")
        content = result.get("content", "")
        combined = f"{title} {content} {url}".lower()
        if not url or any(term in combined for term in excluded):
            continue
        industrial_hits = sum(1 for term in industrial if term in combined)
        product_hits = sum(1 for term in product_terms if term in combined)
        score = industrial_hits * 18 + product_hits * 8
        if score < 10:
            continue
        if industrial_hits >= 2:
            status = "Fabricant probable"
        elif industrial_hits == 1:
            status = "Statut industriel a confirmer"
        else:
            status = "A verifier"
        qualified.append({
            "company": urlparse(url).netloc.replace("www.", ""),
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
    output = []
    for item in qualified:
        if item["company"] in seen_domains:
            continue
        seen_domains.add(item["company"])
        output.append(item)
    return output[:15]


def verified_benchmarks(product_name):
    normalized = normalize_text(product_name)
    if "DANISH" not in normalized and "VIENNOISERIE" not in normalized:
        return []
    return [
        {
            "company": "La Lorraine Bakery Group", "country": "Belgique",
            "manufacturer_status": "Fabricant industriel confirme",
            "similar_product": "Panesco Mini Danish Mix 40 g",
            "product_url": "https://llbg.com/en-EU/products/5000929-mini-danish-mix",
            "technical_sheet_url": "https://llbg.com/en-EU/products/5000929-mini-danish-mix",
            "contact_url": "https://llbg.com/en-EU/contact", "private_label": "A confirmer",
            "evidence": "Page produit officielle: assortiment de cinq mini Danish, poids 40 g.", "relevance": 100,
        },
        {
            "company": "Kohberg", "country": "Danemark",
            "manufacturer_status": "Fabricant industriel confirme",
            "similar_product": "Mini Danish Pastries Mix, 5 variants, 40 g",
            "product_url": "https://www.kohberg.com/our-products/product-overview/danish-pastries/mini-danish-pastries-mix-5-variants/",
            "technical_sheet_url": "", "contact_url": "https://www.kohberg.com/contact/",
            "private_label": "A confirmer", "evidence": "Page produit officielle: mix de cinq mini Danish, poids 40 g.",
            "relevance": 99,
        },
        {
            "company": "Gourmand Pastries", "country": "Belgique",
            "manufacturer_status": "Fabricant industriel confirme", "similar_product": "Mix Mini Danish 42 g",
            "product_url": "https://gourmandpastries.com/en-int/c/bake-off-pastries-en-int/bake-off-danish-pastries-en-int/p/mix-mini-danish-42g/",
            "technical_sheet_url": "", "contact_url": "https://gourmandpastries.com/en/contact/",
            "private_label": "Oui", "evidence": "Page produit officielle: mix mini Danish 42 g.", "relevance": 98,
        },
        {
            "company": "Lantmannen Unibake", "country": "Europe",
            "manufacturer_status": "Fabricant industriel confirme", "similar_product": "Mini Danish Selection",
            "product_url": "https://www.lantmannenunibake.com/all-products/mini-danish-selection3/",
            "technical_sheet_url": "", "contact_url": "https://www.lantmannenunibake.com/contact/",
            "private_label": "A confirmer", "evidence": "Page produit officielle: assortiment de mini Danish surgeles.",
            "relevance": 96,
        },
        {
            "company": "Europastry", "country": "Espagne",
            "manufacturer_status": "Fabricant industriel confirme", "similar_product": "Gamme Mini Danish 30 a 42 g",
            "product_url": "https://europastry.com/fr/fr/product/mini-danish-pomme-creme/",
            "technical_sheet_url": "", "contact_url": "https://europastry.com/fr/fr/contact/",
            "private_label": "A confirmer", "evidence": "Page produit officielle de mini Danish.", "relevance": 90,
        },
    ]


def search_industrials(product_name, terms):
    focused_queries = [
        f'"{product_name}" manufacturer Europe frozen bakery',
        f'"{product_name}" product assortment 35g 45g',
        f'{terms[0]} manufacturer private label foodservice Europe',
        f'{terms[1] if len(terms) > 1 else product_name} industrial producer frozen pastry',
    ]
    web_results = {}
    for query in focused_queries:
        for result in tavily_search(query):
            url = result.get("url", "")
            if url:
                web_results[url] = result

    merged = verified_benchmarks(product_name) + qualify_web_results(product_name, list(web_results.values()))
    merged.sort(key=lambda item: item.get("relevance", 0), reverse=True)
    output = []
    seen = set()
    for item in merged:
        domain = urlparse(item.get("product_url", "")).netloc.replace("www.", "")
        key = domain or normalize_text(item.get("company", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output[:20]


def show_link(url, label):
    if url:
        st.link_button(label, url, width="stretch")
    else:
        st.caption(f"{label}: non trouve publiquement")


for key, default in {
    "r2_objects": [], "r2_signature": None, "pages_index": [], "catalogue_stats": [],
    "internal_results": None, "product_name": "", "search_terms": [], "web_results": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

st.title("🔎 AI Sourcing Delifrance")
st.caption("Catalogues Cloudflare R2 · Recherche interne · Sourcing industriel Internet")

with st.sidebar:
    st.header("☁️ Cloudflare R2")
    if not r2_is_configured():
        st.error("R2 non configure dans les Secrets Streamlit.")
    else:
        if st.button("Synchroniser R2", type="primary", width="stretch") or not st.session_state.r2_objects:
            try:
                objects = list_r2_pdfs()
                signature = library_signature(objects)
                st.session_state.r2_objects = objects
                if st.session_state.r2_signature != signature:
                    with st.spinner("Indexation des catalogues R2..."):
                        pages, stats = build_library_index(signature)
                        st.session_state.pages_index = pages
                        st.session_state.catalogue_stats = stats
                        st.session_state.r2_signature = signature
                        st.session_state.internal_results = None
                st.success("Synchronisation terminee")
            except Exception as exc:
                st.error(f"Erreur R2: {exc}")

        st.metric("PDF R2", len(st.session_state.r2_objects))
        st.metric("Pages indexees", len(st.session_state.pages_index))
        if st.session_state.r2_objects:
            total_size = sum(item["Size"] for item in st.session_state.r2_objects)
            st.metric("Stockage catalogue", f"{total_size / 1024 / 1024:.1f} Mo")

        with st.expander("Ajouter des catalogues dans R2"):
            folder = st.text_input("Sous-dossier", placeholder="viennoiseries/unibake")
            files = st.file_uploader("PDF a envoyer", type=["pdf"], accept_multiple_files=True)
            if st.button("Envoyer vers R2", width="stretch"):
                if not files:
                    st.warning("Selectionne au moins un PDF.")
                else:
                    try:
                        keys = upload_catalogues(files, folder)
                        st.success(f"{len(keys)} fichier(s) envoye(s). Clique sur Synchroniser R2.")
                    except Exception as exc:
                        st.error(f"Echec de l'envoi: {exc}")

    st.divider()
    st.write("✅ Tavily configure" if configured(TAVILY_API_KEY, "tvly-") else "⚪ Tavily non configure")
    st.caption("Mode gratuit sans OpenAI")

need_tab, internal_tab, web_tab, library_tab = st.tabs([
    "🎯 Besoin produit", "📁 Resultats internes", "🌍 Industriels web", "📚 Bibliotheque R2"
])

with need_tab:
    st.header("Decrire le produit recherche")
    column1, column2 = st.columns([2, 1])
    with column1:
        product_name = st.text_input("Nom du produit", placeholder="Mini Danish assortiment")
        criteria = st.text_area("Criteres complementaires", placeholder="Surgele, assortiment, private label...")
    with column2:
        weight = st.text_input("Grammage cible", placeholder="40 g")
        result_limit = st.slider("Nombre de resultats", 5, 40, 15, 5)

    image_file = st.file_uploader("Photo du produit", type=["jpg", "jpeg", "png", "webp"])
    reference_sheet = st.file_uploader("Fiche technique d'un produit semblable", type=["pdf"])
    if image_file:
        st.image(image_file, width=320, caption="Produit recherche")

    if st.button("1. Rechercher dans les catalogues R2", type="primary", width="stretch"):
        if not product_name:
            st.warning("Indique le nom du produit.")
        elif not st.session_state.pages_index:
            st.error("Aucune page indexee. Synchronise R2 ou verifie les PDF.")
        else:
            reference_text = extract_uploaded_pdf_text(reference_sheet)
            terms = fallback_terms(product_name, weight)
            query_text = "\n".join(filter(None, [product_name, weight, criteria, reference_text, " ".join(terms)]))
            with st.spinner("Recherche dans la base R2..."):
                st.session_state.internal_results = search_internal(
                    st.session_state.pages_index, query_text, result_limit
                )
                st.session_state.product_name = product_name
                st.session_state.search_terms = terms
                st.session_state.web_results = None
            st.success("Recherche terminee. Consulte Resultats internes.")

with internal_tab:
    st.header("Resultats de la bibliotheque R2")
    results = st.session_state.internal_results
    if results is None:
        st.info("Lance d'abord une recherche.")
    elif results.empty:
        st.warning("Aucun resultat interne trouve.")
    else:
        st.dataframe(
            results[["Fournisseur", "Catalogue", "Page", "Poids", "Reference", "Similarite texte"]],
            width="stretch", hide_index=True,
        )
        object_map = {item["Key"]: item for item in st.session_state.r2_objects}
        for rank, (_, row) in enumerate(results.head(8).iterrows(), start=1):
            with st.expander(f"{rank}. {row['Fournisseur']} · page {row['Page']} · {row['Similarite texte']} %"):
                left, right = st.columns([1, 1.25])
                with left:
                    st.write(f"**Catalogue:** {row['Catalogue']}")
                    st.write(f"**Poids detecte:** {row['Poids']}")
                    st.write(f"**Reference:** {row['Reference']}")
                    st.write(f"**Extrait:** {row['Extrait']}")
                with right:
                    item = object_map.get(row["Catalogue"], {})
                    try:
                        page_image = load_r2_pdf_page(
                            row["Catalogue"], item.get("ETag", ""), int(row["Page"])
                        )
                        st.image(page_image, width="stretch")
                    except Exception as exc:
                        st.warning(f"Apercu indisponible: {exc}")

        st.divider()
        if st.button("2. Rechercher de nouveaux industriels", type="primary", width="stretch"):
            if not configured(TAVILY_API_KEY, "tvly-"):
                st.error("Configure TAVILY_API_KEY dans les Secrets Streamlit.")
            else:
                try:
                    with st.spinner("Recherche des fabricants industriels..."):
                        st.session_state.web_results = search_industrials(
                            st.session_state.product_name, st.session_state.search_terms
                        )
                    st.success("Recherche terminee. Consulte Industriels web.")
                except Exception as exc:
                    st.error(f"Recherche Internet impossible: {exc}")

with web_tab:
    st.header("Industriels potentiels a contacter")
    suppliers = st.session_state.web_results
    if suppliers is None:
        st.info("Lance la recherche Internet depuis Resultats internes.")
    elif not suppliers:
        st.warning("Aucun industriel suffisamment pertinent trouve.")
    else:
        for rank, supplier in enumerate(suppliers, start=1):
            with st.container(border=True):
                st.subheader(f"{rank}. {supplier.get('company', 'Entreprise a confirmer')}")
                c1, c2, c3 = st.columns(3)
                c1.write(f"**Pays:** {supplier.get('country', 'A confirmer')}")
                c2.write(f"**Statut:** {supplier.get('manufacturer_status', 'A confirmer')}")
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
            "shortlist_industriels.csv", "text/csv", width="stretch",
        )

with library_tab:
    st.header("Bibliotheque Cloudflare R2")
    if st.session_state.catalogue_stats:
        st.dataframe(pd.DataFrame(st.session_state.catalogue_stats), width="stretch", hide_index=True)
    else:
        st.info("Aucun catalogue synchronise.")
    st.caption("OCR requis signifie que le PDF ne contient pas de texte directement exploitable.")

st.divider()
st.caption(
    "Assistant de sourcing pour l'acheteur tiers Delifrance. "
    "Verifier les preuves, capacites, certifications, MOQ et conditions avant tout contact fournisseur."
)
