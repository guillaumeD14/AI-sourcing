# AI Sourcing Délifrance

Application Streamlit de sourcing industriel agroalimentaire destinée à assister un acheteur tiers dans la recherche de produits comparables et de nouveaux fabricants.

L'application fonctionne en deux étapes :

1. recherche dans une bibliothèque interne de catalogues fournisseurs au format PDF ;
2. recherche sur Internet de nouveaux industriels susceptibles de fabriquer un produit semblable.

## Objectif métier

L'application répond à la question suivante :

> À partir du nom d'un produit, d'un grammage, d'une photographie et d'une fiche technique de référence, quels produits proches sont présents dans les catalogues fournisseurs et quels industriels agroalimentaires pourraient être contactés pour un appel d'offres ?

L'outil est conçu comme une aide au sourcing et non comme un outil de décision automatique.

Les résultats doivent toujours être vérifiés avant de contacter un fournisseur ou de prendre une décision achat.

## Fonctionnalités

### 1. Bibliothèque de catalogues

L'application charge automatiquement tous les fichiers PDF placés dans le dossier `catalogues/`.

Les sous-dossiers sont également acceptés.

Exemple :

```text
catalogues/
├── VIENNOISERIES/
│   ├── ARGRU/
│   │   └── ARGRU_CATALOGUE_2023.pdf
│   ├── UNIBAKE/
│   │   └── UNIBAKE_CATALOGUE_2026.pdf
│   └── KOHBERG/
│       └── KOHBERG_DANISH_2026.pdf
├── PAINS/
│   └── FOURNISSEUR_X/
│       └── FOURNISSEUR_X_PAINS_2026.pdf
└── PATISSERIES/
    └── FOURNISSEUR_Y/
        └── FOURNISSEUR_Y_PATISSERIES.pdf
```

Pour chaque catalogue, l'application extrait notamment :

- le fournisseur, déduit du nom du fichier ;
- le nombre de pages ;
- le texte de chaque page ;
- les grammages détectés ;
- certaines références produit ;
- le chemin du catalogue ;
- le numéro de page d'origine.

### 2. Recherche interne

L'utilisateur peut renseigner :

- le nom du produit ;
- le grammage cible ;
- des critères complémentaires ;
- une photographie de référence ;
- une fiche technique PDF d'un produit semblable.

La recherche interne utilise :

- les mots du besoin ;
- les synonymes définis dans l'application ;
- le texte extrait de la fiche technique ;
- la proximité textuelle avec les pages des catalogues ;
- la proximité du grammage lorsqu'il est disponible.

Les résultats affichent :

- le fournisseur ;
- le catalogue ;
- le numéro de page ;
- le grammage détecté ;
- la référence détectée ;
- le score de proximité textuelle ;
- un extrait du texte ;
- un aperçu de la page PDF.

### 3. Recherche de nouveaux industriels

Après la recherche interne, l'utilisateur peut lancer une recherche Internet.

L'application utilise Tavily pour trouver des pages susceptibles de correspondre au besoin.

Le moteur privilégie les termes associés à :

- fabricant ;
- producteur ;
- usine ;
- industrie agroalimentaire ;
- foodservice ;
- private label ou MDD ;
- co-manufacturing ;
- boulangerie et viennoiserie surgelées ;
- export et activité B2B.

Le moteur tente d'écarter :

- marketplaces ;
- supermarchés ;
- restaurants ;
- artisans ;
- sites de recettes ;
- réseaux sociaux ;
- simples revendeurs sans preuve de fabrication.

Pour chaque piste, l'application peut afficher :

- le nom de l'entreprise ou le domaine du site ;
- le pays lorsqu'il est disponible ;
- le statut industriel estimé ;
- le produit similaire identifié ;
- le lien direct vers la page produit ;
- le lien vers une fiche technique publique lorsqu'elle est trouvée ;
- le lien de contact lorsqu'il est trouvé ;
- l'activité private label lorsqu'elle est identifiable ;
- les éléments de preuve ;
- un niveau de pertinence.

