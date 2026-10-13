"""Modèles de données partagés par la recherche Internet, le scoring et l'export."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Level(str, Enum):
    """Niveau de preuve d'une information (vocabulaire imposé par CLAUDE.md)."""

    CONFIRMED = "confirmé"
    PROBABLE = "probable"
    TO_CONFIRM = "à confirmer"
    NOT_FOUND = "non trouvé"

    @property
    def rank(self) -> int:
        return {"confirmé": 3, "probable": 2, "à confirmer": 1, "non trouvé": 0}[self.value]


NOT_FOUND_PUBLIC = "Non trouvé publiquement"


@dataclass(frozen=True)
class Evidence:
    kind: str  # industriel | produit | fiche technique | contact | private label | foodservice
    url: str
    title: str = ""
    snippet: str = ""
    origin: str = "recherche web"  # ou « base vérifiée »
    checked_on: str = ""


@dataclass
class SupplierCandidate:
    domain: str
    company: str
    country: str | None = None
    country_source: str = "à confirmer"
    categories: list[str] = field(default_factory=list)
    industrial_level: Level = Level.NOT_FOUND
    product_level: Level = Level.NOT_FOUND
    private_label: Level = Level.NOT_FOUND
    foodservice: Level = Level.NOT_FOUND
    industrial_evidence: list[Evidence] = field(default_factory=list)
    product_evidence: list[Evidence] = field(default_factory=list)
    product_page_url: str | None = None
    datasheet_url: str | None = None
    contact_url: str | None = None
    similar_product: str | None = None
    weights_g: list[float] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    to_confirm: list[str] = field(default_factory=list)
    distributor_signals: bool = False
    from_verified_base: bool = False
    known_supplier: bool = False
    known_as: str | None = None  # nom du fournisseur référencé reconnu
    score: float = 0.0
    max_score: float = 100.0
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    checked_on: str = ""

    @property
    def status(self) -> str:
        """Statut industriel global : « fabricant confirmé » exige deux preuves officielles."""
        if self.distributor_signals and self.industrial_level is not Level.CONFIRMED:
            return "à confirmer"
        if self.industrial_level is Level.CONFIRMED and self.product_level is Level.CONFIRMED:
            return "fabricant confirmé"
        if self.industrial_level.rank >= Level.PROBABLE.rank and self.product_level.rank >= Level.PROBABLE.rank:
            return "fabricant probable"
        if self.industrial_level.rank >= Level.TO_CONFIRM.rank or self.product_level.rank >= Level.TO_CONFIRM.rank:
            return "à confirmer"
        return "non trouvé"

    @property
    def key(self) -> str:
        return f"web:{self.domain}"

    @property
    def score_label(self) -> str:
        return f"{self.score:.0f} / {self.max_score:.0f} pts"
