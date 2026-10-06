import io
import re
import unicodedata
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import cloudinary
import cloudinary.api
import cloudinary.uploader
import fitz
import pandas as pd
import requests
import streamlit as st
from PIL import Image
from rapidfuzz import fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

st.set_page_config(page_title="AI Sourcing Delifrance", page_icon="🔎", layout="wide")

HTTP_TIMEOUT = 45
INDEX_TTL = 3600
MAX_RAW_BYTES = 9 * 1024 * 1024


def get_secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


CLOUDINARY_CLOUD_NAME = get_secret("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = get_secret("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = get_secret("CLOUDINARY_API_SECRET")
CLOUDINARY_PREFIX = get_secret("CLOUDINARY_PREFIX", "ai-sourcing/catalogues").strip("/")
TAVILY_API_KEY = get_secret("TAVILY_API_KEY")


def configured(value, prefix=None):
    if not value or value.lower().startswith(("votre-", "your-", "colle-")):
        return False
    return not prefix or value.startswith(prefix)


def cloudinary_ready():
    return all([
        configured(CLOUDINARY_CLOUD_NAME),
        configured(CLOUDINARY_API_KEY),
        configured(CLOUDINARY_API_SECRET),
    ])


@st.cache_resource
def configure_cloudinary(cloud_name, api_key, api_secret):
    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    return True


def ensure_cloudinary():
    if not cloudinary_ready():
        raise RuntimeError("Configuration Cloudinary incomplete dans les Secrets Streamlit.")
    configure_cloudinary(CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)


def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^A-Z0-9\s/.,'()+-]", " ", text.upper())
    return re.sub(r"\s+", " ", text).strip()


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def supplier_from_public_id(public_id):
    stem = PurePosixPath(public_id).name
    stem = re.sub(r"_part_\d+_pages_\d+-\d+$", "", Path(stem).stem)
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


def optimized_pdf_bytes(document):
    return document.tobytes(garbage=4, deflate=True, clean=True)


def rasterize_single_page(source_page, max_bytes=MAX_RAW_BYTES):
    for zoom, quality in ((1.25, 70), (1.0, 60), (0.8, 48), (0.65, 40)):
        pixmap = source_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        jpeg = pixmap.tobytes("jpeg", jpg_quality=quality)
        output = fitz.open()
        page = output.new_page(width=source_page.rect.width, height=source_page.rect.height)
        page.insert_image(page.rect, stream=jpeg)
        data = optimized_pdf_bytes(output)
        output.close()
        if len(data) <= max_bytes:
            return data
    raise ValueError("Une page reste superieure a 9 Mo apres compression.")


def split_pdf_bytes(pdf_bytes, max_bytes=MAX_RAW_BYTES):
    source = fitz.open(stream=pdf_bytes, filetype="pdf")
    chunks = []
    current = fitz.open()
    current_start = 1

    for page_index in range(source.page_count):
        trial = fitz.open()
        if current.page_count:
            trial.insert_pdf(current)
        trial.insert_pdf(source, from_page=page_index, to_page=page_index)
        trial_bytes = optimized_pdf_bytes(trial)

        if len(trial_bytes) <= max_bytes:
            current.close()
            current = trial
            continue

        trial.close()
        if current.page_count:
            chunks.append({
                "bytes": optimized_pdf_bytes(current),
                "page_start": current_start,
                "page_end": page_index,
            })
            current.close()
            current = fitz.open()
            current_start = page_index + 1

        single = fitz.open()
        single.insert_pdf(source, from_page=page_index, to_page=page_index)
        single_bytes = optimized_pdf_bytes(single)
        single.close()

        if len(single_bytes) > max_bytes:
            single_bytes = rasterize_single_page(source.load_page(page_index), max_bytes)

        single_doc = fitz.open(stream=single_bytes, filetype="pdf")
        current.insert_pdf(single_doc)
        single_doc.close()

    if current.page_count:
        chunks.append({
            "bytes": optimized_pdf_bytes(current),
            "page_start": current_start,
            "page_end": source.page_count,
        })

    current.close()
    source.close()
    return chunks


def upload_pdfs(files, folder, progress_callback=None):
    ensure_cloudinary()
    safe_folder = re.sub(r"[^A-Za-z0-9/_-]", "_", folder.strip("/"))
    target_folder = CLOUDINARY_PREFIX + ("/" + safe_folder if safe_folder else "")
    uploaded_parts = []

    for file_index, uploaded_file in enumerate(files, start=1):
        chunks = split_pdf_bytes(uploaded_file.getvalue())
        safe_stem = safe_name(Path(uploaded_file.name).stem)
        total_parts = len(chunks)

        for part_number, chunk in enumerate(chunks, start=1):
            suffix = f"_part_{part_number:02d}_pages_{chunk['page_start']}-{chunk['page_end']}"
            public_id = f"{target_folder}/{safe_stem}{suffix}.pdf"
            result = cloudinary.uploader.upload(
                io.BytesIO(chunk["bytes"]),
                resource_type="raw",
                type="upload",
                public_id=public_id,
                overwrite=True,
                invalidate=True,
                tags=["ai-sourcing", "catalogue-fournisseur", safe_stem],
                context={
                    "original_filename": uploaded_file.name,
                    "part": f"{part_number}/{total_parts}",
                    "original_pages": f"{chunk['page_start']}-{chunk['page_end']}",
                },
            )
            uploaded_parts.append({
                "catalogue": uploaded_file.name,
                "public_id": result.get("public_id", public_id),
                "part": part_number,
                "total_parts": total_parts,
                "page_start": chunk["page_start"],
                "page_end": chunk["page_end"],
                "size_mb": round(len(chunk["bytes"]) / 1024 / 1024, 2),
            })
            if progress_callback:
                progress_callback(file_index, len(files), part_number, total_parts)

    return uploaded_parts


def list_cloudinary_pdfs():
    ensure_cloudinary()
    resources = []
    cursor = None
    while True:
        kwargs = {
            "resource_type": "raw",
            "type": "upload",
            "prefix": CLOUDINARY_PREFIX + "/",
            "max_results": 500,
        }
        if cursor:
            kwargs["next_cursor"] = cursor
        response = cloudinary.api.resources(**kwargs)
        for item in response.get("resources", []):
            public_id = item.get("public_id", "")
            if public_id.lower().endswith(".pdf") or str(item.get("format", "")).lower() == "pdf":
                resources.append({
                    "public_id": public_id,
                    "secure_url": item.get("secure_url", ""),
                    "bytes": int(item.get("bytes", 0)),
                    "version": int(item.get("version", 0)),
                })
        cursor = response.get("next_cursor")
        if not cursor:
            break
    return sorted(resources, key=lambda item: item["public_id"].lower())


def library_signature(resources):
    return tuple((r["public_id"], r["bytes"], r["version"], r["secure_url"]) for r in resources)


def download_pdf(url):
    response = requests.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.content


@st.cache_data(show_spinner=False, ttl=INDEX_TTL, max_entries=1)
def build_index(signature):
    pages = []
    stats = []
    for public_id, size, version, secure_url in signature:
        supplier = supplier_from_public_id(public_id)
        page_count = 0
        indexed = 0
        status = "OK"
        try:
            pdf_bytes = download_pdf(secure_url)
            document = fitz.open(stream=pdf_bytes, filetype="pdf")
            page_count = document.page_count
            for page_index in range(page_count):
                text = re.sub(r"\s+", " ", document.load_page(page_index).get_text("text")).strip()
                if not text:
                    continue
                pages.append({
                    "Fournisseur": supplier,
                    "Catalogue": public_id,
                    "URL": secure_url,
                    "Version": version,
                    "Page": page_index + 1,
                    "Texte": text,
                    "Poids": extract_weight(text),
                    "Reference": extract_reference(text),
                })
                indexed += 1
            document.close()
            if indexed == 0:
                status = "OCR requis"
        except Exception as exc:
            status = f"Erreur: {str(exc)[:130]}"
        stats.append({
            "Fournisseur": supplier,
            "Catalogue": public_id,
            "Taille Mo": round(size / 1024 / 1024, 2),
            "Pages": page_count,
            "Pages indexees": indexed,
            "Statut": status,
        })
    return pages, stats


@st.cache_data(show_spinner=False, ttl=1800, max_entries=4)
def load_page_image(secure_url, version, page_number):
    pdf_bytes = download_pdf(secure_url)
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = document.load_page(page_number - 1)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.15, 1.15), alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
    document.close()
    return image


