"""Exceptions de l'application. Chaque message est destiné à l'acheteur : clair, sans secret."""

from __future__ import annotations


class SourcingError(Exception):
    """Erreur attendue et affichable telle quelle à l'utilisateur."""


class ConfigError(SourcingError):
    """Configuration ou secret manquant / invalide."""


class NetworkError(SourcingError):
    """Réseau indisponible, délai dépassé ou service en erreur après plusieurs tentatives."""


class AuthError(SourcingError):
    """Identifiants refusés (401/403) ou droits insuffisants."""


class QuotaError(SourcingError):
    """Quota ou limite de débit atteint chez le fournisseur d'API."""


class PdfError(SourcingError):
    """PDF illisible, protégé ou impossible à découper."""