### 4. Référentiel métier

Pour certaines familles de produits, l'application peut intégrer des fabricants de référence vérifiés afin d'éviter que les industriels importants soient absents des résultats d'une recherche gratuite.

Pour la famille des mini Danish, le référentiel actuel couvre notamment :

- Lantmännen Unibake ;
- Kohberg ;
- La Lorraine Bakery Group ;
- Europastry ;
- Gourmand Pastries.

Ce référentiel sert de filet de sécurité. Il ne remplace pas la recherche ouverte de nouveaux industriels.

### 5. Export

La shortlist d'industriels peut être exportée au format CSV afin de faciliter :

- la création d'une liste de prospects ;
- la préparation d'un appel d'offres ;
- le suivi des contacts fournisseurs ;
- la qualification manuelle des industriels.

## Architecture du dépôt

```text
AI-sourcing/
├── app.py
├── requirements.txt
├── README.md
├── secrets.example.toml
└── catalogues/
    ├── FOURNISSEUR_A_CATALOGUE.pdf
    ├── FOURNISSEUR_B_CATALOGUE.pdf
    └── sous_dossiers_facultatifs/
        └── FOURNISSEUR_C_CATALOGUE.pdf
```

## Installation locale

### Prérequis

- Python 3.12 recommandé ;
- Git ;
- une clé API Tavily gratuite ;
- les catalogues PDF à analyser.

### 1. Cloner le dépôt

```bash
git clone URL_DU_DEPOT
cd AI-sourcing
```

### 2. Créer un environnement virtuel

Sous Windows :

```bash
python -m venv .venv
.venv\Scripts\activate
```

Sous macOS ou Linux :

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Installer les dépendances

```bash
pip install -r requirements.txt
```

### 4. Configurer les secrets localement

Créer le fichier suivant :

```text
.streamlit/secrets.toml
```

Ajouter :

```toml
TAVILY_API_KEY = "tvly-votre-cle-tavily"
```

Ne jamais publier ce fichier sur GitHub.

### 5. Ajouter les catalogues

Placer les fichiers PDF dans :

```text
catalogues/
```

### 6. Lancer l'application

```bash
streamlit run app.py
```

## Déploiement sur Streamlit Community Cloud

### Configuration

Dans Streamlit Community Cloud :

- dépôt : le dépôt GitHub contenant l'application ;
- branche : `main` ;
- fichier principal : `app.py` ;
- version Python recommandée : `3.12`.

### Secret Tavily

Dans :

```text
Manage app > Settings > Secrets
```

Ajouter :

```toml
TAVILY_API_KEY = "tvly-votre-cle-tavily"
```

Puis enregistrer et redémarrer l'application.

## Utilisation

### Étape 1 : renseigner le besoin

Exemple :

```text
Produit : Mini Danish assortiment
Grammage : 40 g
Critères : surgelé, assortiment de cinq parfums, fabricant industriel européen
Photo : image du produit recherché
Fiche technique : fiche PDF d'un produit semblable
```

### Étape 2 : rechercher dans les catalogues

Cliquer sur :

```text
1. Rechercher dans les catalogues
```

Consulter ensuite l'onglet :

```text
Résultats internes
```

### Étape 3 : rechercher de nouveaux industriels

Dans les résultats internes, cliquer sur :

```text
2. Rechercher des industriels sur Internet
```

Consulter ensuite l'onglet :

```text
Industriels web
```

### Étape 4 : qualifier les prospects

Avant tout contact, vérifier au minimum :

- le statut réel de fabricant ;
- le site de production ;
- le produit similaire ;
- le grammage disponible ;
- la capacité à produire sur cahier des charges ;
- le private label ou la MDD ;
- le MOQ ;
- la capacité annuelle ;
- les certifications ;
- l'origine ;
- les délais ;
- la capacité à livrer la France ;
- les conditions logistiques et commerciales.

## Règles de nommage des catalogues

Utiliser idéalement le format :

