# AI Sourcing Délifrance: instructions projet

## Contexte métier
Application de sourcing pour un acheteur tiers Délifrance. Les besoins portent sur des volumes industriels importants. Le but n'est pas de trouver des boutiques, artisans, distributeurs ou marketplaces, mais des fabricants agroalimentaires européens capables de produire à grande échelle, idéalement en foodservice, private label ou co-manufacturing.

## État du dépôt (version 2)
Une implémentation complète existe déjà. Lis `README.md` avant toute modification.

- `app.py`: interface Streamlit uniquement.
- `cloudinary_pdf_tools.py`: configuration, liste, envoi signé, téléchargement (avec repli authentifié sur 401), envois parallèles et reprise.
- `sourcing/`: logique métier sans dépendance Streamlit (`textutils`, `suppliers`, `catalog_names`, `blocks`, `pdf_split`, `pdf_render`, `store`, `indexer`, `internal_search`, `web_search`, `web_classify`, `web_scoring`, `domains`, `manufacturers`, `shortlist`, `settings`, `cache`).
- `data/known_suppliers.txt`: fournisseurs déjà référencés de l'acheteur (modifiable sans toucher au code).
- `data/verified_manufacturers.csv`: base OPTIONNELLE, vide et désactivée par défaut.
- `tests/`: tests `unittest` (collectés aussi par `pytest`), avec faux HTTP (`tests/fakes.py`), fabrique de PDF (`tests/pdf_factory.py`) et faux module Streamlit (`tests/streamlit_stub.py`).
- `requirements.txt`: dépendances compatibles Python 3.12. `secrets.example.toml`: noms de secrets sans vraies valeurs.

Le code reprend les fonctions de l'ancien `app.py` (conservé dans `baseline_before_claude/` après `run_claude`): nommage `_part_NN_pages_a-b`, `CLOUDINARY_PREFIX` (défaut `ai-sourcing/catalogues`), fournisseur lu au début du nom de fichier, aperçu de page, onglet Bibliothèque, synchronisation au démarrage. Toute fonctionnalité de l'ancienne version encore absente doit être portée sans régression.

## Contraintes impératives
1. Ne jamais écrire de vraie clé API dans le dépôt, les logs, les erreurs ou les tests. Ne jamais lire `.streamlit/secrets.toml` ni `.env`.
2. Conserver Cloudinary comme stockage des PDF et Tavily comme moteur web gratuit.
3. Ne pas ajouter OpenAI ni une autre API payante obligatoire.
4. Supporter les PDF Cloudinary `raw`, y compris les gros PDF découpés en parties de moins de 10 Mo.
5. Ne jamais charger toute la bibliothèque PDF en mémoire simultanément.
6. Ne pas indexer tous les catalogues à chaque interaction Streamlit.
7. Toutes les erreurs réseau doivent avoir un timeout, des tentatives limitées et un message utilisateur clair.
8. Python 3.12, typage sur les fonctions publiques, fonctions courtes, responsabilités séparées.
9. Aucun résultat fournisseur ne doit être présenté comme confirmé sans preuve issue d'une page officielle.
10. Ne jamais inventer une URL, une information fournisseur ou une ligne de la base vérifiée.
11. **Aucun nom de fournisseur, aucune URL de fournisseur dans le code** (un test le vérifie). La recherche Internet s'adapte à la demande: produit, grammage, attributs, pays cités, langues locales, variantes apprises dans les résultats. Elle ne part d'aucune liste.
12. Les fournisseurs déjà référencés (`data/known_suppliers.txt` + fournisseurs des catalogues indexés) sont écartés avant toute requête ciblée, sauf case « inclure ». La reconnaissance reste prudente: un nom composé de mots génériques ne reconnaît jamais une entreprise par ressemblance.
13. Compatibilité Streamlit 1.65: `width="stretch"`, jamais `use_container_width`.

## Vocabulaire des statuts (un seul, dans tout le code)
Niveau de preuve d'une information (`sourcing.models.Level`): `confirmé`, `probable`, `à confirmer`, `non trouvé`.

Statut global d'un fournisseur (`SupplierCandidate.status`), dérivé des niveaux de preuve industrielle et produit, jamais saisi à la main:
- `fabricant confirmé`: preuve industrielle confirmée **et** preuve produit confirmée, toutes deux sur le domaine officiel (ou dans la base vérifiée datée et sourcée);
- `fabricant probable`: les deux preuves au moins probables;
- `à confirmer`: une seule preuve, ou signaux de distributeur;
- `non trouvé`: aucune preuve.

Quand aucune page n'existe, afficher exactement `Non trouvé publiquement`.

## Recherche catalogue interne
La recherche interne est prioritaire et doit être fiable.

