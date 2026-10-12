# Mission Claude Code

Le dépôt contient déjà une implémentation complète d'AI Sourcing Délifrance (voir `README.md` et `CLAUDE.md`). Elle reprend les fonctions de l'ancien `app.py` et a été testée avec des réponses simulées, **sans Streamlit réel, ni Cloudinary, ni Tavily, ni PyMuPDF, ni catalogues réels**. Ta mission est de la valider, de la durcir et de la rendre conforme à la réalité du projet, sans régression.

## Étapes

### 1. Vérifier l'existant
- Lire `CLAUDE.md`, `README.md`, puis le code.
- Exécuter `python -m py_compile app.py cloudinary_pdf_tools.py` et `python -m pytest -q`. Les corriger s'ils échouent chez l'utilisateur (version de Python, dépendances).
- Lancer `streamlit run app.py` si c'est possible et vérifier que l'application démarre **sans aucune clé**. Corriger toute erreur d'interface que le faux module `tests/streamlit_stub.py` n'aurait pas détectée (signatures de widgets selon la version de Streamlit installée, par exemple).

### 2. Vérifier la parité avec l'ancienne version (priorité haute)
- Examiner `baseline_before_claude/` sans le modifier.
- Comparer fonctionnalités, noms de secrets, conventions Cloudinary, textes et filtres avec la nouvelle version; porter ce qui manque sans casser les tests. L'ancienne liste de fournisseurs codée en dur est **volontairement supprimée**: ne pas la rétablir.
- Tester avec le vrai PyMuPDF installé: aperçu de page, extraction, optimisation, rasterisation d'une page trop lourde (`sourcing/pdf_render.py`).
- Si l'ancien code utilisait d'autres noms de secrets ou de dossiers Cloudinary, les accepter dans `CloudinaryConfig.from_mapping` / `Settings` plutôt que d'imposer un renommage.

### 3. Confronter au réel (sans jamais afficher de secret)
- Si des clés sont disponibles dans l'environnement (`CLOUDINARY_*`, `TAVILY_API_KEY`), tester la liste, le téléchargement, l'envoi d'un petit PDF de test et une recherche Tavily réelle. Sinon, le dire et ne rien simuler à la place.
- Si des catalogues PDF réels sont fournis, indexer 2 ou 3 d'entre eux et vérifier: blocs produits, grammages, références, numéros de pages, classement de « Mini Danish 40 g ». Améliorer `sourcing/blocks.py` et `sourcing/textutils.py` à partir de cas réels, avec un test par cas.

### 4. Qualité de la recherche Internet (sans fournisseur codé en dur)
- Avec une vraie clé Tavily, essayer plusieurs demandes très différentes (viennoiserie, pizza, dessert, avec et sans pays) et vérifier que les requêtes et les résultats s'adaptent. Corriger les heuristiques (`web_search.py`, `web_classify.py`, `textutils.py`) à partir de cas réels, avec un test par cas.
- Vérifier que `data/known_suppliers.txt` écarte bien les fournisseurs déjà référencés et que la reconnaissance ne produit ni faux positif ni faux négatif évidents.
- Ne jamais ajouter de fournisseur dans le code ni pré-remplir `data/verified_manufacturers.csv`.

### 5. Robustesse
- Vérifier l'absence de régression sur: secrets jamais exposés, erreurs 401 Cloudinary diagnostiquées, reprise d'envoi, index incrémental, PDF scannés signalés, exclusion des marketplaces et recettes, liens produits profonds uniquement.
- Si tu ajoutes un OCR, il doit être facultatif, documenté et désactivé par défaut.

## Critères d'acceptation
- L'application démarre sans clé réelle et les secrets ne sont jamais exposés.
- Le code compile et tous les tests passent; chaque correction a son test.
- Un PDF de 47 Mo est géré sans dépasser 9 Mio par partie.
- L'index ne se reconstruit pas intégralement à chaque interaction.
- Les résultats internes affichent page originale, produit, fournisseur, référence et grammage.
- Les résultats Internet privilégient de gros industriels européens, avec preuves officielles et page produit accessible en un clic, ou `Non trouvé publiquement`.
- L'export CSV est exploitable par un acheteur.
- Le README reste exact (mettre à jour le tableau « testé / non testé »).

## Déroulement
1. Diagnostic court (ce qui marche, ce qui manque par rapport à `baseline_before_claude/`).
2. Plan bref.
3. Implémentation sans demander de validation intermédiaire.
4. Tests.
5. Bilan final: fichiers modifiés, **ce qui n'a pas pu être vérifié**, commandes de déploiement.
