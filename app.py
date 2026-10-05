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


def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9\s/.,'-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_supplier(filename, text):
    searchable = normalize_text(filename + " " + text[:5000])
    if "ARGRU" in searchable:
        return "ARGRU"
    stem = Path(filename).stem
    return re.split(r"[\s_-]+", stem)[0].upper() or "INCONNU"


def extract_weight(text):
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(KG|G)\b", normalize_text(text))
    if not match:
        return "Non communiqué"
    value = match.group(1).replace(",", ".")
    return f"{value} {match.group(2).lower()}"


def candidate_product_lines(page_text):
    lines = [re.sub(r"\s+", " ", x).strip() for x in page_text.splitlines()]
    return [x for x in lines if len(x) >= 5]


def parse_product_line(line, supplier, page, filename):
    patterns = [
        re.compile(r"^(?P<name>.+?)\s+(?P<ref>\d{5,8})\s+(?P<p1>\d+)\s+(?P<p2>\d+)$"),
        re.compile(r"^(?P<name>.+?)\s+(?P<ref>\d{5,8})\s+(?P<p1>\d+)$"),
    ]
    for pattern in patterns:
        match = pattern.match(line)
        if match:
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
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    all_text = []
    for index in range(doc.page_count):
        text = doc.load_page(index).get_text("text")
        pages.append({"page": index + 1, "text": text})
        all_text.append(text)
    supplier = detect_supplier(filename, "\n".join(all_text))
    products = []
    for page in pages:
        for line in candidate_product_lines(page["text"]):
            product = parse_product_line(line, supplier, page["page"], filename)
            if product:
                products.append(product)
    doc.close()
    return {
        "supplier": supplier,
        "filename": filename,
        "page_count": len(pages),
        "pages": pages,
        "products": products,
    }


def similarity(query, product, requested_weight=""):
    q = normalize_text(query)
    p = normalize_text(product)
    score = 0.65 * fuzz.token_set_ratio(q, p) + 0.35 * fuzz.partial_ratio(q, p)
    q_weight = re.search(r"(\d+(?:[.,]\d+)?)", requested_weight or q)
    p_weight = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:G|KG)", p)
    if q_weight and p_weight:
        diff = abs(float(q_weight.group(1).replace(",", ".")) - float(p_weight.group(1).replace(",", ".")))
        if diff == 0:
            score += 15
        elif diff <= 5:
            score += 10
        elif diff <= 10:
            score += 5
    return round(min(score, 100), 1)


def render_page(pdf_bytes, page_number):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc.load_page(page_number - 1).get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    doc.close()
    return image


if "analyses" not in st.session_state:
    st.session_state.analyses = []
if "pdf_files" not in st.session_state:
    st.session_state.pdf_files = {}

st.title("🔎 AI Sourcing")
st.caption("Recherche de produits dans les catalogues fournisseurs")

with st.sidebar:
    st.header("📁 Catalogues")
    uploads = st.file_uploader(
        "Ajouter un ou plusieurs PDF",
        type=["pdf"],
        accept_multiple_files=True,
    )
    if st.button("Analyser les catalogues", type="primary", use_container_width=True):
        if not uploads:
            st.warning("Ajoute au moins un PDF.")
        else:
            analyses = []
            files = {}
            progress = st.progress(0)
            for index, uploaded in enumerate(uploads, start=1):
                raw = uploaded.getvalue()
                analyses.append(analyze_pdf(raw, uploaded.name))
                files[uploaded.name] = raw
                progress.progress(index / len(uploads))
            st.session_state.analyses = analyses
            st.session_state.pdf_files = files
            st.success(f"{len(uploads)} catalogue(s) analysé(s).")

    if st.session_state.analyses:
        st.divider()
        for analysis in st.session_state.analyses:
            st.write(f"**{analysis['supplier']}**")
            st.caption(
                f"{analysis['filename']} · {analysis['page_count']} pages · "
                f"{len(analysis['products'])} produits détectés"
            )

    if st.button("Vider la session", use_container_width=True):
        st.session_state.analyses = []
        st.session_state.pdf_files = {}
        st.rerun()

