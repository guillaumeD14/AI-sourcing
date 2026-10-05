import streamlit as st


st.set_page_config(
    page_title="AI Sourcing",
    page_icon="🔎",
    layout="wide"
)


# TITRE
st.title("🔎 AI Sourcing")
st.subheader("Assistant IA de sourcing industriel agroalimentaire")

st.write(
    """
    Recherchez des produits similaires dans vos catalogues fournisseurs
    et sur Internet afin d'identifier de nouveaux industriels à contacter.
    """
)

st.divider()


# NOUVEAU SOURCING
st.header("🎯 Nouveau sourcing")

product_name = st.text_input(
    "Nom du produit recherché",
    placeholder="Exemple : Mini Danish assortiment 40 g"
)


# COLONNES PHOTO + FICHE TECHNIQUE
col1, col2 = st.columns(2)


with col1:

    st.subheader("🖼️ Photo du produit")

    product_image = st.file_uploader(
        "Ajoutez une photo",
        type=["png", "jpg", "jpeg", "webp"]
    )

    if product_image:
        st.image(
            product_image,
            caption="Produit recherché",
            use_container_width=True
        )


with col2:

    st.subheader("📄 Fiche technique similaire")

    technical_sheet = st.file_uploader(
        "Ajoutez une fiche technique",
        type=["pdf"]
    )

    if technical_sheet:
        st.success(
            f"Fichier ajouté : {technical_sheet.name}"
        )


# INFORMATIONS FACULTATIVES
st.divider()

with st.expander("➕ Ajouter des critères facultatifs"):

    weight = st.text_input(
        "Poids / grammage",
        placeholder="Exemple : 40 g"
    )

    volume = st.text_input(
        "Volume annuel",
        placeholder="Exemple : 30 tonnes"
    )

    country = st.text_input(
        "Pays de livraison",
        value="France"
    )

    sourcing_zone = st.multiselect(
        "Zone de sourcing",
        [
            "France",
            "Europe",
            "Europe de l'Est",
            "Royaume-Uni",
            "Amérique du Nord",
            "Asie",
            "Monde"
        ],
        default=["Europe"]
    )

    additional_info = st.text_area(
        "Autres informations",
        placeholder=(
            "Exemple : surgelé, private label, "
            "assortiment de plusieurs parfums..."
        )
    )


# LANCER LE SOURCING
st.divider()

launch = st.button(
    "🔎 LANCER LE SOURCING",
    type="primary",
    use_container_width=True
)


if launch:

    if not product_name:

        st.warning(
            "⚠️ Indiquez d'abord le nom du produit recherché."
        )

    else:

        st.success(
            f"✅ Nouveau sourcing : {product_name}"
        )

        st.info(
            """
            La première version de l'interface fonctionne.

            Prochaines briques à connecter :

            📁 Recherche dans les catalogues fournisseurs

            🖼️ Recherche par similarité d'image

            📄 Analyse de la fiche technique

            🌍 Recherche de nouveaux industriels sur Internet

            🔗 Récupération des fiches produits

            ✉️ Recherche des contacts fournisseurs
            """
        )


st.divider()

st.caption(
    "AI Sourcing - Prototype | "
    "Les résultats devront être vérifiés par l'acheteur."
)