def extract_uploaded_pdf_text(uploaded_pdf, max_chars=12000):
    if not uploaded_pdf:
        return ""
    document = fitz.open(stream=uploaded_pdf.getvalue(), filetype="pdf")
    texts = []
    length = 0
    for index in range(document.page_count):
        text = document.load_page(index).get_text("text")
        texts.append(text)
        length += len(text)
        if length >= max_chars:
            break
    document.close()
    return "\n".join(texts)[:max_chars]


def fallback_terms(product_name, weight):
    normalized = normalize_text(product_name)
    terms = [product_name]
    groups = {
        "DANISH": ["mini Danish", "assorted Danish pastries", "Danish selection", "mini viennoiseries", "frozen laminated pastry"],
        "CROISSANT": ["frozen croissant", "laminated pastry", "industrial viennoiserie"],
        "BEIGNET": ["industrial donut", "filled doughnut", "frozen beignet"],
        "BAGUETTE": ["industrial frozen baguette", "part baked baguette", "frozen bakery"],
        "TARTE": ["industrial tart", "frozen tart", "private label pastry"],
    }
    for keyword, alternatives in groups.items():
        if keyword in normalized:
            terms.extend(alternatives)
    if weight:
        terms = [f"{term} {weight}" for term in terms]
    return list(dict.fromkeys(terms))[:12]


