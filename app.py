import io
import re
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path

import fitz
import pandas as pd
import streamlit as st
from PIL import Image
from rapidfuzz import fuzz


# ==========================================================
# CONFIGURATION
# ==========================================================

st.set_page_config(
    page_title="AI Sourcing",
    page_icon="🔎",
    layout="wide",
)


# ==========================================================
# MODELE DE DONNEES
# ==========================================================

@dataclass
class Product:
    supplier: str
    product_name: str
    weight: str
    reference: str
    packaging_1: str
    packaging_2: str
    page: int
    source_file: str
    source_text: str


# ==========================================================
# FONCTIONS UTILITAIRES
# ==========================================================

def normalize_text(text: str) -> str:
    """
    Normalise le texte pour améliorer la recherche :
    - suppression des accents ;
    - conversion en majuscules ;
    - suppression des caractères inutiles ;
    - réduction des espaces.
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        character
        for character in text
        if not unicodedata.combining(character)
    )

    text = text.upper()
    text = re.sub(r"[^A-Z0-9\s/-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


def detect_supplier(file_name: str, full_text: str) -> str:
    """
    Essaye d'identifier le fournisseur à partir du nom du fichier
    ou du contenu du PDF.
    """
    normalized_text = normalize_text(full_text)

    known_suppliers = {
        "ARGRU": "ARGRU",
    }

    for keyword, supplier_name in known_suppliers.items():
        if keyword in normalized_text:
            return supplier_name

    file_stem = Path(file_name).stem
    first_word = re.split(r"[\s_-]+", file_stem)[0]

    return first_word.upper() if first_word else "FOURNISSEUR INCONNU"


def extract_weight(product_name: str) -> str:
    """
    Extrait le grammage du nom du produit.
    Exemples :
    MINI BRETZEL 40G -> 40 g
    PARIS-BREST 120GX2 -> 120 g x 2
    PAIN DE CAMPAGNE 1KG -> 1 kg
    """
    normalized_name = normalize_text(product_name)

    multiplied_weight = re.search(
        r"\b(\d+(?:[.,]\d+)?)\s*(G|KG)\s*X\s*(\d+)\b",
        normalized_name,
    )

    if multiplied_weight:
        value = multiplied_weight.group(1).replace(",", ".")
        unit = multiplied_weight.group(2).lower()
        quantity = multiplied_weight.group(3)
        return f"{value} {unit} x {quantity}"

    simple_weight = re.search(
        r"\b(\d+(?:[.,]\d+)?)\s*(G|KG)\b",
        normalized_name,
    )

    if simple_weight:
        value = simple_weight.group(1).replace(",", ".")
        unit = simple_weight.group(2).lower()
        return f"{value} {unit}"

    return "Non communiqué"


def parse_product_line(
    line: str,
    supplier: str,
    page_number: int,
    file_name: str,
) -> Product | None:
    """
    Détecte les lignes du type :

    MINI BRETZEL 40G 600400 144 44

    La logique considère les 3 derniers nombres comme :
    - référence ;
    - premier champ de conditionnement ;
    - second champ de conditionnement.
    """
    cleaned_line = re.sub(r"\s+", " ", line).strip()

    pattern = re.compile(
        r"^(?P<name>.+?)\s+"
        r"(?P<reference>\d{5,8})\s+"
        r"(?P<packaging_1>\d+)\s+"
        r"(?P<packaging_2>\d+)$"
    )

    match = pattern.match(cleaned_line)

    if not match:
        return None

    product_name = match.group("name").strip()

    if len(product_name) < 3:
        return None

    return Product(
        supplier=supplier,
        product_name=product_name,
        weight=extract_weight(product_name),
        reference=match.group("reference"),
        packaging_1=match.group("packaging_1"),
        packaging_2=match.group("packaging_2"),
        page=page_number,
        source_file=file_name,
        source_text=cleaned_line,
    )


@st.cache_data(show_spinner=False)
def analyze_pdf(pdf_bytes: bytes, file_name: str) -> dict:
    """
    Lit un PDF et extrait :
    - le texte par page ;
    - les produits détectés ;
    - le fournisseur ;
    - quelques statistiques.
    """
    document = fitz.open(stream=pdf_bytes, filetype="pdf")

    pages = []
    all_text_parts = []

    for page_index in range(document.page_count):
        page = document.load_page(page_index)
        page_text = page.get_text("text")

        pages.append(
            {
                "page": page_index + 1,
                "text": page_text,
            }
        )

        all_text_parts.append(page_text)

    full_text = "\n".join(all_text_parts)
    supplier = detect_supplier(file_name, full_text)

    products = []

    for page_data in pages:
        page_number = page_data["page"]
        page_text = page_data["text"]

        for raw_line in page_text.splitlines():
            product = parse_product_line(
                line=raw_line,
                supplier=supplier,
                page_number=page_number,
                file_name=file_name,
            )

            if product:
                products.append(asdict(product))

    document.close()

    return {
        "supplier": supplier,
        "file_name": file_name,
        "page_count": len(pages),
        "pages": pages,
        "products": products,
        "full_text": full_text,
    }


def build_search_terms(query: str**-> list:
    Produit des termes de recherche simples.
    Cette première version fonctionne sans API IA.
    """
    normalized_query = normalize_text(query)

    synonym_groups = {
        "DANISH": [
            "DANISH",
            "VIENNOISERIE",
            "VIENNOISERIES",
            "STREUSSEL",
            "STREUSEL",
        ],
        "BRETZEL": [
            "BRETZEL",
            "PRETZEL",
        ],
        "BEIGNET": [
            "BEIGNET",
            "DONUT",
            "DOUGHNUT",
        ],
        "PAIN": [
            "PAIN",
            "BAGUETTE",
            "BREAD",
            "PETIT PAIN",
        ],
    }

    terms = [normalized_query]

    for keyword, synonyms in synonym_groups.items():
        if keyword in normalized_query:
            terms.extend(synonyms)

    unique_terms = []

    for term in terms:
        term = normalize_text(term)

        if term and term not in unique_terms:
            unique_terms.append(term)

    return unique_terms


