# AI Sourcing Delifrance avec Cloudinary

Application Streamlit de sourcing industriel agroalimentaire pour un acheteur tiers Delifrance.

## Architecture

- GitHub contient uniquement le code.
- Cloudinary stocke les catalogues PDF comme ressources `raw`.
- Streamlit indexe le texte sans conserver tous les PDF en memoire.
- Tavily effectue la recherche gratuite de nouveaux industriels.

## Fonctions

1. Lister les PDF du dossier Cloudinary.
2. Indexer le texte des catalogues.
3. Rechercher un produit dans les pages PDF.
4. Afficher une page PDF a la demande.
5. Ajouter des PDF dans Cloudinary depuis Streamlit.
6. Rechercher des fabricants industriels avec Tavily.
7. Exporter la shortlist au format CSV.

## Configuration Cloudinary

Creer un compte Cloudinary puis recuperer dans les parametres API :

- Cloud name ;
- API Key ;
- API Secret.

Les PDF sont envoyes avec `resource_type="raw"` dans :

```text
ai-sourcing/catalogues/
```

## Secrets Streamlit

Dans `Manage app > Settings > Secrets` :

```toml
CLOUDINARY_CLOUD_NAME = "votre-cloud-name"
CLOUDINARY_API_KEY = "votre-api-key"
CLOUDINARY_API_SECRET = "votre-api-secret"
CLOUDINARY_PREFIX = "ai-sourcing/catalogues"
TAVILY_API_KEY = "tvly-votre-cle-tavily"
```

Ne jamais publier les vraies valeurs dans GitHub.

## Installation

Utiliser Python 3.12.

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Utilisation

1. Ouvrir l'application.
2. Synchroniser Cloudinary.
3. Ajouter les catalogues depuis la barre laterale si necessaire.
4. Decrire le besoin produit.
5. Rechercher dans les catalogues Cloudinary.
6. Consulter les pages proches.
7. Rechercher de nouveaux industriels.
8. Verifier les preuves et liens avant tout contact.

## Organisation recommandee

```text
ai-sourcing/catalogues/
├── viennoiseries/
│   ├── unibake/
│   ├── kohberg/
│   └── la-lorraine/
├── pains/
├── patisseries/
└── archives/
```

## Memoire

Chaque PDF est telecharge separement pendant l'indexation, puis libere. Les apercus sont charges uniquement a la demande et le cache est limite.

## Limites

- Le plan gratuit Cloudinary peut limiter la taille des fichiers `raw`.
- Les PDF scannes sans couche texte necessitent un OCR.
- La photo n'est pas encore comparee aux images des catalogues.
- La recherche Tavily gratuite a un quota mensuel.
- Les resultats fournisseurs doivent etre verifies.

## Securite

Ne jamais envoyer vers Cloudinary des documents confidentiels sans validation interne. Ne jamais placer les cles API ou secrets dans GitHub.