def search_internal(pages, query_text, limit):
    if not pages:
        return pd.DataFrame()
    documents = [normalize_text(row["Texte"]) for row in pages]
    query = normalize_text(query_text)
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, max_features=30000)
    matrix = vectorizer.fit_transform(documents + [query])
    semantic = cosine_similarity(matrix[-1], matrix[:-1]).flatten()
    results = []
    for index, item in enumerate(pages):
        fuzzy_score = fuzz.token_set_ratio(query, documents[index]) / 100
        row = dict(item)
        row["Similarite texte"] = round(100 * (0.72 * semantic[index] + 0.28 * fuzzy_score), 1)
        row["Extrait"] = item["Texte"][:650]
        results.append(row)
    return pd.DataFrame(results).sort_values("Similarite texte", ascending=False).head(limit)


def tavily_search(query, max_results=8):
    if not configured(TAVILY_API_KEY, "tvly-"):
        raise RuntimeError("TAVILY_API_KEY absente ou invalide.")
    response = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"},
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


def qualify_web(product_name, raw_results):
    excluded = {"amazon", "walmart", "carrefour", "restaurant", "marketplace", "retail", "recipe", "pinterest", "instagram", "facebook", "youtube", "wikipedia"}
    industrial = {"manufacturer", "manufacturing", "producer", "factory", "industrial", "private label", "foodservice", "wholesale", "b2b", "frozen bakery", "production facility", "co-manufacturer", "supplier", "export"}
    product_terms = {term.lower() for term in normalize_text(product_name).split() if len(term) > 2}
    results = []
    for item in raw_results:
        title = item.get("title", "")
        url = item.get("url", "")
        content = item.get("content", "")
        combined = f"{title} {url} {content}".lower()
        if not url or any(term in combined for term in excluded):
            continue
        industrial_hits = sum(term in combined for term in industrial)
        product_hits = sum(term in combined for term in product_terms)
        score = industrial_hits * 18 + product_hits * 8
        if score < 10:
            continue
        results.append({
            "company": urlparse(url).netloc.replace("www.", ""),
            "country": "A confirmer",
            "manufacturer_status": "Fabricant probable" if industrial_hits >= 2 else "Statut industriel a confirmer",
            "similar_product": title,
            "product_url": url,
            "technical_sheet_url": url if url.lower().endswith(".pdf") else "",
            "contact_url": "",
            "private_label": "Oui probable" if "private label" in combined else "A confirmer",
            "evidence": content[:550] or title,
            "relevance": min(score, 100),
        })
    results.sort(key=lambda row: row["relevance"], reverse=True)
    output, seen = [], set()
    for row in results:
        if row["company"] in seen:
            continue
        seen.add(row["company"])
        output.append(row)
    return output[:15]