def calculate_similarity(
    query: str,
    product_name: str,
    target_weight: str,
    product_weight: str,
) -> float:
    """
    Calcule un score indicatif à partir du nom du produit,
    des mots communs et de la proximité du grammage.
    """
    normalized_query = normalize_text(query)
    normalized_product = normalize_text(product_name)

    ratio_score = fuzz.token_set_ratio(
        normalized_query,
        normalized_product,
    )

    partial_score = fuzz.partial_ratio(
        normalized_query,
        normalized_product,
    )

    semantic_score = (
        ratio_score * 0.65
        + partial_score * 0.35
    )

    query_terms = build_search_terms(query)
    synonym_bonus = 0

    for term in query_terms:
        if term and term in normalized_product:
            synonym_bonus = max(synonym_bonus, 15)

    weight_bonus = 0

    target_weight_match = re.search(
        r"(\d+(?:[.,]\d+)?)",
        target_weight or "",
    )

    product_weight_match = re.search(
        r"(\d+(?:[.,]\d+)?)",
        product_weight or "",
    )

    if target_weight_match and product_weight_match:
        requested = float(
            target_weight_match.group(1).replace(",", ".")
        )

        found = float(
            product_weight_match.group(1).replace(",", ".")
        )

        difference = abs(requested - found)

        if difference == 0:
            weight_bonus = 15
        elif difference <= 5:
            weight_bonus = 12
        elif difference <= 10:
            weight_bonus = 8
        elif difference <= 20:
            weight_bonus = 3

    final_score = (
        semantic_score
        + synonym_bonus
        + weight_bonus
    )

    return round(min(final_score, 100), 1)


