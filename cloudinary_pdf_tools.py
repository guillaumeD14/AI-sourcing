"""Accès Cloudinary pour les catalogues PDF (stockage ``raw``, parties < 10 Mo).

Responsabilités : configuration, liste des PDF, envoi signé, téléchargement (avec repli
authentifié côté serveur en cas de 401 de livraison), envois parallèles reprenables.

Sécurité : les secrets ne sont ni journalisés, ni inclus dans les messages d'erreur, ni
affichés par ``repr``. Les URL signées (qui contiennent une signature) ne sont jamais
affichées. Aucune dépendance au SDK Cloudinary : l'API REST suffit (``requests``).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode, urlparse

import requests

from sourcing.errors import AuthError, NetworkError, QuotaError, ServiceError, SourcingError, redact
from sourcing.http_utils import DEFAULT_TIMEOUT, request_with_retry
from sourcing import pdf_render
from sourcing.catalog_names import safe_stem
from sourcing.pdf_split import MAX_PART_BYTES, SplitPart, split_pdf

API_BASE = "https://api.cloudinary.com/v1_1"
DELIVERY_BASE = "https://res.cloudinary.com"
MAX_LIST_PAGES = 40  # garde-fou : 40 x 500 = 20 000 ressources
DEFAULT_PREFIX = "ai-sourcing/catalogues"  # dossier des catalogues dans l'application d'origine
_PLACEHOLDERS = ("votre", "your", "colle", "xxx", "changeme", "placeholder", "<")


class CloudinaryError(SourcingError):
    """Erreur Cloudinary générique."""


class CloudinaryAuthError(AuthError):
    """Identifiants Cloudinary refusés (401/403)."""


class CloudinaryDeliveryError(CloudinaryAuthError):
    """La livraison publique des PDF est refusée (401) et le repli authentifié a échoué."""


class CloudinaryConfigError(SourcingError):
    """Configuration Cloudinary absente ou incomplète."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloudinaryConfig:
    cloud_name: str = ""
    api_key: str = ""
    api_secret: str = ""
    prefix: str = DEFAULT_PREFIX  # dossier Cloudinary des catalogues (CLOUDINARY_PREFIX)
    resource_types: tuple[str, ...] = ("raw",)
    signature_algorithm: str = "sha1"

    @property
    def is_complete(self) -> bool:
        return bool(self.cloud_name and self.api_key and self.api_secret)

    def missing(self) -> list[str]:
        names = {"CLOUDINARY_CLOUD_NAME": self.cloud_name, "CLOUDINARY_API_KEY": self.api_key,
                 "CLOUDINARY_API_SECRET": self.api_secret}
        return [name for name, value in names.items() if not value]

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(v for v in (self.api_key, self.api_secret) if v)

    def cache_key(self) -> str:
        """Empreinte non réversible pour les caches Streamlit (jamais le secret en clair)."""
        raw = "|".join((self.cloud_name, self.api_key, self.api_secret, self.prefix, ",".join(self.resource_types)))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def __repr__(self) -> str:  # jamais de secret dans les logs / tracebacks
        return f"CloudinaryConfig(cloud_name={self.cloud_name!r}, api_key=***, api_secret=***, prefix={self.prefix!r})"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "CloudinaryConfig":
        """Lit les noms plats (``CLOUDINARY_*``), une table ``[cloudinary]`` ou ``CLOUDINARY_URL``."""
        nested = values.get("cloudinary") if isinstance(values.get("cloudinary"), Mapping) else {}

        def pick(flat: str, short: str) -> str:
            value = str(values.get(flat) or nested.get(short) or "").strip()  # type: ignore[union-attr]
            return "" if value.lower().startswith(_PLACEHOLDERS) else value  # valeur d'exemple non remplacée

        cloud, key, secret = pick("CLOUDINARY_CLOUD_NAME", "cloud_name"), pick("CLOUDINARY_API_KEY", "api_key"), pick(
            "CLOUDINARY_API_SECRET", "api_secret"
        )
        url = str(values.get("CLOUDINARY_URL") or nested.get("url") or "").strip()  # type: ignore[union-attr]
        if url.startswith("cloudinary://") and not (cloud and key and secret):
            parsed = urlparse(url)
            cloud = cloud or (parsed.hostname or "")
            key = key or (parsed.username or "")
            secret = secret or (parsed.password or "")
        types_raw = pick("CLOUDINARY_RESOURCE_TYPES", "resource_types") or "raw"
        types = tuple(t.strip() for t in types_raw.split(",") if t.strip() in {"raw", "image"}) or ("raw",)
        algo = pick("CLOUDINARY_SIGNATURE_ALGORITHM", "signature_algorithm").lower()
        return cls(
            cloud_name=cloud, api_key=key, api_secret=secret,
            prefix=(pick("CLOUDINARY_PREFIX", "prefix") or pick("CLOUDINARY_FOLDER", "folder") or DEFAULT_PREFIX).strip("/"),
            resource_types=types, signature_algorithm=algo if algo in {"sha1", "sha256"} else "sha1",
        )


