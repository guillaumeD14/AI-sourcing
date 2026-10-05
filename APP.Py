import streamlit as st

st.title("🔎 AI Sourcing")

st.success("✅ L'application fonctionne !")

st.write("Bienvenue dans votre futur agent de sourcing IA.")

product = st.text_input(
    "Quel produit recherchez-vous ?"
)

if st.button("Tester"):

    if product:
        st.write(f"Recherche demandée : {product}")
    else:
        st.warning("Indiquez un produit.")
