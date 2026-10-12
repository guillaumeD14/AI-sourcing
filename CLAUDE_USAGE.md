# Utilisation du pack

Ce zip contient **l'application complète** (version 2) et le pack Claude Code pour la valider et la faire évoluer.

## Mise en place

1. Si tu as déjà un dépôt: ouvre-le, puis décompresse ce zip **à sa racine** (à côté de l'ancien `app.py`). Les anciens `app.py`, `requirements.txt`, `README.md`, `secrets.example.toml` et `cloudinary_pdf_tools.py` seront remplacés: sauvegarde-les d'abord (le script ci-dessous le fait pour toi dans `baseline_before_claude/`).
2. Sans dépôt: décompresse dans un dossier vide.
3. N'ajoute aucune vraie clé dans les fichiers versionnés: copie `secrets.example.toml` vers `.streamlit/secrets.toml` (ignoré par Git).

## Lancer l'application

```bash
python -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt   # requirements.txt + pytest
python -m pytest -q
streamlit run app.py
```

## Faire valider le code par Claude Code (recommandé)

Le code a été testé avec des réponses simulées, pas avec tes vrais catalogues ni tes vraies clés. Claude Code peut le confronter à ton ancien `app.py` et à la réalité.

**Windows PowerShell**
```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\run_claude.ps1
```

**macOS, Linux ou WSL**
```bash
chmod +x run_claude.sh
./run_claude.sh
```

Le script sauvegarde les fichiers actuels dans `baseline_before_claude/`, crée la branche `claude/improve-ai-sourcing` si Git est disponible, puis ouvre Claude Code avec la mission de `PROMPT_CLAUDE_CODE.md`. Les modifications de fichiers sont acceptées automatiquement; les commandes de test (`pytest`, `py_compile`, `git status/diff/log`) sont préautorisées par `.claude/settings.json`; les autres commandes (par exemple `pip install`) te demanderont confirmation. La lecture de `.streamlit/secrets.toml` et `.env` est interdite à Claude Code.

## Après le travail de Claude Code

```bash
git diff --stat
python -m py_compile app.py cloudinary_pdf_tools.py
python -m pytest -q
streamlit run app.py
```

Ne fusionne pas la branche tant que les tests ne passent pas et que la recherche interne et les liens fournisseurs n'ont pas été vérifiés manuellement sur quelques cas réels.
