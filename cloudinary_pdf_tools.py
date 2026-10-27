"""Stockage des catalogues PDF sur Cloudinary (plan gratuit : 10 Mo maximum par fichier).

- Liste les PDF ``raw`` (et ``image``) d'un dossier, avec pagination.
- Télécharge à la demande (flux, jamais en mémoire) : URL directe, puis URL signée de l'API si le CDN répond 401.
- Découpe les gros PDF sous 9 Mo en conservant la plage de pages d'origine dans le nom des parties.
- Envoie en parallèle limité, avec reprise des seules parties en échec.

Aucun secret n'est écrit dans les messages d'erreur.
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

from sourcing_errors import AuthError, NetworkError, PdfError, SourcingError
from sourcing_http import new_session, request_with_retry
from sourcing_settings import CloudinarySettings

API_BASE = "https://api.cloudinary.com/v1_1"
MAX_PART_BYTES = 9 * 1024 * 1024          # marge sous la limite gratuite de 10 Mo
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
DELIVERY_HELP = (
    "Cloudinary refuse de livrer ce PDF (HTTP {status}). Cause la plus fréquente : dans Cloudinary, ouvrez "
    "Settings > Security > « PDF and ZIP files delivery » et cochez « Allow delivery of PDF and ZIP files ». "
    "Sinon, vérifiez que CLOUDINARY_API_KEY et CLOUDINARY_API_SECRET correspondent bien au compte « {cloud} »."
)


@dataclass(frozen=True)
class RemotePdf:
    public_id: str
    version: str
    size_bytes: int
    etag: str
    resource_type: str
    secure_url: str
    file_format: str = "pdf"

    @property
    def filename(self) -> str:
        return self.public_id.rsplit("/", 1)[-1]

    @property
    def fingerprint(self) -> str:
        return f"{self.version}|{self.size_bytes}|{self.etag}"


class CloudinaryClient:
    def __init__(self, settings: CloudinarySettings, session: Any = None, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self._session = session or new_session()
        self._clock = clock
        self._sleep = sleep

    # -- signature et URLs ---------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{API_BASE}/{self.settings.cloud_name}/{path}"

    def _sign(self, params: dict[str, Any]) -> str:
        excluded = {"file", "cloud_name", "resource_type", "api_key", "signature"}
        to_sign = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in excluded and v not in (None, ""))
        return hashlib.sha1(f"{to_sign}{self.settings.api_secret}".encode()).hexdigest()

    def signed_download_url(self, remote: RemotePdf) -> str:
        """URL de téléchargement signée de l'API (fonctionne même si la livraison CDN des PDF est bloquée)."""
        public_id, file_format = remote.public_id, ""
        if remote.resource_type == "image":
            file_format = remote.file_format or "pdf"
        params: dict[str, Any] = {"timestamp": int(self._clock()), "public_id": public_id, "type": "upload",
                                  "format": file_format or None}
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["signature"] = self._sign(params)
        params["api_key"] = self.settings.api_key
        return self._url(f"{remote.resource_type}/download") + "?" + urlencode(params, quote_via=quote)

    # -- liste -----------------------------------------------------------------

    def _auth(self) -> tuple[str, str]:
        return (self.settings.api_key, self.settings.api_secret)

    def _list_prefix(self) -> str:
        prefix = self.settings.prefix.strip("/ ")
        return f"{prefix}/" if prefix else ""

    def ping(self) -> str:
        """Vérifie les identifiants : retourne un message de succès ou lève une erreur claire."""
        self._require_ready()
        request_with_retry(self._session, "GET", self._url("resources/raw/upload"), service="Cloudinary", sleep=self._sleep, retries=1,
                           params={"max_results": 1, "prefix": self._list_prefix() or None}, auth=self._auth())
        return "Connexion Cloudinary valide."

    def _require_ready(self) -> None:
        missing = self.settings.missing()
        if missing:
            raise AuthError("Cloudinary non configuré : " + ", ".join(missing) + " manquant(s) dans les Secrets Streamlit.")

    def list_pdfs(self, *, max_pages: int = 40) -> list[RemotePdf]:
        self._require_ready()
        found: dict[tuple[str, str], RemotePdf] = {}
        for resource_type in ("raw", "image"):
            cursor: str | None = None
            for _ in range(max_pages):
                params: dict[str, Any] = {"type": "upload", "max_results": 500}
                if self._list_prefix():
                    params["prefix"] = self._list_prefix()
                if cursor:
                    params["next_cursor"] = cursor
                data = request_with_retry(self._session, "GET", self._url(f"resources/{resource_type}/upload"),
                                          service="Cloudinary", sleep=self._sleep, params=params, auth=self._auth()).json()
                for item in data.get("resources", []):
                    public_id = str(item.get("public_id", ""))
                    is_pdf = public_id.lower().endswith(".pdf") if resource_type == "raw" else str(item.get("format", "")).lower() == "pdf"
                    if not is_pdf:
                        continue
                    found[(resource_type, public_id)] = RemotePdf(
                        public_id=public_id, version=str(item.get("version", "")), size_bytes=int(item.get("bytes") or 0),
                        etag=str(item.get("etag", "")), resource_type=resource_type, secure_url=str(item.get("secure_url", "")),
                        file_format=str(item.get("format", "pdf") or "pdf"))
                cursor = data.get("next_cursor")
                if not cursor:
                    break
        return sorted(found.values(), key=lambda r: r.public_id)

    # -- téléchargement -------------------------------------------------------------

    def download_to_file(self, remote: RemotePdf, target: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> Path:
        """Télécharge en flux vers ``target``. 401/403 sur le CDN : nouvel essai par l'URL signée de l'API."""
        self._require_ready()
        target.parent.mkdir(parents=True, exist_ok=True)
        direct_status = 0
        if remote.secure_url:
            direct_status = self._stream(remote.secure_url, target, max_bytes)
            if direct_status == 200:
                return target
        signed_status = self._stream(self.signed_download_url(remote), target, max_bytes)
        if signed_status == 200:
            return target
        status = direct_status if direct_status in (401, 403) else signed_status
        if status in (401, 403):
            raise AuthError(DELIVERY_HELP.format(status=status, cloud=self.settings.cloud_name))
        if status == 404:
            raise SourcingError(f"PDF introuvable sur Cloudinary : {remote.filename}.")
        raise SourcingError(f"Téléchargement Cloudinary impossible (HTTP {status or direct_status}) : {remote.filename}.")

    def _stream(self, url: str, target: Path, max_bytes: int) -> int:
        """Retourne le code HTTP ; écrit dans ``target`` seulement si 200. Réessaie sur erreur réseau / 5xx."""
        last_error = ""
        for attempt in range(3):
            try:
                response = self._session.request("GET", url, timeout=(6.0, 60.0), stream=True)
            except Exception as exc:  # noqa: BLE001
                last_error = type(exc).__name__
                self._sleep(1.0 * (2 ** attempt))
                continue
            try:
                if response.status_code in (500, 502, 503, 504):
                    last_error = f"HTTP {response.status_code}"
                    self._sleep(1.0 * (2 ** attempt))
                    continue
                if response.status_code != 200:
                    return int(response.status_code)
                written = 0
                with target.open("wb") as handle:
                    for chunk in response.iter_content(1024 * 256):
                        written += len(chunk)
                        if written > max_bytes:
                            target.unlink(missing_ok=True)
                            raise SourcingError("PDF trop volumineux pour être traité (limite 80 Mo).")
                        handle.write(chunk)
                return 200
            finally:
                response.close()
        raise NetworkError(f"Cloudinary indisponible ({last_error}) après 3 tentatives. Réessayez dans un instant.")

    # -- ressources arbitraires (état de l'application) ----------------------------------

    def get_resource_info(self, public_id: str, resource_type: str = "raw") -> RemotePdf | None:
        """Détails d'une ressource (version, URL) ou ``None`` si elle n'existe pas."""
        self._require_ready()
        url = self._url(f"resources/{resource_type}/upload/{quote(public_id, safe='')}")
        try:
            data = request_with_retry(self._session, "GET", url, service="Cloudinary", sleep=self._sleep, auth=self._auth(), ok_statuses=(200,)).json()
        except SourcingError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return RemotePdf(public_id=public_id, version=str(data.get("version", "")), size_bytes=int(data.get("bytes") or 0),
                         etag=str(data.get("etag", "")), resource_type=resource_type, secure_url=str(data.get("secure_url", "")))

    def upload_file(self, path: Path, public_id: str, *, resource_type: str = "raw") -> dict[str, Any]:
        """Envoi signé avec écrasement. Lève une erreur claire si le fichier dépasse la limite du plan."""
        self._require_ready()
        params: dict[str, Any] = {"public_id": public_id, "timestamp": int(self._clock()), "overwrite": "true",
                                  "unique_filename": "false", "use_filename": "false", "invalidate": "true"}
        data = {**params, "api_key": self.settings.api_key, "signature": self._sign(params)}

        def explain(response: Any) -> None:
            if response.status_code == 400:
                try:
                    message = str(response.json().get("error", {}).get("message", ""))
                except Exception:  # noqa: BLE001
                    message = ""
                if "too large" in message.lower() or "maximum is" in message.lower():
                    raise SourcingError(f"Fichier trop volumineux pour Cloudinary ({path.stat().st_size / 1e6:.1f} Mo) : découpez-le sous 9 Mo.")
                if message:
                    raise SourcingError(f"Cloudinary a refusé l'envoi : {message[:160]}")

        with path.open("rb") as handle:
            response = request_with_retry(self._session, "POST", self._url(f"{resource_type}/upload"), service="Cloudinary", sleep=self._sleep,
                                          data=data, files={"file": (path.name, handle)}, timeout=(8.0, 120.0), error_hook=explain)
        return dict(response.json())

    def upload_bytes(self, content: bytes, public_id: str, *, resource_type: str = "raw") -> dict[str, Any]:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="cld_") as tmp:
            path = Path(tmp) / public_id.rsplit("/", 1)[-1]
            path.write_bytes(content)
            return self.upload_file(path, public_id, resource_type=resource_type)

    def download_bytes(self, public_id: str, *, resource_type: str = "raw", max_bytes: int = 25 * 1024 * 1024) -> bytes | None:
        info = self.get_resource_info(public_id, resource_type)
        if info is None:
            return None
        import tempfile
        with tempfile.TemporaryDirectory(prefix="cld_") as tmp:
            target = Path(tmp) / "blob"
            self.download_to_file(info, target, max_bytes=max_bytes)
            return target.read_bytes()


