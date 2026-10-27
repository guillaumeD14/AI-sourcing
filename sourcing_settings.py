"""Lecture des secrets Streamlit / variables d'environnement.

Règles : aucun secret dans le dépôt, les logs ou les messages d'erreur. Les champs sensibles sont
exclus de ``repr`` et ``redact`` nettoie n'importe quel texte avant affichage.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT


def _flatten(mapping: Mapping[str, Any]) -> dict[str, str]:
    """Aplati les secrets : ``{"cloudinary": {"api_key": "x"}}`` -> ``cloudinary_api_key`` ET ``api_key``."""
    flat: dict[str, str] = {}
    for key, value in mapping.items():
        if hasattr(value, "items"):
            for sub_key, sub_value in value.items():
                if sub_value in (None, "") or hasattr(sub_value, "items"):
                    continue
                flat.setdefault(f"{str(key).lower()}_{str(sub_key).lower()}", str(sub_value).strip())
                flat.setdefault(str(sub_key).lower(), str(sub_value).strip())
        elif value not in (None, ""):
            flat[str(key).lower()] = str(value).strip()
    return flat


def _pick(flat: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = flat.get(name.lower())
        if value:
            return value
    return ""


@dataclass(frozen=True)
class CloudinarySettings:
    cloud_name: str = ""
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    prefix: str = ""

    def missing(self) -> list[str]:
        names = {"CLOUDINARY_CLOUD_NAME": self.cloud_name, "CLOUDINARY_API_KEY": self.api_key,
                 "CLOUDINARY_API_SECRET": self.api_secret}
        return [name for name, value in names.items() if not value]

    @property
    def ready(self) -> bool:
        return not self.missing()

    def cache_key(self) -> str:
        """Empreinte non réversible : invalide les caches Streamlit quand les identifiants changent."""
        raw = f"{self.cloud_name}|{self.api_key}|{self.api_secret}|{self.prefix}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Settings:
    cloudinary: CloudinarySettings = field(default_factory=CloudinarySettings)
    tavily_api_key: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    gemini_api_key: str = field(default="", repr=False)
    openai_model: str = "gpt-4o-mini"
    gemini_model: str = "gemini-3.1-flash-lite"
    app_password: str = field(default="", repr=False)
    ocr_languages: str = "fra+eng"
    db_path: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "delifrance_sourcing" / "index.sqlite")
    known_suppliers_path: Path = DATA_DIR / "known_suppliers.csv"
    manufacturers_path: Path = DATA_DIR / "verified_manufacturers.csv"

    @classmethod
    def from_mapping(cls, secrets: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None) -> "Settings":
        """Secrets Streamlit en priorité, puis variables d'environnement. Tolère plusieurs noms de clés."""
        flat = _flatten(env if env is not None else os.environ)
        flat.update(_flatten(secrets or {}))
        url = _pick(flat, "cloudinary_url")
        url_cloud = url_key = url_secret = ""
        if url.startswith("cloudinary://"):
            parsed = urlparse(url)
            url_cloud, url_key, url_secret = parsed.hostname or "", parsed.username or "", parsed.password or ""
        cloudinary = CloudinarySettings(
            cloud_name=_pick(flat, "cloudinary_cloud_name", "cloud_name", "cloudinary_name") or url_cloud,
            api_key=_pick(flat, "cloudinary_api_key", "api_key") or url_key,
            api_secret=_pick(flat, "cloudinary_api_secret", "api_secret") or url_secret,
            prefix=_pick(flat, "cloudinary_prefix", "cloudinary_folder", "cloudinary_pdf_folder", "prefix", "folder").strip("/ "),
        )
        defaults = cls()
        db = _pick(flat, "sourcing_db_path")
        return cls(
            cloudinary=cloudinary,
            tavily_api_key=_pick(flat, "tavily_api_key", "tavily_key"),
            openai_api_key=_pick(flat, "openai_api_key"),
            gemini_api_key=_pick(flat, "gemini_api_key", "google_api_key"),
            openai_model=_pick(flat, "openai_vision_model", "openai_model") or defaults.openai_model,
            gemini_model=_pick(flat, "gemini_model") or defaults.gemini_model,
            app_password=_pick(flat, "app_password"),
            ocr_languages=_pick(flat, "ocr_languages") or defaults.ocr_languages,
            db_path=Path(db) if db else defaults.db_path,
        )

    @property
    def cloudinary_ready(self) -> bool:
        return self.cloudinary.ready

    @property
    def tavily_ready(self) -> bool:
        return bool(self.tavily_api_key)

    @property
    def ai_provider(self) -> str | None:
        """Fournisseur d'IA vision optionnel : Gemini (offre gratuite) avant OpenAI (payant)."""
        if self.gemini_api_key:
            return "gemini"
        if self.openai_api_key:
            return "openai"
        return None

    def secret_values(self) -> list[str]:
        values = [self.cloudinary.api_key, self.cloudinary.api_secret, self.tavily_api_key,
                  self.openai_api_key, self.gemini_api_key, self.app_password]
        return [v for v in values if v and len(v) >= 6]


def redact(text: object, secrets: list[str] | tuple[str, ...]) -> str:
    """Remplace toute valeur secrète présente dans ``text`` (messages d'erreur, logs)."""
    result = str(text)
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    return result
