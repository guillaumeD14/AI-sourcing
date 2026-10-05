import io
import re
import unicodedata
from pathlib import Path

import fitz
import pandas as pd
import streamlit as st
from PIL import Image
from rapidfuzz import fuzz

st.set_page_config(page_title="AI Sourcing", page_icon="🔎", layout="wide")
CATALOGUE_DIR = Path("catalogues")


def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Z0-9\s/.,'()-]", " ", text.upper())
    return re.sub(r"\s+", " ", text).strip()


def detect_supplier(filename, text):
    stem = Path(filename).stem
    first_part = re.split(r"[\s_-]+", stem)[0]
    return first_part.upper() if first_part else "INCONNU"


def extract_weight(text):
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(KG|G)\b", normalize_text(text))
    if not match:
        return "Non communiqué"
    return f"{match.group(1).replace(',', '.')} {match.group(2).lower()}"


def parse_product_line(line, supplier, page, filename):
    line = re.sub(r"\s+", " ", line).strip()
    patterns = [
        re.compile(r"^(?P<name>.+?)\s+(?P<ref>\d{5,10})\s+(?P<p1>\d+)\s+(?P<p2>\d+)$"),
        re.compile(r"^(?P<name>.+?)\s+(?P<ref>\d{5,10})\s+(?P<p1>\d+)$"),
    ]
    for pattern in patterns:
        match = pattern.match(line)
        if not match:
            continue
        name = match.group("name").strip(" -")
        if len(name) < 3 or not re.search(r"[A-Za-zÀ-ÿ]", name):
            return None
        return {
            "Fournisseur": supplier,
            "Produit": name,
            "Poids": extract_weight(name),
            "Référence": match.group("ref"),
            "Conditionnement 1": match.groupdict().get("p1", ""),
            "Conditionnement 2": match.groupdict().get("p2", ""),
            "Page": page,
            "Catalogue": filename,
            "Texte source": line,
        }
    return None


@st.cache_data(show_spinner=False)
def analyze_pdf(pdf_bytes, filename):
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    all_text = []
    for index in range(document.page_count):
        text = document.load_page(index).get_text("text")
        pages.append({"page": index + 1, "text": text})
        all_text.append(text)
    supplier = detect_supplier(filename, "\n".join(all_text))
    products = []
    for page in pages:
        for raw_line in page["text"].splitlines():
            product = parse_product_line(raw_line, supplier, page["page"], filename)
            if product:
                products.append(product)
    document.close()
    return {
        "supplier": supplier,
        "filename": filename,
        "page_count": len(pages),
        "pages": pages,
        "products": products,
    }


def catalogue_signature():
    if not CATALOGUE_DIR.exists():
        return tuple()
    return tuple(
        (str(path.relative_to(CATALOGUE_DIR)), path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(CATALOGUE_DIR.rglob("*.pdf"))
    )


@st.cache_data(show_spinner=False)
def load_catalogue_library(signature):
    analyses = []
    files = {}
    for relative_name, _size, _modified in signature:
        path = CATALOGUE_DIR / relative_name
        raw = path.read_bytes()
        analyses.append(analyze_pdf(raw, relative_name))
        files[relative_name] = raw
    return analyses, files


def calculate_similarity(query, product, requested_weight=""):
    normalized_query = normalize_text(query)
    normalized_product = normalize_text(product)
    score = 0.65 * fuzz.token_set_ratio(normalized_query, normalized_product)
    score += 0.35 * fuzz.partial_ratio(normalized_query, normalized_product)
    query_weight = re.search(r"(\d+(?:[.,]\d+)?)", requested_weight or normalized_query)
    product_weight = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:G|KG)", normalized_product)
    if query_weight and product_weight:
        difference = abs(
            float(query_weight.group(1).replace(",", "."))
            - float(product_weight.group(1).replace(",", "."))
        )
        if difference == 0:
            score += 15
        elif difference <= 5:
            score += 10
        elif difference <= 10:
            score += 5
    return round(min(score, 100), 1)


def render_pdf_page(pdf_bytes, page_number):
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = document.load_page(page_number - 1)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    document.close()
    return image


if "analyses" not in st.session_state:
    st.session_state.analyses = []
if "pdf_files" not in st.session_state:
    st.session_state.pdf_files = {}
if "loaded_signature" not in st.session_state:
    st.session_state.loaded_signature = None

signature = catalogue_signature()

st.title("🔎 AI Sourcing")
st.caption("Base de travail alimentée automatiquement par la bibliothèque de catalogues PDF")