# ---------------------------------------------------------------------------
# Découpage des gros PDF
# ---------------------------------------------------------------------------


def safe_filename(stem: str) -> str:
    text = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return (text or "catalogue")[:80]


class _PdfBackend:
    """Interface minimale : PyMuPDF si présent (plus rapide, optimise), sinon pypdf."""

    def __init__(self, source: Path) -> None:
        self.source = source

    def __enter__(self) -> "_PdfBackend":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None: ...
    def page_count(self) -> int: raise NotImplementedError
    def write_pages(self, first: int, last: int, target: Path) -> int: raise NotImplementedError
    def shrink_images(self) -> bool: return False


class _FitzBackend(_PdfBackend):
    def __init__(self, source: Path, fitz: Any) -> None:
        super().__init__(source)
        self._fitz = fitz
        try:
            self._doc = fitz.open(str(source))
        except Exception as exc:  # noqa: BLE001
            raise PdfError(f"PDF illisible : {type(exc).__name__}.") from None
        if getattr(self._doc, "needs_pass", False):
            raise PdfError("PDF protégé par mot de passe : impossible de le découper.")

    def close(self) -> None:
        self._doc.close()

    def page_count(self) -> int:
        return len(self._doc)

    def write_pages(self, first: int, last: int, target: Path) -> int:
        part = self._fitz.open()
        try:
            part.insert_pdf(self._doc, from_page=first - 1, to_page=last - 1)
            part.save(str(target), garbage=4, deflate=True, clean=True)
        finally:
            part.close()
        return target.stat().st_size

    def shrink_images(self) -> bool:
        try:  # PyMuPDF >= 1.24 : recompresse les images trop définies (gros catalogues photo)
            self._doc.rewrite_images(dpi_threshold=130, dpi_target=100, quality=72)
            return True
        except Exception:  # noqa: BLE001
            return False