def danish_benchmarks(product_name):
    normalized = normalize_text(product_name)
    if "DANISH" not in normalized and "VIENNOISERIE" not in normalized:
        return []
    return [
        {"company": "La Lorraine Bakery Group", "country": "Belgique", "manufacturer_status": "Fabricant industriel confirme", "similar_product": "Panesco Mini Danish Mix 40 g", "product_url": "https://llbg.com/en-EU/products/5000929-mini-danish-mix", "technical_sheet_url": "", "contact_url": "https://llbg.com/en-EU/contact", "private_label": "A confirmer", "evidence": "Page produit officielle, assortiment de cinq mini Danish, 40 g.", "relevance": 100},
        {"company": "Kohberg", "country": "Danemark", "manufacturer_status": "Fabricant industriel confirme", "similar_product": "Mini Danish Pastries Mix, 5 variants, 40 g", "product_url": "https://www.kohberg.com/our-products/product-overview/danish-pastries/mini-danish-pastries-mix-5-variants/", "technical_sheet_url": "", "contact_url": "https://www.kohberg.com/contact/", "private_label": "A confirmer", "evidence": "Page produit officielle, cinq mini Danish, 40 g.", "relevance": 99},
        {"company": "Gourmand Pastries", "country": "Belgique", "manufacturer_status": "Fabricant industriel confirme", "similar_product": "Mix Mini Danish 42 g", "product_url": "https://gourmandpastries.com/en-int/c/bake-off-pastries-en-int/bake-off-danish-pastries-en-int/p/mix-mini-danish-42g/", "technical_sheet_url": "", "contact_url": "https://gourmandpastries.com/en/contact/", "private_label": "Oui", "evidence": "Page produit officielle, mix mini Danish 42 g.", "relevance": 98},
        {"company": "Lantmannen Unibake", "country": "Europe", "manufacturer_status": "Fabricant industriel confirme", "similar_product": "Mini Danish Selection", "product_url": "https://www.lantmannenunibake.com/all-products/mini-danish-selection3/", "technical_sheet_url": "", "contact_url": "https://www.lantmannenunibake.com/contact/", "private_label": "A confirmer", "evidence": "Page produit officielle, assortiment de mini Danish surgeles.", "relevance": 96},
        {"company": "Europastry", "country": "Espagne", "manufacturer_status": "Fabricant industriel confirme", "similar_product": "Gamme Mini Danish 30 a 42 g", "product_url": "https://europastry.com/fr/fr/product/mini-danish-pomme-creme/", "technical_sheet_url": "", "contact_url": "https://europastry.com/fr/fr/contact/", "private_label": "A confirmer", "evidence": "Page produit officielle de mini Danish.", "relevance": 90},
    ]


