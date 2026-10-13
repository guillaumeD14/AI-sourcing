"""Lecture des secrets et paramètres. Fonctions pures : aucune dépendance à Streamlit."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cloudinary_pdf_tools import CloudinaryConfig

SECRET_NAMES = (
    "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET", "CLOUDINARY_URL", "CLOUDINARY_PREFIX",
    "CLOUDINARY_FOLDER", "CLOUDINARY_RESOURCE_TYPES", "CLOUDINARY_SIGNATURE_ALGORITHM", "TAVILY_API_KEY",
)
_PLACEHOLDERS = ("votre", "your", "colle", "xxx", "changeme", "placeholder", "<")


@dataclass(frozen=True)
class Settings:
    cloudinary: CloudinaryConfig
    tavily_api_key: str
    db_path: Path
    manufacturers_path: Path
    known_suppliers_path: Path

    @property
    def cloudinary_ready(self) -> bool:
        return self.cloudinary.is_complete

    @property
    def tavily_ready(self) -> bool:
        """Une clé Tavily valide commence par « tvly- » (même contrôle que l'application d'origine)."""
        return self.tavily_api_key.startswith("tvly-")

    def __repr__(self) -> str:
        return f"Settings(cloudinary={self.cloudinary!r}, tavily_api_key=***, db_path={self.db_path})"

    def cache_key(self) -> str:
        return f"{self.cloudinary.cache_key()}|{bool(self.tavily_api_key)}|{self.db_path}"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any], env: Mapping[str, str] | None = None) -> "Settings":
        """``values`` (secrets Streamlit) prime sur ``env`` (variables d'environnement)."""
        environment = os.environ if env is None else env
        merged: dict[str, Any] = {name: environment[name] for name in SECRET_NAMES if environment.get(name)}
        merged.update({k: v for k, v in values.items() if v not in (None, "")})
        tavily = merged.get("tavily")
        tavily_key = str(merged.get("TAVILY_API_KEY") or (tavily.get("api_key") if isinstance(tavily, Mapping) else "") or "").strip()
        if tavily_key.lower().startswith(_PLACEHOLDERS):
            tavily_key = ""
        return cls(
            cloudinary=CloudinaryConfig.from_mapping(merged),
            tavily_api_key=tavily_key,
            db_path=Path(str(environment.get("SOURCING_DB_PATH", "catalog_index.sqlite"))),
            manufacturers_path=Path(str(environment.get("SOURCING_MANUFACTURERS_CSV", "verified_manufacturers.csv"))),
            known_suppliers_path=Path(str(environment.get("SOURCING_KNOWN_SUPPLIERS", "known_suppliers.txt"))),
        )