class _PyPdfBackend(_PdfBackend):
    def __init__(self, source: Path) -> None:
        super().__init__(source)
        from pypdf import PdfReader
        try:
            self._reader = PdfReader(str(source))
            if self._reader.is_encrypted:
                raise PdfError("PDF protégé par mot de passe : impossible de le découper.")
        except PdfError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PdfError(f"PDF illisible : {type(exc).__name__}.") from None

    def page_count(self) -> int:
        return len(self._reader.pages)

    def write_pages(self, first: int, last: int, target: Path) -> int:
        from pypdf import PdfWriter
        writer = PdfWriter()
        for index in range(first - 1, last):
            writer.add_page(self._reader.pages[index])
        with target.open("wb") as handle:
            writer.write(handle)
        return target.stat().st_size


def open_pdf_backend(source: Path) -> _PdfBackend:
    try:
        try:
            import pymupdf as fitz  # type: ignore[import-not-found]
        except ImportError:
            import fitz  # type: ignore[import-not-found,no-redef]
        return _FitzBackend(source, fitz)
    except ImportError:
        pass
    try:
        return _PyPdfBackend(source)
    except ImportError as exc:
        raise PdfError("Aucune bibliothèque PDF disponible (installez pymupdf).") from exc


@dataclass
class UploadPart:
    path: Path
    public_id: str
    index: int
    total: int
    first_page: int
    last_page: int
    size_bytes: int
    status: str = "pending"      # pending | done | error
    message: str = ""