def search_products(
    analyses: list[dict],
    query: str,
    target_weight: str,
) -> pd.DataFrame:
    """
    Recherche dans tous les produits extraits des PDF.
    """
    result_rows = []

    for analysis in analyses:
        for product in analysis["products"]:
            similarity = calculate_similarity(
                query=query,
                product_name=product["product_name"],
                target_weight=target_weight,
                product_weight=product["weight"],
            )

            result_rows.append(
                {
                    "Fournisseur": product["supplier"],
                    "Produit": product["product_name"],
                    "Poids": product["weight"],
                    "Référence": product["reference"],
                    "Conditionnement 1": product["packaging_1"],
                    "Conditionnement 2": product["packaging_2"],
                    "Similarité": similarity,
                    "Page": product["page"],
                    "Catalogue": product["source_file"],
                }
            )

    if not result_rows:
        return pd.DataFrame()

    result_frame = pd.DataFrame(result_rows)

    result_frame = result_frame.sort_values(
        by="Similarité",
        ascending=False,
    ).reset_index(drop=True)

    return result_frame


def render_pdf_page(
    pdf_bytes: bytes,
    page_number: int,
    zoom: float = 1.5,
) -> Image.Image:
    """
    Transforme une page PDF en image pour prévisualisation.
    """
    document = fitz.open(
        stream=pdf_bytes,
        filetype="pdf",
    )

    page = document.load_page(page_number - 1)

    matrix = fitz.Matrix(zoom, zoom)
    pixmap = page.get_pixmap(
        matrix=matrix,
        alpha=False,
    )

    image = Image.open(
        io.BytesIO(pixmap.tobytes("png"))
    )

    document.close()

    return image


# ==========================================================
# ETAT DE SESSION
# ==========================================================

if "catalogue_analyses" not in st.session_state:
    st.session_state.catalogue_analyses = []

if "catalogue_files" not in st.session_state:
    st.session_state.catalogue_files = {}


# ==========================================================
# EN-TETE
# ==========================================================

st.title("🔎 AI Sourcing")

st.caption(
    "Recherche de produits similaires dans les catalogues fournisseurs"
)

st.info(
    """
    Cette première version analyse les catalogues PDF chargés pendant
    la session. Les fichiers ne sont pas ajoutés au dépôt GitHub.
    """
)


# ==========================================================
# BARRE LATERALE : CATALOGUES
# ==========================================================

with st.sidebar:
    st.header("📁 Base catalogues")

    uploaded_catalogues = st.file_uploader(
        "Ajouter des catalogues fournisseurs",
        type=["pdf"],
        accept_multiple_files=True,
    )

    analyze_button = st.button(
        "📥 Analyser les catalogues",
        use_container_width=True,
    )

    if analyze_button:
        if not uploaded_catalogues:
            st.warning("Ajoutez au moins un catalogue PDF.")

        else:
            new_analyses = []
            new_files = {}

            progress_bar = st.progress(0)
            status_placeholder = st.empty()

            total_files = len(uploaded_catalogues)

            for index, uploaded_file in enumerate(
                uploaded_catalogues,
                start=1,
            ):
                status_placeholder.write(
                    f"Analyse de {uploaded_file.name}..."
                )

                pdf_bytes = uploaded_file.getvalue()

                analysis = analyze_pdf(
                    pdf_bytes=pdf_bytes,
                    file_name=uploaded_file.name,
                )

                new_analyses.append(analysis)
                new_files[uploaded_file.name] = pdf_bytes

                progress_bar.progress(index / total_files)

            st.session_state.catalogue_analyses = new_analyses
            st.session_state.catalogue_files = new_files

            status_placeholder.success(
                f"{total_files} catalogue(s) analysé(s)."
            )

    st.divider()

    if st.session_state.catalogue_analyses:
        st.subheader("Catalogues actifs")

        for analysis in st.session_state.catalogue_analyses:
            st.write(
                f"**{analysis['supplier']}**"
            )
            st.caption(
                f"{analysis['file_name']} · "
                f"{analysis['page_count']} pages · "
                f"{len(analysis['products'])} produits détectés"
            )

    if st.button(
        "🗑️ Vider la session",
        use_container_width=True,
    ):
        st.session_state.catalogue_analyses = []
        st.session_state.catalogue_files = {}
        st.rerun()


