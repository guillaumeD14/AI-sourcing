# AI Sourcing Delifrance avec Cloudflare R2

Application Streamlit de sourcing industriel agroalimentaire pour un acheteur tiers Delifrance.

## Architecture

- GitHub contient uniquement le code.
- Cloudflare R2 contient les catalogues PDF.
- Streamlit indexe le texte sans conserver tous les PDF en memoire.
- Tavily effectue la recherche gratuite de nouveaux industriels.

## Fonctions

1. Lister les PDF du bucket R2.
2. Indexer le texte des catalogues.
3. Rechercher un produit dans les pages PDF.
4. Afficher une page PDF a la demande.
5. Ajouter des PDF dans R2 depuis l'interface.
6. Rechercher de nouveaux fabricants industriels avec Tavily.
7. Exporter la shortlist au format CSV.

## Creation du bucket R2

1. Ouvrir Cloudflare.
2. Aller dans `Storage & databases > R2`.
3. Creer un bucket, par exemple `catalogues-fournisseurs`.
4. Utiliser la classe de stockage Standard.
5. Creer un jeton API R2 limite a ce bucket avec droits lecture et ecriture.
6. Copier immediatement l'Access Key ID et le Secret Access Key.

## Secrets Streamlit

Dans `Manage app > Settings > Secrets`, ajouter :

```toml
R2_ACCOUNT_ID = "votre-account-id-cloudflare"
R2_ACCESS_KEY_ID = "votre-access-key-id-r2"
R2_SECRET_ACCESS_KEY = "votre-secret-access-key-r2"
R2_BUCKET_NAME = "catalogues-fournisseurs"
R2_PREFIX = "catalogues/"
TAVILY_API_KEY = "tvly-votre-cle-tavily"
```

Ne jamais mettre les vraies valeurs dans GitHub.

## Organisation R2

```text
catalogues/
├── viennoiseries/
│   ├── unibake/
│   ├── kohberg/
│   └── la-lorraine/
├── pains/
├── patisseries/
└── archives/
```

## Installation

Utiliser Python 3.12.

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Utilisation

1. Synchroniser R2.
2. Verifier le nombre de PDF et de pages indexees.
3. Decrire le besoin produit.
4. Lancer la recherche dans les catalogues R2.
5. Consulter les pages proches.
6. Lancer la recherche de nouveaux industriels.
7. Verifier manuellement les preuves et liens.

## Memoire

Les PDF sont telecharges un par un pendant l'indexation puis liberes. Une page est telechargee uniquement lorsqu'un resultat est ouvert. Cette architecture evite de conserver toute la bibliotheque en memoire.

## Limites

- Les PDF scannes sans texte necessitent un OCR.
- La photo n'est pas encore comparee aux images internes des catalogues.
- La recherche Tavily gratuite est limitee par un quota mensuel.
- Les resultats Internet doivent etre verifies.

## Securite

Ne pas envoyer vers R2 des documents confidentiels sans validation interne. Ne jamais stocker de cles API, prix confidentiels, contrats ou donnees personnelles dans GitHub.