### Indexation
- Index incrémental basé sur `public_id`, `version`, `bytes`, ETag ou équivalent Cloudinary.
- Conserver le nom logique du catalogue même lorsqu'il est découpé en parties (`<Nom>__part02of06_p0046.pdf`).
- Reconstituer le numéro de page original.
- Extraire texte, références, grammages, conditionnements et titres produits.
- Rechercher au niveau de blocs produits ou de passages courts, pas seulement de pages entières.
- Dédupliquer les passages et produits.
- Signaler les PDF nécessitant un OCR sans faire planter l'application. Un OCR éventuel doit rester facultatif.
- Index persistant SQLite (FTS5) pour éviter une réindexation complète.

### Classement interne
Le score est explicable et combine séparément: mots exacts, proximité BM25, famille produit et synonymes multilingues, grammage avec tolérance configurable, référence, conditionnement, bloc produit structuré, pénalités (indices produit insuffisants, texte non produit, passage très court, grammage incompatible).

Afficher les composantes du score. Ne pas afficher un pourcentage arbitraire présenté comme une certitude.

## Recherche Internet de fabricants
La recherche doit viser de grands industriels européens adaptés aux volumes Délifrance.

### Priorités
- fabricants industriels européens;
- boulangerie, viennoiserie, pâtisserie ou catégorie correspondante;
- foodservice, B2B, private label, MDD, contract manufacturing;
- usines, capacité industrielle, présence internationale, export;
- produit et grammage proches;
- pages produits officielles et catalogues officiels.

### Requêtes
- Plusieurs requêtes très ciblées en français et anglais (et allemand).
- Termes `manufacturer`, `industrial`, `foodservice`, `private label`, `factory`, `Europe`.
- Exclure marketplaces, retail, recettes, restaurants, artisans, réseaux sociaux, annuaires faibles et distributeurs sans preuve de fabrication (`sourcing/domains.py`, `exclude_domains` Tavily).
- Un petit nombre de requêtes concurrentes, avec un budget strict pour le quota gratuit.

### Contenu d'un résultat
Entreprise; pays; statut industriel; produit similaire; grammage si trouvé; lien direct vers la page produit officielle; lien catalogue ou fiche technique; lien contact officiel; preuve industrielle; preuve produit; private label/foodservice; points à confirmer (MOQ, capacité, certifications, origine, lead time, livraison France); score explicable; date de vérification.

## Base de fabricants vérifiés (optionnelle)
- Vide par défaut et désactivée par défaut (case à cocher). Fichier de données séparé (`data/verified_manufacturers.csv`) avec sources, catégorie et date de vérification; jamais une liste codée en dur.
- **Une ligne n'est ajoutée que si ses URLs ont été réellement lues sur une page officielle pendant la session.** Sans accès web, ne pas enrichir le fichier: le laisser tel quel et le signaler.
- Une marque ou un site pays avec un autre domaine a sa propre ligne.

## Performance
- Cache borné avec TTL (`sourcing/cache.py`, `st.cache_data(max_entries=..., ttl=...)`).
- Téléchargements Cloudinary à la demande, un PDF à la fois, supprimé après indexation.
- Envois parallèles limités et reprise après erreur.
- Pas de recalcul tant que `index_version` n'a pas changé.
- Progression honnête; mémoire limitée pour Streamlit Community Cloud.

## UX acheteur
- Parcours en deux étapes: base interne, puis nouveaux industriels, puis shortlist.
- Filtres: pays, catégorie, statut industriel, grammage, fournisseur connu/nouveau.
- Boutons `Pertinent`, `À étudier`, `Non pertinent` avec export CSV (séparateur `;`, UTF-8 BOM).
- Lien direct vers la page produit obligatoire lorsqu'il existe.

## Tests et validation
Avant de déclarer le travail terminé:
- `python -m py_compile app.py cloudinary_pdf_tools.py`
- `python -m pytest -q` (ou `python -m unittest discover -s tests -t .`): tous les tests passent;
- ajouter un test pour chaque défaut corrigé;
- une demande hors boulangerie (ex. tiramisu, pizza) produit des requêtes sans vocabulaire de boulangerie imposé;
- test sans secrets; réponses Cloudinary/Tavily simulées;
- PDF de 47 Mo découpé sous 9 Mio; erreur 401 Cloudinary diagnostiquée clairement;
- une recherche « Mini Danish 40 g » fait remonter les fabricants vérifiés pertinents;
- Amazon, recettes, marketplaces et boutiques exclus;
- le lien retourné mène à une page produit officielle, pas à la page d'accueil.

Les tests utilisent un faux module Streamlit: ils ne remplacent pas `streamlit run app.py`. Dis explicitement ce que tu n'as pas pu exécuter.

## Méthode de travail Claude Code
1. Lire tout le dépôt et le présent fichier.
2. Écrire un diagnostic court avant modification.
3. Créer ou vérifier la sauvegarde `baseline_before_claude/`.
4. Proposer l'architecture cible, brièvement.
5. Modifier les fichiers.
6. Exécuter les tests.
7. Corriger les erreurs.
8. Résumer: changements, limites, ce qui n'a pas été vérifié, commandes de déploiement.