def sign_params(params: Mapping[str, Any], api_secret: str, algorithm: str = "sha1") -> str:
    """Signature Cloudinary : paramètres triés ``k=v`` joints par ``&`` + secret, hachés."""
    ignored = {"file", "cloud_name", "resource_type", "api_key", "signature"}
    parts = []
    for key in sorted(params):
        value = params[key]
        if key in ignored or value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        parts.append(f"{key}={value}")
    to_sign = "&".join(parts) + api_secret
    hasher = hashlib.sha256 if algorithm == "sha256" else hashlib.sha1
    return hasher(to_sign.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemotePdf:
    public_id: str
    version: str
    bytes: int
    etag: str
    resource_type: str
    secure_url: str
    created_at: str = ""

    @property
    def fingerprint(self) -> str:
        """Change dès que le contenu change (version, taille, etag Cloudinary)."""
        return f"{self.resource_type}|{self.public_id}|{self.version}|{self.bytes}|{self.etag}"


@dataclass
class UploadResult:
    part: SplitPart
    public_id: str
    status: str  # "envoyé" | "déjà présent" | "échec"
    message: str = ""


@dataclass
class UploadJob:
    """État d'un envoi, pour reprendre après échec sans tout renvoyer."""

    stem: str
    parts: list[SplitPart]
    folder: str = ""
    tags: tuple[str, ...] = ("ai-sourcing", "catalogue-fournisseur")
    results: dict[str, UploadResult] = field(default_factory=dict)

    def public_id(self, part: SplitPart) -> str:
        name = part.path.name
        return f"{self.folder}/{name}" if self.folder else name

    def pending(self) -> list[SplitPart]:
        return [p for p in self.parts if self.results.get(self.public_id(p), None) is None
                or self.results[self.public_id(p)].status == "échec"]

    @property
    def failed(self) -> list[UploadResult]:
        return [r for r in self.results.values() if r.status == "échec"]

    @property
    def complete(self) -> bool:
        return not self.pending()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CloudinaryClient:
    def __init__(
        self,
        config: CloudinaryConfig,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not config.is_complete:
            raise CloudinaryConfigError(
                "Configuration Cloudinary incomplète : renseignez " + ", ".join(config.missing()) + "."
            )
        self.config = config
        self._factory = session_factory
        self._local = threading.local()
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep
        self._clock = clock

    # -- plomberie --------------------------------------------------------

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = self._factory()
        return self._local.session

    def _safe(self, text: str) -> str:
        return redact(text, self.config.secrets)

    def _call(self, method: str, url: str, *, service: str, timeout: Any = None, **kwargs: Any) -> requests.Response:
        return request_with_retry(
            self._session(), method, url, service=service, timeout=timeout or self.timeout,
            retries=self.retries, sleep=self._sleep, **kwargs,
        )

    def _server_message(self, response: requests.Response) -> str:
        try:
            payload = response.json()
            message = payload.get("error", {}).get("message", "") if isinstance(payload, dict) else ""
        except ValueError:
            message = ""
        header = response.headers.get("x-cld-error", "") if response.headers else ""
        return self._safe((message or header or "")[:200])

    def _raise_for_api(self, response: requests.Response, action: str) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = self._server_message(response)
        suffix = f" Détail Cloudinary : {detail}." if detail else ""
        if status in (401, 403):
            raise CloudinaryAuthError(
                f"Cloudinary refuse l'accès ({status}) pendant « {action} ». Vérifiez CLOUDINARY_CLOUD_NAME, "
                f"CLOUDINARY_API_KEY et CLOUDINARY_API_SECRET.{suffix}"
            )
        if status == 429:
            raise QuotaError(f"Limite d'appels Cloudinary atteinte pendant « {action} » : réessayez dans quelques minutes.")
        if status == 400 and "file size" in detail.lower():
            raise CloudinaryError(f"Fichier trop volumineux pour Cloudinary (limite gratuite 10 Mo).{suffix}")
        raise ServiceError(f"Cloudinary a répondu {status} pendant « {action} ».{suffix}")

    # -- liste ------------------------------------------------------------

    def list_pdfs(self, prefix: str | None = None) -> list[RemotePdf]:
        """Tous les PDF (``raw`` par défaut) sous ``prefix`` ou ``config.folder``."""
        prefix = self.config.prefix if prefix is None else prefix.strip("/")
        found: list[RemotePdf] = []
        for resource_type in self.config.resource_types:
            cursor: str | None = None
            for _ in range(MAX_LIST_PAGES):
                params: dict[str, Any] = {"max_results": 500}
                if prefix:
                    params["prefix"] = prefix
                if cursor:
                    params["next_cursor"] = cursor
                url = f"{API_BASE}/{self.config.cloud_name}/resources/{resource_type}/upload"
                response = self._call(
                    "GET", url, service="Cloudinary (liste des PDF)", params=params,
                    auth=(self.config.api_key, self.config.api_secret),
                )
                self._raise_for_api(response, "liste des PDF")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ServiceError("Réponse Cloudinary illisible (liste des PDF).") from exc
                for item in payload.get("resources", []):
                    public_id = str(item.get("public_id", ""))
                    is_pdf = public_id.lower().endswith(".pdf") or str(item.get("format", "")).lower() == "pdf"
                    if not is_pdf:
                        continue
                    if resource_type == "image" and not public_id.lower().endswith(".pdf"):
                        public_id = f"{public_id}.pdf"  # le format fait partie de l'adresse de livraison
                    found.append(
                        RemotePdf(
                            public_id=public_id, version=str(item.get("version", "")), bytes=int(item.get("bytes", 0)),
                            etag=str(item.get("etag", "")), resource_type=resource_type,
                            secure_url=str(item.get("secure_url", "")), created_at=str(item.get("created_at", "")),
                        )
                    )
                cursor = payload.get("next_cursor")
                if not cursor:
                    break
        return found

    def check_credentials(self) -> None:
        """Lève une erreur explicite si le compte ou les clés sont invalides."""
        url = f"{API_BASE}/{self.config.cloud_name}/resources/raw/upload"
        response = self._call("GET", url, service="Cloudinary (test des identifiants)", params={"max_results": 1},
                              auth=(self.config.api_key, self.config.api_secret))
        self._raise_for_api(response, "test des identifiants")

    # -- téléchargement ---------------------------------------------------

    def _delivery_url(self, remote: RemotePdf) -> str:
        version = f"v{remote.version}/" if remote.version else ""
        return (f"{DELIVERY_BASE}/{self.config.cloud_name}/{remote.resource_type}/upload/"
                f"{version}{quote(remote.public_id, safe='/')}")

    def _private_download_url(self, remote: RemotePdf) -> str:
        """URL d'API signée : téléchargement authentifié côté serveur, indépendant de la livraison CDN."""
        params: dict[str, Any] = {"public_id": remote.public_id, "type": "upload", "timestamp": int(self._clock())}
        if remote.resource_type == "image":
            params["public_id"] = remote.public_id.removesuffix(".pdf")
            params["format"] = "pdf"
        signature = sign_params(params, self.config.api_secret, self.config.signature_algorithm)
        query = urlencode({**params, "api_key": self.config.api_key, "signature": signature})
        return f"{API_BASE}/{self.config.cloud_name}/{remote.resource_type}/download?{query}"

    def _stream_to_file(self, url: str, dest: Path) -> requests.Response | None:
        response = self._call("GET", url, service="Cloudinary (téléchargement)", stream=True,
                              timeout=(self.timeout[0], 60.0))
        if response.status_code in (401, 403):
            response.close()
            return response
        try:
            self._raise_for_api(response, "téléchargement du PDF")
            with dest.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        handle.write(chunk)
        finally:
            response.close()
        return None

    def download_to_file(self, remote: RemotePdf, dest: str | Path) -> int:
        """Télécharge un PDF sur disque (flux, jamais en mémoire). Retourne la taille écrite.

        1. livraison publique ``res.cloudinary.com`` ;
        2. si 401/403 (« PDF and ZIP files delivery » désactivé), téléchargement authentifié via l'API ;
        3. si le repli échoue aussi, erreur ``CloudinaryDeliveryError`` avec la marche à suivre.
        """
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        refused = self._stream_to_file(self._delivery_url(remote), target)
        if refused is not None:
            fallback = self._stream_to_file(self._private_download_url(remote), target)
            if fallback is not None:
                hint = self._server_message(refused)
                raise CloudinaryDeliveryError(
                    "Cloudinary refuse de livrer ce PDF (401). Dans Cloudinary : Settings → Security → cochez "
                    "« PDF and ZIP files delivery », puis relancez. Le téléchargement authentifié de secours a aussi "
                    "été refusé : vérifiez que CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET appartiennent bien au "
                    f"compte « {self.config.cloud_name} ».{(' Détail : ' + hint) if hint else ''}"
                )
        size = target.stat().st_size
        if size == 0:
            raise ServiceError("Cloudinary a renvoyé un fichier vide.")
        return size

    # -- envoi ------------------------------------------------------------

    def upload_pdf(
        self, path: str | Path, public_id: str, *, overwrite: bool = True, tags: tuple[str, ...] = (),
        invalidate: bool = True,
    ) -> dict[str, Any]:
        """Envoie un PDF (≤ 10 Mo en offre gratuite) dans ``raw/upload`` avec signature."""
        file_path = Path(path)
        params: dict[str, Any] = {
            "public_id": public_id, "overwrite": overwrite, "unique_filename": False, "use_filename": False,
            "invalidate": invalidate, "timestamp": int(self._clock()),
        }
        if tags:
            params["tags"] = ",".join(tags)
        signature = sign_params(params, self.config.api_secret, self.config.signature_algorithm)
        form = {k: ("true" if v is True else "false" if v is False else str(v)) for k, v in params.items()}
        form.update({"api_key": self.config.api_key, "signature": signature})
        url = f"{API_BASE}/{self.config.cloud_name}/raw/upload"
        with file_path.open("rb") as handle:
            response = self._call(
                "POST", url, service="Cloudinary (envoi du PDF)", data=form,
                files={"file": (file_path.name, handle, "application/pdf")}, timeout=(self.timeout[0], 120.0),
            )
        self._raise_for_api(response, "envoi du PDF")
        try:
            return dict(response.json())
        except ValueError as exc:
            raise ServiceError("Réponse Cloudinary illisible après l'envoi.") from exc


# ---------------------------------------------------------------------------
# Envoi d'un gros PDF : découpage + envois parallèles limités + reprise
# ---------------------------------------------------------------------------


def prepare_upload(
    source: str | Path,
    work_dir: str | Path,
    *,
    stem: str,
    folder: str = "",
    max_bytes: int = MAX_PART_BYTES,
    progress: Callable[[int, int], None] | None = None,
    optimize: bool = True,
) -> UploadJob:
    """Optimise (si PyMuPDF), découpe sous ``max_bytes`` et prépare le suivi d'envoi."""
    source_path = Path(source)
    optimized = Path(work_dir) / "optimized.pdf"
    if optimize and pdf_render.optimize_pdf(source_path, optimized) and optimized.stat().st_size < source_path.stat().st_size:
        source_path = optimized  # PDF réécrit plus petit (PyMuPDF) : moins de parties à envoyer
    parts = split_pdf(source_path, work_dir, stem=stem, max_bytes=max_bytes, progress=progress)
    optimized.unlink(missing_ok=True)
    return UploadJob(stem=safe_stem(stem), parts=parts, folder=folder.strip("/"))


def run_upload(
    client: CloudinaryClient,
    job: UploadJob,
    *,
    max_workers: int = 3,
    skip_existing: bool = True,
    on_progress: Callable[[int, int, UploadResult], None] | None = None,
) -> UploadJob:
    """Envoie les parties en attente (au plus ``max_workers`` en parallèle).

    Les parties déjà envoyées (même nom, même taille) sont ignorées : relancer l'appel après un
    échec ne renvoie que ce qui manque. ``on_progress`` est appelé depuis le thread appelant.
    """
    pending = job.pending()
    existing: dict[str, int] = {}
    if skip_existing and pending:
        prefix = job.folder or None
        existing = {r.public_id: r.bytes for r in client.list_pdfs(prefix=prefix)}
    todo: list[SplitPart] = []
    done = 0
    total = len(pending)
    for part in pending:
        public_id = job.public_id(part)
        if existing.get(public_id) == part.size_bytes:
            result = UploadResult(part, public_id, "déjà présent")
            job.results[public_id] = result
            done += 1
            if on_progress:
                on_progress(done, total, result)
        else:
            todo.append(part)

    def send(part: SplitPart) -> UploadResult:
        public_id = job.public_id(part)
        try:
            client.upload_pdf(part.path, public_id, tags=(*job.tags, job.stem))
            return UploadResult(part, public_id, "envoyé")
        except SourcingError as exc:
            return UploadResult(part, public_id, "échec", client._safe(str(exc)))

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 4))) as pool:
        futures = [pool.submit(send, part) for part in todo]
        for future in as_completed(futures):
            result = future.result()
            job.results[result.public_id] = result
            done += 1
            if on_progress:
                on_progress(done, total, result)
    return job


def job_summary(job: UploadJob) -> str:
    sent = sum(1 for r in job.results.values() if r.status == "envoyé")
    skipped = sum(1 for r in job.results.values() if r.status == "déjà présent")
    failed = len(job.failed)
    return json.dumps({"parties": len(job.parts), "envoyées": sent, "déjà présentes": skipped, "échecs": failed},
                      ensure_ascii=False)