# ==========================================================
# ONGLET 1 : RECHERCHE
# ==========================================================

search_tab, database_tab, diagnostics_tab = st.tabs(
    [
        "🔎 Recherche",
        "📚 Base produits",
        "🛠️ Diagnostic PDF",
    ]
)


with search_tab:
    st.header("🎯 Rechercher un produit")

    product_query = st.text_input(
        "Produit recherché",
        placeholder="Exemple : mini bretzel 40 g",
    )

    col_1, col_2 = st.columns(2)

    with col_1:
        target_weight = st.text_input(
            "Grammage cible, facultatif",
            placeholder="Exemple : 40 g",
        )

    with col_2:
        maximum_results = st.slider(
            "Nombre de résultats",
            min_value=5,
            max_value=50,
            value=15,
            step=5,
        )

    product_image = st.file_uploader(
        "🖼️ Photo du produit, facultative",
        type=["png", "jpg", "jpeg", "webp"],
        key="search_image",
    )

    reference_sheet = st.file_uploader(
        "📄 Fiche technique similaire, facultative",
        type=["pdf"],
        key="reference_sheet",
    )

    if product_image:
        st.image(
            product_image,
            caption="Photo du produit recherché",
            width=300,
        )

    if reference_sheet:
        st.success(
            f"Fiche de référence chargée : {reference_sheet.name}"
        )
        st.caption(
            "L'analyse technique de cette fiche sera ajoutée "
            "dans la prochaine version."
        )

    launch_search = st.button(
        "🔎 LANCER LA RECHERCHE",
        type="primary",
        use_container_width=True,
    )

    if launch_search:
        if not product_query:
            st.warning("Indiquez le produit recherché.")

        elif not st.session_state.catalogue_analyses:
            st.warning(
                "Ajoutez et analysez au moins un catalogue "
                "dans la barre latérale."
            )

        else:
            result_frame = search_products(
                analyses=st.session_state.catalogue_analyses,
                query=product_query,
                target_weight=target_weight,
            )

            if result_frame.empty:
                st.error(
                    "Aucun produit structuré n'a été détecté "
                    "dans les catalogues."
                )

            else:
                displayed_results = (
                    result_frame
                    .head(maximum_results)
                    .copy()
                )

                displayed_results["Similarité"] = (
                    displayed_results["Similarité"]
                    .map(lambda value: f"{value:.1f} %")
                )

                st.subheader(
                    f"Résultats pour « {product_query} »"
                )

                st.dataframe(
                    displayed_results,
                    use_container_width=True,
                    hide_index=True,
                )

                st.download_button(
                    "⬇️ Télécharger les résultats en CSV",
                    displayed_results.to_csv(
                        index=False,
                    ).encode("utf-8-sig"),
                    file_name="resultats_sourcing.csv",
                    mime="text/csv",
                )

                st.subheader("Aperçu des meilleurs résultats")

                raw_top_results = (
                    result_frame
                    .head(min(5, maximum_results))
                )

                for row_index, row in raw_top_results.iterrows():
                    with st.expander(
                        f"{row_index + 1}. "
                        f"{row['Fournisseur']} · "
                        f"{row['Produit']} · "
                        f"{row['Similarité']:.1f} %"
                    ):
                        detail_col, preview_col = st.columns(
                            [1, 1.4]
                        )

                        with detail_col:
                            st.write(
                                f"**Référence :** {row['Référence']}"
                            )
                            st.write(
                                f"**Poids :** {row['Poids']}"
                            )
                            st.write(
                                f"**Page :** {row['Page']}"
                            )
                            st.write(
                                f"**Catalogue :** {row['Catalogue']}"
                            )
                            st.write(
                                "**Source :** base interne"
                            )

                        with preview_col:
                            pdf_bytes = (
                                st.session_state.catalogue_files
                                .get(row["Catalogue"])
                            )

                            if pdf_bytes:
                                page_image = render_pdf_page(
                                    pdf_bytes=pdf_bytes,
                                    page_number=int(row["Page"]),
                                )

                                st.image(
                                    page_image,
                                    caption=(
                                        f"Page {row['Page']} du catalogue"
                                    ),
                                    use_container_width=True,
                                )


