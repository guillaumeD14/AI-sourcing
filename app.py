import io
import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
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
TARGET_PART_BYTES = 7_500_000
MAX_PART_BYTES = 9_000_000
MAX_PARALLEL_UPLOADS = 3


def secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


CLOUD_NAME = secret("CLOUDINARY_CLOUD_NAME")
API_KEY = secret("CLOUDINARY_API_KEY")
API_SECRET = secret("CLOUDINARY_API_SECRET")
CLOUD_PREFIX = secret("CLOUDINARY_PREFIX", "ai-sourcing/catalogues").strip("/")
TAVILY_KEY = secret("TAVILY_API_KEY")


def configured(value, prefix=None):
    if not value or value.lower().startswith(("votre-", "your-", "colle-")):
        return False
    return not prefix or value.startswith(prefix)


def cloudinary_ready():
    return all(map(configured, [CLOUD_NAME, API_KEY, API_SECRET]))


@st.cache_resource
def init_cloudinary(cloud_name, api_key, api_secret):
    cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret, secure=True)
    return True


def ensure_cloudinary():
    if not cloudinary_ready():
        raise RuntimeError("Configuration Cloudinary incomplete dans les Secrets Streamlit.")
    init_cloudinary(CLOUD_NAME, API_KEY, API_SECRET)


