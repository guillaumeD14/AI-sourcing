"""Exceptions applicatives. Chaque exception porte un message lisible par l'acheteur.

Règle de sécurité : aucun message ne doit contenir de clé, de secret, de signature
ni d'URL complète avec paramètres (voir ``redact``).
"""

from __future__ import annotations

from typing import Iterable


class SourcingError(Exception):
    """Erreur de base : ``str(exc)`` est déjà un message utilisateur."""


class NetworkError(SourcingError):
    """Réseau indisponible, timeout ou erreur serveur persistante."""


class AuthError(SourcingError):
    """Identifiants refusés (401/403)."""


class QuotaError(SourcingError):
    """Quota ou limite de débit atteint (429 et assimilés)."""


class ServiceError(SourcingError):
    """Réponse inattendue d'un service distant."""


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """Masque toute occurrence d'un secret dans ``text``."""
    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "***")
    return cleaned