def search_industrials(product_name, terms):
    queries = [
        f'"{product_name}" manufacturer Europe frozen bakery',
        f'"{product_name}" product assortment 35g 45g',
        f'{terms[0]} manufacturer private label foodservice Europe',
        f'{terms[1] if len(terms) > 1 else product_name} industrial producer frozen pastry',
    ]
    web = {}
    for query in queries:
        for item in tavily_search(query):
            if item.get("url"):
                web[item["url"]] = item
    merged = danish_benchmarks(product_name) + qualify_web(product_name, list(web.values()))
    merged.sort(key=lambda row: row.get("relevance", 0), reverse=True)
    output, seen = [], set()
    for row in merged:
        domain = urlparse(row.get("product_url", "")).netloc.replace("www.", "")
        key = domain or normalize_text(row.get("company", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output[:20]


def show_link(url, label):
    if url:
        st.link_button(label, url, width="stretch")
    else:
        st.caption(f"{label}: non trouve publiquement")


for key, default in {
    "resources": [], "signature": None, "pages": [], "stats": [],
    "internal_results": None, "product_name": "", "terms": [], "web_results": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

st.title("🔎 AI Sourcing Delifrance")
st.caption("Catalogues Cloudinary · Recherche interne · Sourcing industriel Internet")

with st.sidebar:
    st.header("☁️ Cloudinary")
    if not cloudinary_ready():
        st.error("Cloudinary non configure dans les Secrets Streamlit.")
    else:
        if st.button("Synchroniser Cloudinary", type="primary", width="stretch") or not st.session_state.resources:
            try:
                resources = list_cloudinary_pdfs()
                signature = library_signature(resources)
                st.session_state.resources = resources
                if st.session_state.signature != signature:
                    with st.spinner("Indexation des catalogues Cloudinary..."):
                        pages, stats = build_index(signature)
                        st.session_state.pages = pages
                        st.session_state.stats = stats
                        st.session_state.signature = signature
                        st.session_state.internal_results = None
                st.success("Synchronisation terminee")
            except Exception as exc:
                st.error(f"Erreur Cloudinary: {exc}")

        st.metric("PDF Cloudinary", len(st.session_state.resources))
        st.metric("Pages indexees", len(st.session_state.pages))
        if st.session_state.resources:
            total_size = sum(item["bytes"] for item in st.session_state.resources)
            st.metric("Stockage catalogue", f"{total_size / 1024 / 1024:.1f} Mo")

        with st.expander("Ajouter des catalogues"):
            folder = st.text_input("Sous-dossier", placeholder="viennoiseries/unibake")
            files = st.file_uploader("PDF a envoyer", type=["pdf"], accept_multiple_files=True)
            st.caption("Les PDF de plus de 9 Mo sont compresses et decoupes automatiquement.")
            if st.button("Envoyer vers Cloudinary", width="stretch"):
                if not files:
                    st.warning("Selectionne au moins un PDF.")
                else:
                    progress = st.progress(0)
                    status = st.empty()

                    def update_progress(file_number, file_total, part_number, part_total):
                        completed_before = file_number - 1
                        fraction = (completed_before + part_number / part_total) / file_total
                        progress.progress(min(fraction, 1.0))
                        status.write(
                            f"Catalogue {file_number}/{file_total} · partie {part_number}/{part_total}"
                        )

                    try:
                        uploaded = upload_pdfs(files, folder, update_progress)
                        progress.progress(1.0)
                        status.success(
                            f"Envoi termine : {len(files)} catalogue(s), {len(uploaded)} partie(s)."
                        )
                        with st.expander("Voir les parties creees"):
                            for part in uploaded:
                                st.write(
                                    f"{part['catalogue']} · partie {part['part']}/{part['total_parts']} · "
                                    f"pages {part['page_start']}-{part['page_end']} · {part['size_mb']} Mo"
                                )
                        st.cache_data.clear()
                        st.session_state.resources = []
                        st.session_state.signature = None
                    except Exception as exc:
                        st.error(f"Echec de l'envoi: {exc}")

    st.divider()
    st.write("✅ Tavily configure" if configured(TAVILY_API_KEY, "tvly-") else "⚪ Tavily non configure")
    st.caption("Mode gratuit sans OpenAI")

need_tab, internal_tab, web_tab, library_tab = st.tabs([
    "🎯 Besoin produit", "📁 Resultats internes", "🌍 Industriels web", "📚 Bibliotheque Cloudinary"
])

with need_tab:
    st.header("Decrire le produit recherche")
    col1, col2 = st.columns([2, 1])
    with col1:
        product_name = st.text_input("Nom du produit", placeholder="Mini Danish assortiment")
        criteria = st.text_area("Criteres complementaires", placeholder="Surgele, assortiment, private label...")
    with col2:
        weight = st.text_input("Grammage cible", placeholder="40 g")
        limit = st.slider("Nombre de resultats", 5, 40, 15, 5)

    image_file = st.file_uploader("Photo du produit", type=["jpg", "jpeg", "png", "webp"])
    reference_sheet = st.file_uploader("Fiche technique d'un produit semblable", type=["pdf"])
    if image_file:
        st.image(image_file, width=320, caption="Produit recherche")

    if st.button("1. Rechercher dans les catalogues Cloudinary", type="primary", width="stretch"):
        if not product_name:
            st.warning("Indique le nom du produit.")
        elif not st.session_state.pages:
            st.error("Aucune page indexee. Synchronise Cloudinary ou verifie les PDF.")
        else:
            reference_text = extract_uploaded_pdf_text(reference_sheet)
            terms = fallback_terms(product_name, weight)
            query = "\n".join(filter(None, [product_name, weight, criteria, reference_text, " ".join(terms)]))
            with st.spinner("Recherche dans la base Cloudinary..."):
                st.session_state.internal_results = search_internal(st.session_state.pages, query, limit)
                st.session_state.product_name = product_name
                st.session_state.terms = terms
                st.session_state.web_results = None
            st.success("Recherche terminee. Consulte Resultats internes.")

with internal_tab:
    st.header("Resultats de la bibliotheque Cloudinary")
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
        for rank, (_, row) in enumerate(results.head(8).iterrows(), start=1):
            with st.expander(f"{rank}. {row['Fournisseur']} · page {row['Page']} · {row['Similarite texte']} %"):
                left, right = st.columns([1, 1.25])
                with left:
                    st.write(f"**Catalogue:** {row['Catalogue']}")
                    st.write(f"**Poids detecte:** {row['Poids']}")
                    st.write(f"**Reference:** {row['Reference']}")
                    st.write(f"**Extrait:** {row['Extrait']}")
                with right:
                    try:
                        image = load_page_image(row["URL"], int(row["Version"]), int(row["Page"]))
                        st.image(image, width="stretch")
                    except Exception as exc:
                        st.warning(f"Apercu indisponible: {exc}")

        st.divider()
        if st.button("2. Rechercher de nouveaux industriels", type="primary", width="stretch"):
            if not configured(TAVILY_API_KEY, "tvly-"):
                st.error("Configure TAVILY_API_KEY dans les Secrets Streamlit.")
            else:
                try:
                    with st.spinner("Recherche des fabricants industriels..."):
                        st.session_state.web_results = search_industrials(st.session_state.product_name, st.session_state.terms)
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
    st.header("Bibliotheque Cloudinary")
    if st.session_state.stats:
        st.dataframe(pd.DataFrame(st.session_state.stats), width="stretch", hide_index=True)
    else:
        st.info("Aucun catalogue synchronise.")
    st.caption("OCR requis signifie que le PDF ne contient pas de texte directement exploitable.")

st.divider()
st.caption(
    "Assistant de sourcing pour l'acheteur tiers Delifrance. "
    "Verifier les preuves, capacites, certifications, MOQ et conditions avant tout contact fournisseur."
)