def normalize(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\s/.,'()+-]", " ", text)).strip()


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def supplier_name(public_id):
    name = Path(PurePosixPath(public_id).name).stem
    name = re.sub(r"_part_\d+_pages_\d+-\d+$", "", name)
    parts = [p for p in re.split(r"[\s_-]+", name) if p]
    return parts[0].upper() if parts else "INCONNU"


def original_page_offset(public_id):
    match = re.search(r"_pages_(\d+)-(\d+)$", Path(public_id).stem)
    return int(match.group(1)) - 1 if match else 0


def extract_weight(text):
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(KG|G)\b", normalize(text))
    return f"{match.group(1).replace(',', '.')} {match.group(2).lower()}" if match else "Non communique"


def extract_reference(text):
    value = normalize(text)
    match = re.search(r"\bREF(?:ERENCE)?\s*[:#-]?\s*([A-Z0-9-]{4,16})\b", value)
    if not match:
        match = re.search(r"\b(\d{6,10})\b", value)
    return match.group(1) if match else "Non trouvee"


def pdf_range_bytes(source, start, end):
    output = fitz.open()
    output.insert_pdf(source, from_page=start, to_page=end)
    data = output.tobytes(garbage=4, deflate=True, clean=True)
    output.close()
    return data


def rasterize_page(source_page):
    for zoom, quality in ((1.0, 58), (0.8, 48), (0.65, 38)):
        pix = source_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        jpeg = pix.tobytes("jpeg", jpg_quality=quality)
        output = fitz.open()
        page = output.new_page(width=source_page.rect.width, height=source_page.rect.height)
        page.insert_image(page.rect, stream=jpeg)
        data = output.tobytes(garbage=4, deflate=True, clean=True)
        output.close()
        if len(data) <= MAX_PART_BYTES:
            return data
    raise ValueError("Une page reste trop lourde apres compression.")


def split_range(source, start, end):
    data = pdf_range_bytes(source, start, end)
    if len(data) <= MAX_PART_BYTES:
        return [{"bytes": data, "start": start + 1, "end": end + 1}]
    if start == end:
        return [{"bytes": rasterize_page(source.load_page(start)), "start": start + 1, "end": end + 1}]
    middle = (start + end) // 2
    return split_range(source, start, middle) + split_range(source, middle + 1, end)


def fast_split_pdf(pdf_bytes):
    source = fitz.open(stream=pdf_bytes, filetype="pdf")
    optimized = pdf_range_bytes(source, 0, source.page_count - 1)
    if len(optimized) <= MAX_PART_BYTES:
        result = [{"bytes": optimized, "start": 1, "end": source.page_count}]
        source.close()
        return result

    estimated_parts = max(2, math.ceil(len(optimized) / TARGET_PART_BYTES))
    pages_per_part = max(1, math.ceil(source.page_count / estimated_parts))
    parts = []
    for start in range(0, source.page_count, pages_per_part):
        end = min(source.page_count - 1, start + pages_per_part - 1)
        parts.extend(split_range(source, start, end))
    source.close()
    return parts


def upload_one_part(target_folder, stem, part_number, total_parts, part):
    public_id = (
        f"{target_folder}/{stem}_part_{part_number:02d}_"
        f"pages_{part['start']}-{part['end']}.pdf"
    )
    result = cloudinary.uploader.upload(
        io.BytesIO(part["bytes"]),
        resource_type="raw",
        type="upload",
        public_id=public_id,
        overwrite=True,
        invalidate=True,
        tags=["ai-sourcing", "catalogue-fournisseur", stem],
        context={
            "part": f"{part_number}/{total_parts}",
            "original_pages": f"{part['start']}-{part['end']}",
        },
    )
    return {
        "public_id": result.get("public_id", public_id),
        "part": part_number,
        "total": total_parts,
        "pages": f"{part['start']}-{part['end']}",
        "size_mb": round(len(part["bytes"]) / 1024 / 1024, 2),
    }


def upload_catalogue(uploaded_file, folder, progress):
    ensure_cloudinary()
    safe_folder = re.sub(r"[^A-Za-z0-9/_-]", "_", folder.strip("/"))
    target = CLOUD_PREFIX + ("/" + safe_folder if safe_folder else "")
    stem = safe_name(Path(uploaded_file.name).stem)

    progress(0.05, "Optimisation et decoupage du PDF...")
    parts = fast_split_pdf(uploaded_file.getvalue())
    total = len(parts)
    progress(0.20, f"{total} partie(s) creee(s). Envoi parallele...")

    uploaded = []
    errors = []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_UPLOADS, total)) as pool:
        futures = {
            pool.submit(upload_one_part, target, stem, i, total, part): i
            for i, part in enumerate(parts, start=1)
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                uploaded.append(future.result())
            except Exception as exc:
                errors.append(f"Partie {futures[future]}: {exc}")
            progress(0.20 + 0.80 * completed / total, f"Envoi {completed}/{total}")

    if errors:
        raise RuntimeError(" | ".join(errors))
    return sorted(uploaded, key=lambda item: item["part"])


def list_pdfs():
    ensure_cloudinary()
    resources, cursor = [], None
    while True:
        kwargs = {
            "resource_type": "raw", "type": "upload",
            "prefix": CLOUD_PREFIX + "/", "max_results": 500,
        }
        if cursor:
            kwargs["next_cursor"] = cursor
        response = cloudinary.api.resources(**kwargs)
        for item in response.get("resources", []):
            pid = item.get("public_id", "")
            if pid.lower().endswith(".pdf") or str(item.get("format", "")).lower() == "pdf":
                resources.append({
                    "public_id": pid,
                    "url": item.get("secure_url", ""),
                    "bytes": int(item.get("bytes", 0)),
                    "version": int(item.get("version", 0)),
                })
        cursor = response.get("next_cursor")
        if not cursor:
            break
    return sorted(resources, key=lambda item: item["public_id"].lower())


def signature(resources):
    return tuple((r["public_id"], r["bytes"], r["version"], r["url"]) for r in resources)


def download(url):
    response = requests.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.content


@st.cache_data(show_spinner=False, ttl=3600, max_entries=1)
def build_index(library_signature):
    pages, stats = [], []
    for public_id, size, version, url in library_signature:
        count, indexed, status = 0, 0, "OK"
        try:
            document = fitz.open(stream=download(url), filetype="pdf")
            count = document.page_count
            offset = original_page_offset(public_id)
            for i in range(count):
                text = re.sub(r"\s+", " ", document.load_page(i).get_text("text")).strip()
                if not text:
                    continue
                pages.append({
                    "Fournisseur": supplier_name(public_id),
                    "Catalogue": public_id,
                    "URL": url,
                    "Version": version,
                    "Page partie": i + 1,
                    "Page originale": offset + i + 1,
                    "Texte": text,
                    "Poids": extract_weight(text),
                    "Reference": extract_reference(text),
                })
                indexed += 1
            document.close()
            if indexed == 0:
                status = "OCR requis"
        except Exception as exc:
            status = f"Erreur: {str(exc)[:120]}"
        stats.append({
            "Fournisseur": supplier_name(public_id), "Catalogue": public_id,
            "Taille Mo": round(size / 1024 / 1024, 2), "Pages": count,
            "Pages indexees": indexed, "Statut": status,
        })
    return pages, stats


@st.cache_data(show_spinner=False, ttl=1800, max_entries=4)
def page_image(url, version, page_number):
    document = fitz.open(stream=download(url), filetype="pdf")
    pix = document.load_page(page_number - 1).get_pixmap(matrix=fitz.Matrix(1.1, 1.1), alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    document.close()
    return image


def search_internal(pages, query, limit):
    documents = [normalize(row["Texte"]) for row in pages]
    normalized_query = normalize(query)
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, max_features=30000)
    matrix = vectorizer.fit_transform(documents + [normalized_query])
    semantic = cosine_similarity(matrix[-1], matrix[:-1]).flatten()
    rows = []
    for index, item in enumerate(pages):
        fuzzy_score = fuzz.token_set_ratio(normalized_query, documents[index]) / 100
        row = dict(item)
        row["Similarite"] = round(100 * (0.72 * semantic[index] + 0.28 * fuzzy_score), 1)
        row["Extrait"] = item["Texte"][:650]
        rows.append(row)
    return pd.DataFrame(rows).sort_values("Similarite", ascending=False).head(limit)


def tavily_search(query):
    if not configured(TAVILY_KEY, "tvly-"):
        raise RuntimeError("TAVILY_API_KEY absente ou invalide.")
    response = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {TAVILY_KEY}", "Content-Type": "application/json"},
        json={"query": query, "search_depth": "basic", "max_results": 8, "include_answer": False},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def supplier_benchmarks(product):
    value = normalize(product)
    if "DANISH" not in value and "VIENNOISERIE" not in value:
        return []
    return [
        {"Entreprise": "La Lorraine Bakery Group", "Pays": "Belgique", "Statut": "Fabricant confirme", "Produit": "Mini Danish Mix 40 g", "URL": "https://llbg.com/en-EU/products/5000929-mini-danish-mix", "Pertinence": 100},
        {"Entreprise": "Kohberg", "Pays": "Danemark", "Statut": "Fabricant confirme", "Produit": "Mini Danish Mix 40 g", "URL": "https://www.kohberg.com/our-products/product-overview/danish-pastries/mini-danish-pastries-mix-5-variants/", "Pertinence": 99},
        {"Entreprise": "Gourmand Pastries", "Pays": "Belgique", "Statut": "Fabricant confirme", "Produit": "Mix Mini Danish 42 g", "URL": "https://gourmandpastries.com/en-int/c/bake-off-pastries-en-int/bake-off-danish-pastries-en-int/p/mix-mini-danish-42g/", "Pertinence": 98},
        {"Entreprise": "Lantmannen Unibake", "Pays": "Europe", "Statut": "Fabricant confirme", "Produit": "Mini Danish Selection", "URL": "https://www.lantmannenunibake.com/all-products/mini-danish-selection3/", "Pertinence": 96},
        {"Entreprise": "Europastry", "Pays": "Espagne", "Statut": "Fabricant confirme", "Produit": "Gamme Mini Danish", "URL": "https://europastry.com/fr/fr/product/mini-danish-pomme-creme/", "Pertinence": 90},
    ]


def industrial_search(product):
    results = supplier_benchmarks(product)
    seen = {urlparse(item["URL"]).netloc for item in results}
    for query in [
        f'"{product}" manufacturer Europe frozen bakery',
        f'{product} industrial producer private label foodservice',
    ]:
        for item in tavily_search(query):
            url = item.get("url", "")
            domain = urlparse(url).netloc.replace("www.", "")
            content = f"{item.get('title', '')} {item.get('content', '')}".lower()
            if not url or domain in seen or not any(x in content for x in ("manufacturer", "producer", "factory", "private label", "industrial")):
                continue
            seen.add(domain)
            results.append({
                "Entreprise": domain, "Pays": "A confirmer", "Statut": "Fabricant probable",
                "Produit": item.get("title", "Produit a verifier"), "URL": url, "Pertinence": 60,
            })
    return sorted(results, key=lambda row: row["Pertinence"], reverse=True)


for key, default in {
    "resources": [], "signature": None, "pages": [], "stats": [],
    "results": None, "product": "", "web_results": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

st.title("🔎 AI Sourcing Delifrance")
st.caption("Bibliotheque Cloudinary · Recherche catalogues · Sourcing industriel")

with st.sidebar:
    st.header("☁️ Cloudinary")
    if not cloudinary_ready():
        st.error("Cloudinary non configure dans les Secrets Streamlit.")
    else:
        if st.button("Synchroniser", type="primary", width="stretch") or not st.session_state.resources:
            try:
                resources = list_pdfs()
                new_signature = signature(resources)
                st.session_state.resources = resources
                if new_signature != st.session_state.signature:
                    with st.spinner("Indexation des catalogues..."):
                        st.session_state.pages, st.session_state.stats = build_index(new_signature)
                        st.session_state.signature = new_signature
                        st.session_state.results = None
                st.success("Bibliotheque synchronisee")
            except Exception as exc:
                st.error(f"Erreur Cloudinary: {exc}")

        st.metric("PDF", len(st.session_state.resources))
        st.metric("Pages indexees", len(st.session_state.pages))

        with st.expander("Ajouter un gros catalogue"):
            folder = st.text_input("Sous-dossier", placeholder="viennoiseries/fournisseur")
            uploaded_file = st.file_uploader("PDF", type=["pdf"], accept_multiple_files=False)
            st.caption("Compression, decoupage sous 9 Mo et envoi parallele automatique.")
            if st.button("Optimiser et envoyer", width="stretch"):
                if not uploaded_file:
                    st.warning("Selectionne un PDF.")
                else:
                    bar = st.progress(0)
                    message = st.empty()

                    def progress(value, text):
                        bar.progress(min(max(value, 0.0), 1.0))
                        message.write(text)

                    try:
                        parts = upload_catalogue(uploaded_file, folder, progress)
                        message.success(f"Envoi termine: {len(parts)} partie(s). Clique sur Synchroniser.")
                        with st.expander("Details"):
                            st.dataframe(pd.DataFrame(parts), width="stretch", hide_index=True)
                        st.cache_data.clear()
                        st.session_state.resources = []
                        st.session_state.signature = None
                    except Exception as exc:
                        st.error(f"Echec: {exc}")

    st.divider()
    st.write("✅ Tavily configure" if configured(TAVILY_KEY, "tvly-") else "⚪ Tavily non configure")

need_tab, result_tab, web_tab, library_tab = st.tabs([
    "🎯 Besoin", "📁 Catalogues", "🌍 Industriels", "📚 Bibliotheque"
])

with need_tab:
    product = st.text_input("Produit recherche", placeholder="Mini Danish assortiment")
    weight = st.text_input("Grammage", placeholder="40 g")
    criteria = st.text_area("Criteres", placeholder="Surgele, assortiment, private label...")
    limit = st.slider("Resultats", 5, 40, 15, 5)
    if st.button("Rechercher dans les catalogues", type="primary", width="stretch"):
        if not product:
            st.warning("Indique le produit.")
        elif not st.session_state.pages:
            st.error("Aucune page indexee.")
        else:
            query = " ".join(filter(None, [product, weight, criteria]))
            st.session_state.results = search_internal(st.session_state.pages, query, limit)
            st.session_state.product = product
            st.session_state.web_results = None
            st.success("Recherche terminee")

with result_tab:
    results = st.session_state.results
    if results is None:
        st.info("Lance une recherche.")
    else:
        st.dataframe(results[["Fournisseur", "Catalogue", "Page originale", "Poids", "Reference", "Similarite"]], width="stretch", hide_index=True)
        for rank, (_, row) in enumerate(results.head(8).iterrows(), 1):
            with st.expander(f"{rank}. {row['Fournisseur']} · page {row['Page originale']} · {row['Similarite']} %"):
                left, right = st.columns([1, 1.2])
                with left:
                    st.write(row["Extrait"])
                with right:
                    try:
                        st.image(page_image(row["URL"], row["Version"], int(row["Page partie"])), width="stretch")
                    except Exception as exc:
                        st.warning(f"Apercu indisponible: {exc}")
        if st.button("Rechercher de nouveaux industriels", type="primary", width="stretch"):
            try:
                st.session_state.web_results = industrial_search(st.session_state.product)
                st.success("Recherche Internet terminee")
            except Exception as exc:
                st.error(f"Recherche impossible: {exc}")

with web_tab:
    items = st.session_state.web_results
    if items is None:
        st.info("Lance la recherche Internet depuis l'onglet Catalogues.")
    else:
        for item in items:
            with st.container(border=True):
                st.subheader(item["Entreprise"])
                st.write(f"**Pays:** {item['Pays']}")
                st.write(f"**Statut:** {item['Statut']}")
                st.write(f"**Produit:** {item['Produit']}")
                st.write(f"**Pertinence:** {item['Pertinence']}")
                st.link_button("Page source", item["URL"], width="stretch")
        st.download_button("Exporter CSV", pd.DataFrame(items).to_csv(index=False).encode("utf-8-sig"), "industriels.csv", "text/csv", width="stretch")

with library_tab:
    if st.session_state.stats:
        st.dataframe(pd.DataFrame(st.session_state.stats), width="stretch", hide_index=True)
    else:
        st.info("Aucun catalogue synchronise.")

st.divider()
st.caption("Outil d'aide au sourcing pour l'acheteur tiers Delifrance. Verifier chaque information avant contact.")
