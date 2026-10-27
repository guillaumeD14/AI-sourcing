"""Analyse IA *optionnelle* d'une photo produit.

Tout fonctionne sans clé (descripteurs gratuits dans ``images.py``). Avec une clé, l'IA décrit le produit
(type, forme, fourrage, décor, couleurs, poids estimé) pour enrichir la recherche texte dans les catalogues et
sur Internet. Gemini (offre gratuite, quotas limités) est utilisé avant OpenAI (payant).

Les clés passent uniquement en en-tête HTTP ; elles n'apparaissent jamais dans les messages d'erreur.
La photo est envoyée au fournisseur d'IA choisi : n'y mettez pas d'image confidentielle.
"""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from sourcing_errors import ConfigError, SourcingError
from sourcing_http import new_session, request_with_retry
from sourcing_settings import Settings, redact

PROMPT = (
    "Tu es expert en sourcing de produits de boulangerie, viennoiserie et pâtisserie industrielles surgelées. "
    "Analyse cette photo d'un produit et réponds UNIQUEMENT par un objet JSON avec ces clés : "
    '"produit_fr" (nom courant en français), "produit_en" (nom en anglais), "description" (2 phrases : forme, pâte, '
    'fourrage, décor, taille relative), "mots_cles" (liste de 6 à 10 termes de recherche en français, anglais, '
    'allemand, italien et espagnol), "couleurs" (texte court), "poids_estime_g" (nombre ou null si incertain), '
    '"etat" ("cru", "précuit", "cuit" ou "inconnu"). N\'invente pas de marque.'
)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


@dataclass
class VisualBrief:
    product_fr: str = ""
    product_en: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    colors: str = ""
    estimated_weight_g: float | None = None
    state: str = ""
    provider: str = ""

    def as_query_text(self) -> str:
        """Texte ajouté à la description du besoin (sans le grammage estimé, jamais traité comme une exigence)."""
        parts = [self.product_fr, self.product_en, *self.keywords]
        return " ".join(dict.fromkeys(p.strip() for p in parts if p and p.strip()))


def prepare_image(data: bytes, max_side: int = 1024) -> bytes:
    """Réduit la photo (JPEG) avant envoi : moins de données transmises, moins de jetons consommés."""
    image = Image.open(io.BytesIO(data))
    image.draft("RGB", (max_side * 2, max_side * 2))
    image = image.convert("RGB")
    image.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def parse_brief_json(text: str) -> dict[str, Any]:
    """Extrait l'objet JSON d'une réponse, même entouré de balises ``` ou de texte."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise SourcingError("L'IA n'a pas renvoyé de description exploitable. Réessayez ou décrivez le produit à la main.")
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        raise SourcingError("Réponse de l'IA illisible. Réessayez ou décrivez le produit à la main.") from None
    return data if isinstance(data, dict) else {}


def brief_from_dict(data: dict[str, Any], provider: str) -> VisualBrief:
    def text(key: str, limit: int = 300) -> str:
        return str(data.get(key) or "").strip()[:limit]

    raw_keywords = data.get("mots_cles") or []
    keywords = [str(k).strip()[:60] for k in raw_keywords if str(k).strip()][:10] if isinstance(raw_keywords, list) else []
    weight = data.get("poids_estime_g")
    try:
        estimated = float(weight) if weight not in (None, "") and 5 <= float(weight) <= 5000 else None
    except (TypeError, ValueError):
        estimated = None
    return VisualBrief(text("produit_fr", 80), text("produit_en", 80), text("description"), keywords, text("couleurs", 80),
                       estimated, text("etat", 20), provider)


def describe_image(settings: Settings, image_bytes: bytes, session: Any = None) -> VisualBrief:
    """Décrit la photo avec le fournisseur d'IA configuré. ``ConfigError`` si aucune clé n'est fournie."""
    provider = settings.ai_provider
    if provider is None:
        raise ConfigError("Aucune clé d'IA configurée (GEMINI_API_KEY gratuit, ou OPENAI_API_KEY). La recherche par image reste disponible sans IA.")
    session = session or new_session()
    payload = base64.b64encode(prepare_image(image_bytes)).decode()
    secrets = settings.secret_values()
    try:
        raw = _call_gemini(settings, payload, session) if provider == "gemini" else _call_openai(settings, payload, session)
    except SourcingError as exc:
        raise type(exc)(redact(exc, secrets)) from None
    return brief_from_dict(parse_brief_json(raw), provider)


def _model_hint(response: Any) -> None:
    if response.status_code in (400, 404):
        raise SourcingError("Modèle d'IA refusé ou introuvable : vérifiez GEMINI_MODEL / OPENAI_VISION_MODEL dans les Secrets Streamlit.")


def _call_gemini(settings: Settings, payload: str, session: Any) -> str:
    body = {"contents": [{"parts": [{"text": PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": payload}}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    response = request_with_retry(session, "POST", GEMINI_URL.format(model=settings.gemini_model), service="Gemini", retries=1,
                                  headers={"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"},
                                  json=body, timeout=(6.0, 45.0), error_hook=_model_hint)
    try:
        return str(response.json()["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise SourcingError("Gemini a renvoyé une réponse vide (image refusée ou quota atteint).") from None


def _call_openai(settings: Settings, payload: str, session: Any) -> str:
    body = {"model": settings.openai_model, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{payload}"}}]}]}
    response = request_with_retry(session, "POST", OPENAI_URL, service="OpenAI", retries=1,
                                  headers={"Authorization": f"Bearer {settings.openai_api_key}"}, json=body,
                                  timeout=(6.0, 45.0), error_hook=_model_hint)
    try:
        return str(response.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise SourcingError("OpenAI a renvoyé une réponse vide.") from None