with st.sidebar:
    st.header("📁 Bibliothèque catalogues")
    st.metric("Catalogues disponibles", len(signature))

    if not signature:
        st.warning("Aucun PDF trouvé dans le dossier catalogues/ du dépôt GitHub.")
    else:
        auto_load = st.checkbox("Chargement automatique", value=True)
        if st.button("Recharger la bibliothèque", type="primary", use_container_width=True):
            st.cache_data.clear()
            st.session_state.analyses = []
            st.session_state.pdf_files = {}
            st.session_state.loaded_signature = None
            st.rerun()

        if auto_load and st.session_state.loaded_signature != signature:
            with st.spinner("Analyse des catalogues en cours..."):
                analyses, files = load_catalogue_library(signature)
                st.session_state.analyses = analyses
                st.session_state.pdf_files = files
                st.session_state.loaded_signature = signature
            st.success(f"{len(analyses)} catalogue(s) chargé(s).")

    if st.session_state.analyses:
        st.divider()
        total_products = sum(len(item["products"]) for item in st.session_state.analyses)
        st.metric("Produits détectés", total_products)
        for analysis in st.session_state.analyses:
            st.write(f"**{analysis['supplier']}**")
            st.caption(
                f"{analysis['filename']} · {analysis['page_count']} pages · "
                f"{len(analysis['products'])} produits"
            )

search_tab, base_tab, diagnostic_tab = st.tabs(["🔎 Recherche", "📚 Base produits", "🛠 Diagnostic"])

with search_tab:
    st.header("Rechercher un produit")
    query = st.text_input("Produit recherché", placeholder="Exemple : mini bretzel 40 g")
    weight = st.text_input("Grammage cible, facultatif", placeholder="Exemple : 40 g")
    limit = st.slider("Nombre de résultats", 5, 50, 15, 5)
    reference_image = st.file_uploader(
        "Photo de référence, facultative",
        type=["png", "jpg", "jpeg", "webp"],
    )
    reference_sheet = st.file_uploader(
        "Fiche technique similaire, facultative",
        type=["pdf"],
        key="reference_sheet",
    )
    if reference_image:
        st.image(reference_image, caption="Photo de référence", width=300)
    if reference_sheet:
        st.info("La fiche technique est chargée. Son analyse avancée sera ajoutée dans une prochaine étape.")

    if st.button("Lancer la recherche", type="primary", use_container_width=True):
        if not query:
            st.warning("Indique le produit recherché.")
        elif not st.session_state.analyses:
            st.warning("La bibliothèque de catalogues n'est pas chargée. Vérifie le dossier catalogues/.")
        else:
            rows = []
            for analysis in st.session_state.analyses:
                for product in analysis["products"]:
                    row = dict(product)
                    row["Similarité"] = calculate_similarity(query, product["Produit"], weight)
                    rows.append(row)
            if not rows:
                st.error("Aucun produit structuré n'a été détecté dans les catalogues. Consulte l'onglet Diagnostic.")
            else:
                results = pd.DataFrame(rows).sort_values("Similarité", ascending=False).head(limit)
                visible_columns = [
                    "Fournisseur", "Produit", "Poids", "Référence",
                    "Similarité", "Page", "Catalogue",
                ]
                st.dataframe(results[visible_columns], use_container_width=True, hide_index=True)
                st.download_button(
                    "Télécharger les résultats CSV",
                    results.to_csv(index=False).encode("utf-8-sig"),
                    "resultats_sourcing.csv",
                    "text/csv",
                )
                st.subheader("Aperçu des meilleurs résultats")
                for rank, (_, row) in enumerate(results.head(5).iterrows(), start=1):
                    title = f"{rank}. {row['Fournisseur']} | {row['Produit']} | {row['Similarité']} %"
                    with st.expander(title):
                        left, right = st.columns([1, 1.4])
                        with left:
                            st.write(f"**Référence :** {row['Référence']}")
                            st.write(f"**Poids :** {row['Poids']}")
                            st.write(f"**Page :** {row['Page']}")
                            st.write(f"**Catalogue :** {row['Catalogue']}")
                        with right:
                            raw = st.session_state.pdf_files.get(row["Catalogue"])
                            if raw:
                                st.image(render_pdf_page(raw, int(row["Page"])), use_container_width=True)

with base_tab:
    st.header("Produits extraits de la bibliothèque")
    products = [product for analysis in st.session_state.analyses for product in analysis["products"]]
    if not products:
        st.info("Aucun produit extrait pour le moment.")
    else:
        frame = pd.DataFrame(products)
        st.metric("Produits détectés", len(frame))
        st.dataframe(frame, use_container_width=True, hide_index=True)
        st.download_button(
            "Exporter la base CSV",
            frame.to_csv(index=False).encode("utf-8-sig"),
            "base_catalogues.csv",
            "text/csv",
        )

with diagnostic_tab:
    st.header("Diagnostic d'extraction PDF")
    if not st.session_state.analyses:
        st.info("La bibliothèque n'est pas encore chargée.")
    else:
        names = [item["filename"] for item in st.session_state.analyses]
        selected_name = st.selectbox("Catalogue", names)
        selected = next(item for item in st.session_state.analyses if item["filename"] == selected_name)
        page_number = st.number_input("Page", 1, selected["page_count"], 1)
        page = selected["pages"][int(page_number) - 1]
        st.text_area("Texte extrait", page["text"], height=350)
        raw = st.session_state.pdf_files.get(selected_name)
        if raw:
            st.image(render_pdf_page(raw, int(page_number)), use_container_width=True)

st.divider()
st.caption(
    "Les PDF sont lus depuis le dossier catalogues/. "
    "Les résultats doivent être vérifiés avant tout contact fournisseur."
)
