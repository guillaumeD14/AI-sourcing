"""Modèles partagés. Les statuts distinguent toujours confirmé / probable / à confirmer / non trouvé."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

NOT_FOUND_PUBLIC = "Non trouvé publiquement"

STATUS_CONFIRMED = "fabricant confirmé"
STATUS_PROBABLE = "fabricant probable"
STATUS_TO_CONFIRM = "à confirmer"
STATUS_NOT_FOUND = "non trouvé"
STATUSES = [STATUS_CONFIRMED, STATUS_PROBABLE, STATUS_TO_CONFIRM, STATUS_NOT_FOUND]
STATUS_ICONS = {STATUS_CONFIRMED: "🟢", STATUS_PROBABLE: "🟡", STATUS_TO_CONFIRM: "🟠", STATUS_NOT_FOUND: "⚪"}


class Level(str, Enum):
    CONFIRMED = "confirmé"
    PROBABLE = "probable"
    TO_CONFIRM = "à confirmer"
    NOT_FOUND = "non trouvé"


@dataclass(frozen=True)
class Evidence:
    """Une preuve : toujours rattachée à une URL et à une date de vérification."""

    kind: str          # "industrielle", "produit", "foodservice", "private label", "contact", "fiche technique"
    url: str
    origin: str        # "site officiel", "base vérifiée", "autre source"
    snippet: str = ""
    checked_on: str = ""

    @property
    def official(self) -> bool:
        return self.origin in ("site officiel", "base vérifiée")


@dataclass
class SupplierCandidate:
    key: str
    company: str
    domain: str
    country: str = ""
    country_code: str = ""
    categories: list[str] = field(default_factory=list)
    known_supplier: bool = False
    known_as: str = ""
    from_verified_base: bool = False
    status: str = STATUS_TO_CONFIRM
    industrial_level: Level = Level.NOT_FOUND
    product_level: Level = Level.NOT_FOUND
    private_label: Level = Level.NOT_FOUND
    foodservice: Level = Level.NOT_FOUND
    similar_product: str = ""
    weights_g: list[float] = field(default_factory=list)
    product_page_url: str | None = None
    datasheet_url: str | None = None
    contact_url: str | None = None
    industrial_evidence: list[Evidence] = field(default_factory=list)
    product_evidence: list[Evidence] = field(default_factory=list)
    other_evidence: list[Evidence] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    max_score: float = 100.0
    score: float = 0.0
    to_confirm: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked_on: str = ""
    visual_similarity: float | None = None
    image_url: str | None = None

    @property
    def score_label(self) -> str:
        return f"{self.score:.0f} / {self.max_score:.0f}"
