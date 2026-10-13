# AI Sourcing Délifrance

Application Streamlit d'aide à l'achat : à partir d'un besoin (produit, grammage, critères), elle
1. cherche d'abord dans **vos catalogues PDF** (Cloudinary), au niveau du produit et de la page d'origine, avec aperçu de la page ;
2. cherche ensuite de **nouveaux fabricants industriels** sur Internet (Tavily) en s'adaptant à la demande, **sans aucun fournisseur codé en dur**, et en écartant ceux que vous avez déjà ;
3. produit une **shortlist CSV** exploitable en appel d'offres.

Aucune API payante : Cloudinary et Tavily (offres gratuites). Pas d'OpenAI, pas de modèle de langage.

## Ce qui a été testé, et ce qui ne l'a pas été

| Testé automatiquement (135 tests, `unittest`) | **Non testé** : à vérifier chez vous |
|---|---|
| Découpage d'un PDF de 47 Mo en parties < 9 Mio, nommées comme votre application d'origine (`_part_01_pages_1-45`) | Lancement dans un vrai Streamlit 1.65 (l'interface a tourné avec un faux module `streamlit`) |
| Reconnaissance de votre liste de 119 fournisseurs (formes juridiques, pays, codes) | Vrais appels Cloudinary et Tavily (réponses simulées uniquement) |
| Index SQLite incrémental, pages d'origine, PDF scannés signalés, synchronisation au démarrage | Les fonctions PyMuPDF réelles (aperçu, extraction rapide, optimisation, rasterisation) : seul le code qui les appelle est testé, avec un faux `fitz` |
| Requêtes Internet qui changent avec le produit, le pays et les attributs ; apprentissage de variantes ; fournisseurs référencés écartés avant toute requête | Vos vrais catalogues (mise en page, PDF scannés) |
| Aucun nom de fournisseur ni URL de fournisseur dans le code | `run_claude.ps1`, Claude Code |
| Cloudinary : liste paginée, signature, retries, 401 + repli authentifié, envois parallèles et reprise | OCR (non implémenté) |
| Aucun secret dans le dépôt ni dans les messages d'erreur | |

## Installation

Python 3.12.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\Activate.ps1
pip install -r requirements.txt
mkdir .streamlit && cp secrets.example.toml .streamlit/secrets.toml   # puis éditer (ne jamais mettre ce fichier sur GitHub)
streamlit run app.py
```

`requirements.txt` reprend vos versions (`streamlit==1.65.0`, `PyMuPDF==1.26.4`, `requests==2.34.2`) et ajoute `pypdf`. Le SDK `cloudinary`, `scikit-learn`, `rapidfuzz`, `pandas` et `Pillow` ne sont plus requis par le code (Streamlit installe lui-même pandas et Pillow). Les tests sont dans un fichier zip facultatif séparé (`tests_optionnels`).

L'application démarre **sans aucune clé** et indique dans la barre latérale ce qui manque.

## Secrets

`.streamlit/secrets.toml` en local (le dossier `.streamlit` se crée sur votre ordinateur, pas sur GitHub), *App settings → Secrets* sur Streamlit Community Cloud (les variables d'environnement de mêmes noms fonctionnent aussi).

| Nom | Rôle |
|---|---|
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | Accès aux catalogues. Alternative : `CLOUDINARY_URL`. Les valeurs d'exemple (`votre-…`, `colle-…`) sont traitées comme non configurées. |
| `CLOUDINARY_PREFIX` | Dossier des catalogues. **Défaut : `ai-sourcing/catalogues`** (celui de votre ancienne application). |
| `TAVILY_API_KEY` | Recherche Internet (clé commençant par `tvly-`). |

Les secrets ne sont jamais affichés, journalisés ni inclus dans les messages d'erreur.

## Cloudinary

- Les catalogues sont des fichiers `raw`. Limite gratuite : 10 Mo par fichier ; l'application découpe tout PDF de plus de 9 Mio.
- **Nommage conservé** : `<préfixe>/<sous-dossier>/<Nom>_part_02_pages_46-90.pdf`. Vos catalogues déjà envoyés sont retrouvés tels quels et le numéro de page d'origine est reconstitué. Les noms `Nom part 2.pdf` sont aussi reconnus.
- **Fournisseur** : comme avant, le premier mot du nom de fichier (`KOHBERG_catalogue.pdf` → KOHBERG). Si ce début correspond sans ambiguïté à un nom de `known_suppliers.txt`, le nom officiel est utilisé (→ *Kohberg Bakery Group A S*). Un début ambigu (`PANIFICIO_…`) reste tel quel.
- Envoi (barre latérale → *Ajouter un gros catalogue*) : optimisation si PyMuPDF, découpage, envois parallèles (3 au plus), reprise après échec, tags `ai-sourcing` / `catalogue-fournisseur`, invalidation du cache CDN. Une page seule trop lourde est recompressée en image (le texte de cette page n'est alors plus cherchable) si PyMuPDF est installé.
- **Erreur 401 au téléchargement** : livraison publique, puis téléchargement authentifié côté serveur. Si les deux échouent : *Settings → Security → « PDF and ZIP files delivery »* dans Cloudinary, et vérifier clé/secret.
- La synchronisation au démarrage (désactivable) ne retraite que les PDF nouveaux ou modifiés.

## Recherche Internet : adaptée à la demande

Aucun fournisseur n'est connu du code. Pour chaque demande :

1. **Compréhension** : produit, grammage, référence, attributs (surgelé, bio, vegan, sans gluten, halal, private label, précuit, assortiment…) et **pays cités** (« Italie »). « pain suisse » est une viennoiserie, « pain en Suisse » est un pays.
2. **Requêtes composées** en anglais, français et langues locales (celle des pays demandés, sinon allemand/espagnol/italien), avec la catégorie déduite de la demande (jamais « bakery » imposé à un tiramisu).
3. **Affinage** : les variantes du produit qui reviennent dans les premiers résultats (« danish pastries ») relancent des requêtes plus précises, tant que le budget de requêtes le permet.
4. **Fournisseurs déjà référencés écartés avant toute requête ciblée** (liste `known_suppliers.txt` + fournisseurs de vos catalogues indexés). Case à cocher pour les inclure ; ils sont alors marqués « déjà référencé (nom de la liste) ».
5. **Preuves officielles** : *fabricant confirmé* = au moins une preuve industrielle **et** une page produit/gamme **sur le domaine officiel**. Un résultat vu seulement à la découverte ne confirme rien. Une page d'accueil n'est jamais un lien produit : « Non trouvé publiquement ».
6. Exclus : marketplaces, grande distribution, recettes, réseaux sociaux, annuaires faibles, domaines hors Europe (case « Europe uniquement »).

L'onglet *Industriels* affiche les requêtes réellement envoyées, les variantes apprises et la liste des fournisseurs écartés. Ce sont des **heuristiques déterministes**, pas un modèle de langage : elles s'adaptent bien aux produits et aux pays, mais ne « comprennent » pas un besoin exprimé de façon très inhabituelle.

Pour chaque fabricant, MOQ, capacité, certifications, origine des matières, lead time et livraison France restent **à confirmer** tant qu'aucune source ne les établit.

### Base de fabricants vérifiés (optionnelle, vide)
`verified_manufacturers.csv` est vide et **désactivée par défaut** (case à cocher). Si vos acheteurs veulent y consigner des fabricants vérifiés, chaque ligne exige un domaine, une date et des URLs réellement lues sur des pages officielles.

### Liste de vos fournisseurs
`known_suppliers.txt` (un nom par ligne, commentaires `#`). La reconnaissance retire formes juridiques, pays et codes (« Lantmännen Unibake Germany GmbH & Co. KG » → *lantmannen unibake*) et rapproche noms, domaines et titres de pages. Un nom composé uniquement de mots génériques (« Bakery SPA ») ne reconnaît jamais une entreprise par ressemblance.

## Recherche interne et score
Candidats via SQLite FTS5/BM25, puis re-classement transparent (points sur un maximum affiché, pas un pourcentage) : mots exacts (30) · BM25 (25) · famille/synonymes (20) · grammage (15, tolérance réglable) · référence (10) · conditionnement (5) · bloc produit structuré (5), moins les pénalités. Détail sous « Pourquoi ce score ? ». Aperçu image de la page à la demande (PyMuPDF).

## Organisation des fichiers (tout est à la racine, aucun dossier)
```text
app.py                         interface Streamlit uniquement
cloudinary_pdf_tools.py        configuration, liste, envoi signé, téléchargement, reprise
sourcing_textutils.py          grammages, références, familles, plan de requête (attributs, pays)
sourcing_suppliers.py          liste de fournisseurs référencés, reconnaissance prudente
sourcing_catalog_names.py      nommage des parties, fournisseur, décalage de pages
sourcing_blocks.py             page -> blocs produits
sourcing_pdf_split.py          découpage < 9 Mio      sourcing_pdf_render.py   PyMuPDF optionnel
sourcing_store.py / sourcing_indexer.py       index SQLite incrémental, synchronisation
sourcing_internal_search.py    recherche et score internes
sourcing_web_search.py / sourcing_web_classify.py / sourcing_web_scoring.py / sourcing_domains.py / sourcing_manufacturers.py
sourcing_shortlist.py          décisions et export CSV (« ; », UTF-8 BOM : s'ouvre dans Excel FR)
sourcing_settings.py / sourcing_errors.py / sourcing_http_utils.py / sourcing_models.py / sourcing_cache.py
known_suppliers.txt            votre liste de fournisseurs (modifiable)
verified_manufacturers.csv     base optionnelle, vide
requirements.txt, secrets.example.toml, README.md
```
Le fichier `catalog_index.sqlite` (index) est créé automatiquement au lancement : ne le mettez pas sur GitHub.

## Tests
Facultatif : décompressez `tests_optionnels` à côté de `app.py`, puis `pip install pytest` et `python -m pytest -q` (135 tests).

## Déploiement (Streamlit Community Cloud)
Le disque est **éphémère** : l'index SQLite est perdu au redémarrage et se reconstruit à la synchronisation de démarrage (incrémentale ensuite). Le premier démarrage peut être long ; « PDF par mise à jour » permet de traiter par lots.

## Limites connues
- Pas d'OCR : les PDF scannés sont signalés (« OCR requis ») et non indexés.
- Les fonctions PyMuPDF réelles n'ont pas pu être exécutées dans l'environnement de test.
- Lien « Ouvrir le PDF » / aperçu : peuvent être refusés tant que *PDF and ZIP files delivery* n'est pas activé (l'aperçu utilise le repli authentifié).
- `requirements.txt` fixe vos versions ; figez-les après un premier déploiement réussi.
