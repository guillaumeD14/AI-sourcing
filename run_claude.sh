#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Sauvegarde de l'existant avant toute modification par Claude Code.
mkdir -p baseline_before_claude/data
for file in app.py requirements.txt README.md secrets.example.toml cloudinary_pdf_tools.py; do
  if [ -f "$file" ]; then
    cp -p "$file" "baseline_before_claude/$file"
  fi
done
for dir in sourcing tests; do
  if [ -d "$dir" ]; then
    rm -rf "baseline_before_claude/$dir"
    cp -pR "$dir" "baseline_before_claude/$dir"
  fi
done
for data_file in verified_manufacturers.csv known_suppliers.txt; do
  if [ -f "data/$data_file" ]; then
    cp -p "data/$data_file" "baseline_before_claude/data/$data_file"
  fi
done

if command -v git >/dev/null 2>&1 && [ -d .git ]; then
  git status --short
  branch="claude/improve-ai-sourcing"
  git switch -c "$branch" 2>/dev/null || git switch "$branch"
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "Claude Code n'est pas installe ou n'est pas dans PATH." >&2
  echo "Installe Claude Code puis relance ce script." >&2
  exit 1
fi

# Le prompt est lu depuis le fichier par Claude Code : pas de probleme de guillemets ni de taille d'argument.
claude --permission-mode acceptEdits "Lis PROMPT_CLAUDE_CODE.md puis execute la mission qui y est decrite, en respectant CLAUDE.md."