search_tab, base_tab, diagnostic_tab = st.tabs(["🔎 Recherche", "📚 Base produits", "🛠 Diagnostic"])

with search_tab:
    st.header("Rechercher un produit")
    query = st.text_input("Produit recherché", placeholder="Exemple : mini bretzel 40 g")
    weight = st.text_input("Grammage cible, facultatif", placeholder="Exemple : 40 g")
    limit = st.slider("Nombre de résultats", 5, 50, 15, 5)
    image = st.file_uploader("Photo de référence, facultative", type=["png", "jpg", "jpeg", "webp"])
    sheet = st.file_uploader("Fiche technique similaire, facultative", type=["pdf"], key="sheet")
    if image:
        st.image(image, caption="Photo de référence", width=300)
    if sheet:
        st.info("La fiche est chargée. Son analyse IA sera ajoutée dans une prochaine étape.")

    if st.button("Lancer la recherche", type="primary", use_container_width=True):
        if not query:
            st.warning("Indique le produit recherché.")
        elif not st.session_state.analyses:
            st.warning("Ajoute et analyse d'abord au moins un catalogue.")
        else:
            rows = []
            for analysis in st.session_state.analyses:
                for product in analysis["products"]:
                    row = dict(product)
                    row["Similarité"] = similarity(query, product["Produit"], weight)
                    rows.append(row)
            if not rows:
                st.error("Aucun produit structuré détecté. Consulte l'onglet Diagnostic.")
            else:
                results = pd.DataFrame(rows).sort_values("Similarité", ascending=False).head(limit)
                st.dataframe(
                    results[["Fournisseur", "Produit", "Poids", "Référence", "Similarité", "Page", "Catalogue"]],
                    use_container_width=True,
                    hide_index=True,
                )
                st.download_button(
                    "Télécharger les résultats CSV",
                    results.to_csv(index=False).encode("utf-8-sig"),
                    "resultats_sourcing.csv",
                    "text/csv",
                )
                st.subheader("Aperçu des meilleurs résultats")
                for rank, (_, row) in enumerate(results.head(5).iterrows(), start=1):
                    with st.expander(f"{rank}. {row['Fournisseur']} | {row['Produit']} | {row['Similarité']} %"):
                        left, right = st.columns([1, 1.4])
                        with left:
                            st.write(f"**Référence :** {row['Référence']}")
                            st.write(f"**Poids :** {row['Poids']}")
                            st.write(f"**Page :** {row['Page']}")
                            st.write(f"**Catalogue :** {row['Catalogue']}")
                        with right:
                            raw = st.session_state.pdf_files.get(row["Catalogue"])
                            if raw:
                                st.image(render_page(raw, int(row["Page"])), use_container_width=True)

with base_tab:
    st.header("Produits extraits")
    products = [p for a in st.session_state.analyses for p in a["products"]]
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
    st.header("Diagnostic PDF")
    if not st.session_state.analyses:
        st.info("Analyse un catalogue pour afficher le texte extrait.")
    else:
        names = [a["filename"] for a in st.session_state.analyses]
        selected_name = st.selectbox("Catalogue", names)
        selected = next(a for a in st.session_state.analyses if a["filename"] == selected_name)
        page_number = st.number_input("Page", 1, selected["page_count"], 1)
        page = selected["pages"][int(page_number) - 1]
        st.text_area("Texte extrait", page["text"], height=350)
        raw = st.session_state.pdf_files.get(selected_name)
        if raw:
            st.image(render_page(raw, int(page_number)), use_container_width=True)

st.divider()
st.caption("Prototype de sourcing. Vérifie toujours les informations avant de contacter un fournisseur.")
