# 🔎 AI Sourcing Délifrance

Application Streamlit de sourcing : recherche dans vos catalogues PDF (Cloudinary), par texte ou par photo, puis recherche
d'industriels européens sur Internet en s'appuyant sur votre liste de fournisseurs (`known_suppliers.csv`) et en découvrant de nouveaux fabricants.

## Fichiers
- `app.py` : l'application · `cloudinary_pdf_tools.py` et `sourcing_*.py` : le moteur (à laisser à la racine)
- `known_suppliers.csv` : votre liste de fournisseurs (renseignez la colonne `Site` autant que possible)
- `verified_manufacturers.csv` : fabricants vérifiés, avec sources et dates (à compléter vous-même, rien n'est inventé)
- `requirements.txt` · `secrets.example.toml` · `.gitignore`

## Mise en ligne
1. Déposez tous ces fichiers à la racine du dépôt GitHub. **Supprimez l'ancien `sourcing_pdf_render.py`** et tout `sourcing_*.py` absent de ce dossier.
2. Streamlit Community Cloud → app → fichier `app.py`, Python 3.12.
3. *Settings → Secrets* : collez `secrets.example.toml` avec vos valeurs (Cloudinary obligatoire, Tavily pour Internet ; Gemini/OpenAI facultatifs).
4. **Cloudinary → Settings → Security → cocher « Allow delivery of PDF and ZIP files »** (sinon erreur 401 sur les PDF).
5. Ouvrez l'app, cliquez **Synchroniser** (20 PDF par lot) jusqu'à « 0 restant ». L'index est sauvegardé sur Cloudinary et restauré après chaque redémarrage.

## Utilisation
Onglet **Besoin** (texte, grammage, photo facultative) → **Catalogues** (base interne) → **Industriels** (Internet) → **Shortlist** (export CSV pour Excel).
Gros catalogue (> 10 Mo) : barre latérale → « Ajouter un gros catalogue » (découpage automatique).

## Bon à savoir
- Un fabricant n'est « confirmé » que si son site officiel prouve l'industrialisation et montre le produit ; sinon « probable » ou « à confirmer ».
- Le quota Tavily gratuit est mensuel : le budget par recherche se règle de 4 à 20 requêtes.
- La recherche par photo fonctionne sans clé (couleur, forme, texture) ; une clé Gemini ou OpenAI ajoute une description automatique (la photo est alors envoyée à ce service).
- Erreur « Modèle d'IA refusé » : changez `GEMINI_MODEL` / `OPENAI_VISION_MODEL` dans les secrets.
- PDF scannés (images sans texte) : signalés « OCR requis », non lus.