```text
FOURNISSEUR_CATEGORIE_ANNEE_LANGUE.pdf
```

Exemples :

```text
UNIBAKE_VIENNOISERIES_2026_FR.pdf
KOHBERG_DANISH_2026_EN.pdf
LA_LORRAINE_BAKERY_2026_FR.pdf
EUROPASTRY_VIENNOISERIES_2026_FR.pdf
GOURMAND_PASTRIES_CATALOGUE_2026_EN.pdf
```

Le premier élément du nom est utilisé par l'application pour identifier le fournisseur.

## Gestion des catalogues

### Ajouter un catalogue

1. déposer le PDF dans `catalogues/` ou un sous-dossier ;
2. valider la modification dans GitHub ;
3. attendre le redéploiement Streamlit ;
4. cliquer sur `Réindexer` dans l'application.

### Remplacer un catalogue

1. supprimer ou archiver l'ancienne version ;
2. ajouter la nouvelle version ;
3. conserver une règle de nommage claire ;
4. réindexer la bibliothèque.

### Éviter les doublons

Ne pas conserver simultanément plusieurs versions actives d'un même catalogue sauf si les années ou les pays sont clairement identifiés.

Exemple :

```text
FOURNISSEUR_CATALOGUE_2025_FR.pdf
FOURNISSEUR_CATALOGUE_2026_FR.pdf
```

## Limites actuelles

### Recherche visuelle

Dans la version gratuite actuelle, la photographie sert de référence visuelle pour l'utilisateur, mais elle n'est pas encore comparée mathématiquement à toutes les images extraites des catalogues.

Une future évolution pourra ajouter un index visuel local.

### PDF scannés

Les PDF sans couche texte peuvent apparaître avec le statut :

```text
OCR requis
```

Ces documents nécessitent un module OCR avant de pouvoir être correctement recherchés.

### Extraction des produits

La recherche fonctionne actuellement principalement au niveau des pages des catalogues.

Une page peut contenir plusieurs produits. Le nom exact, le grammage ou la référence peuvent donc nécessiter une vérification manuelle dans l'aperçu PDF.

### Recherche Internet gratuite

La qualification gratuite repose sur :

- les mots présents dans les résultats ;
- les indices d'activité industrielle ;
- des règles d'exclusion ;
- les fabricants de référence déjà vérifiés.

La présence d'un résultat ne prouve pas à elle seule que l'entreprise peut répondre à l'appel d'offres.

### Limites Tavily

La version gratuite de Tavily possède un quota mensuel. L'application utilise des recherches simples et limite le nombre de requêtes afin d'économiser les crédits.

## Sécurité et confidentialité

Ne jamais publier dans GitHub :

- des clés API ;
- des mots de passe ;
- des prix confidentiels ;
- des conditions commerciales ;
- des contrats ;
- des données personnelles ;
- des documents dont le stockage externe n'est pas autorisé.

Les clés API doivent être enregistrées uniquement dans les Secrets Streamlit ou dans `.streamlit/secrets.toml` en local.

Le fichier local suivant doit rester ignoré par Git :

```text
.streamlit/secrets.toml
```

## Contrôle qualité des résultats

Pour chaque industriel proposé, appliquer les statuts suivants :

### Fabricant industriel confirmé

Une source officielle mentionne directement la fabrication, l'usine, les lignes de production ou une activité industrielle claire.

### Fabricant probable

Plusieurs indices cohérents existent, mais la preuve industrielle complète reste à confirmer.

### Statut industriel à confirmer

Le site présente un produit pertinent, mais ne permet pas de confirmer clairement que l'entreprise le fabrique directement.

### À vérifier

Le résultat peut être pertinent, mais les preuves sont insuffisantes.

## Avertissement

Cette application est un prototype d'aide au sourcing.

Les scores de proximité ne constituent pas une validation technique, qualité ou commerciale.

Avant d'intégrer un fournisseur dans un appel d'offres, l'acheteur doit vérifier les informations, contacter l'entreprise et appliquer les procédures internes de qualification fournisseur.