@dataclass
class UploadJob:
    stem: str
    folder: str
    parts: list[UploadPart] = field(default_factory=list)

    def pending(self) -> list[UploadPart]:
        return [p for p in self.parts if p.status != "done"]

    @property
    def failed(self) -> list[UploadPart]:
        return [p for p in self.parts if p.status == "error"]

    @property
    def complete(self) -> bool:
        return bool(self.parts) and all(p.status == "done" for p in self.parts)


def part_filename(stem: str, index: int, total: int, first: int, last: int) -> str:
    """``catalogue_part02of05_p0041-0080.pdf`` : l'intervalle de pages d'origine reste lisible dans le nom."""
    return f"{stem}_part{index:02d}of{total:02d}_p{first:04d}-{last:04d}.pdf"


def split_pdf(source: Path, out_dir: Path, *, max_bytes: int = MAX_PART_BYTES,
              progress: Callable[[int, int], None] | None = None) -> list[tuple[Path, int, int]]:
    """Découpe ``source`` en parties de taille réelle <= ``max_bytes``. Retourne ``(chemin, p_début, p_fin)``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    source_size = source.stat().st_size
    with open_pdf_backend(source) as backend:
        total_pages = backend.page_count()
        if total_pages == 0:
            raise PdfError("PDF vide.")
        parts: list[tuple[Path, int, int]] = []
        guess = max(1, math.ceil(total_pages * max_bytes * 0.85 / max(source_size, 1)))
        first, shrunk = 1, False
        while first <= total_pages:
            count = min(guess, total_pages - first + 1)
            target = out_dir / f"_tmp_{len(parts):04d}.pdf"
            while True:
                last = first + count - 1
                size = backend.write_pages(first, last, target)
                if size <= max_bytes:
                    break
                if count == 1:
                    if not shrunk and backend.shrink_images():
                        shrunk = True
                        continue
                    raise PdfError(f"La page {first} pèse {size / 1e6:.1f} Mo à elle seule (limite {max_bytes / 1e6:.0f} Mo) : "
                                   "réduisez la définition des images de ce PDF.")
                count = max(1, min(count - 1, int(count * max_bytes / size * 0.92)))
            if size < 0.6 * max_bytes and last < total_pages:  # une tentative d'agrandissement pour limiter le nombre de parties
                bigger = min(total_pages - first + 1, max(count + 1, int(count * max_bytes * 0.9 / max(size, 1))))
                trial = out_dir / f"_try_{len(parts):04d}.pdf"
                trial_size = backend.write_pages(first, first + bigger - 1, trial)
                if trial_size <= max_bytes:
                    trial.replace(target)
                    count, last = bigger, first + bigger - 1
                else:
                    trial.unlink(missing_ok=True)
            parts.append((target, first, last))
            guess = max(count, 1)
            first = last + 1
            if progress:
                progress(last, total_pages)
    return parts


def prepare_upload(source: Path, parts_dir: Path, *, stem: str, folder: str, max_bytes: int = MAX_PART_BYTES,
                   progress: Callable[[int, int], None] | None = None) -> UploadJob:
    """Prépare l'envoi : un seul fichier si < 9 Mo, sinon parties nommées avec leur intervalle de pages."""
    stem = safe_filename(stem)
    folder = folder.strip("/ ")
    prefix = f"{folder}/" if folder else ""
    parts_dir.mkdir(parents=True, exist_ok=True)
    if source.stat().st_size <= max_bytes:
        target = parts_dir / f"{stem}.pdf"
        shutil.copyfile(source, target)
        return UploadJob(stem, folder, [UploadPart(target, f"{prefix}{stem}.pdf", 1, 1, 1, 0, target.stat().st_size)])
    pieces = split_pdf(source, parts_dir, max_bytes=max_bytes, progress=progress)
    job = UploadJob(stem, folder)
    for index, (path, first, last) in enumerate(pieces, start=1):
        final = path.with_name(part_filename(stem, index, len(pieces), first, last))
        path.replace(final)
        job.parts.append(UploadPart(final, f"{prefix}{final.name}", index, len(pieces), first, last, final.stat().st_size))
    return job


def run_upload(client: CloudinaryClient, job: UploadJob, on_progress: Callable[[int, int, UploadPart], None] | None = None,
               max_workers: int = 3) -> None:
    """Envoie les parties non terminées, 3 en parallèle au plus. Une partie en échec n'arrête pas les autres."""
    todo = job.pending()
    if not todo:
        return
    done = len(job.parts) - len(todo)

    def send(part: UploadPart) -> UploadPart:
        try:
            client.upload_file(part.path, part.public_id)
            part.status, part.message = "done", ""
        except AuthError:
            raise
        except SourcingError as exc:
            part.status, part.message = "error", str(exc)
        return part

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(todo)))) as pool:
        futures = [pool.submit(send, part) for part in todo]
        for future in as_completed(futures):
            part = future.result()  # une AuthError (identifiants) interrompt tout : inutile de réessayer
            done += 1
            if on_progress:
                on_progress(done, len(job.parts), part)


def job_summary(job: UploadJob) -> str:
    ok = sum(p.status == "done" for p in job.parts)
    failed = len(job.failed)
    text = f"{ok}/{len(job.parts)} partie(s) envoyée(s)"
    return text + (f", {failed} en échec (réessayez : seules ces parties seront renvoyées)" if failed else "")