# ==========================================================
# ONGLET 2 : BASE PRODUITS
# ==========================================================

with database_tab:
    st.header("📚 Produits extraits")

    if not st.session_state.catalogue_analyses:
        st.info(
            "Ajoutez et analysez un catalogue pour afficher la base."
        )

    else:
        all_products = []

        for analysis in st.session_state.catalogue_analyses:
            all_products.extend(analysis["products"])

        products_frame = pd.DataFrame(all_products)

        if products_frame.empty:
            st.warning(
                "Aucune ligne produit structurée n'a été détectée."
            )

        else:
            display_columns = {
                "supplier": "Fournisseur",
                "product_name": "Produit",
                "weight": "Poids",
                "reference": "Référence",
                "packaging_1": "Conditionnement 1",
                "packaging_2": "Conditionnement 2",
                "page": "Page",
                "source_file": "Catalogue",
            }

            displayed_products = (
                products_frame[
                    list(display_columns.keys())
                ]
                .rename(columns=display_columns)
            )

            st.metric(
                "Produits détectés",
                len(displayed_products),
            )

            st.dataframe(
                displayed_products,
                use_container_width=True,
                hide_index=True,
            )

            st.download_button(
                "⬇️ Exporter la base en CSV",
                displayed_products.to_csv(
                    index=False,
                ).encode("utf-8-sig"),
                file_name="base_produits_catalogues.csv",
                mime="text/csv",
            )


# ==========================================================
# ONGLET 3 : DIAGNOSTIC
# ==========================================================

with diagnostics_tab:
    st.header("🛠️ Diagnostic d'extraction")

    if not st.session_state.catalogue_analyses:
        st.info(
            "Ajoutez et analysez un catalogue pour voir "
            "le texte extrait."
        )

    else:
        diagnostic_catalogue = st.selectbox(
            "Catalogue",
            options=[
                analysis["file_name"]
                for analysis
                in st.session_state.catalogue_analyses
            ],
        )

        selected_analysis = next(
            analysis
            for analysis in st.session_state.catalogue_analyses
            if analysis["file_name"] == diagnostic_catalogue
        )

        diagnostic_page = st.number_input(
            "Page",
            min_value=1,
            max_value=selected_analysis["page_count"],
            value=1,
            step=1,
        )

        page_data = selected_analysis["pages"][
            int(diagnostic_page) - 1
        ]

        st.text_area(
            "Texte extrait",
            value=page_data["text"],
            height=350,
        )

        pdf_bytes = (
            st.session_state.catalogue_files
            .get(diagnostic_catalogue)
        )

        if pdf_bytes:
            diagnostic_image = render_pdf_page(
                pdf_bytes=pdf_bytes,
                page_number=int(diagnostic_page),
            )

            st.image(
                diagnostic_image,
                caption=f"Page {diagnostic_page}",
                use_container_width=True,
            )


# ==========================================================
# PIED DE PAGE
# ==========================================================

st.divider()

st.caption(
    "Prototype AI Sourcing · Les correspondances et données fournisseurs "
    "doivent être vérifiées avant tout contact ou décision d'achat."
)
