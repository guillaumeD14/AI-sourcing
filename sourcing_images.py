"""Recherche par image 100 % gratuite et légère (Pillow + NumPy, aucun modèle à télécharger).

Un descripteur de 229 valeurs décrit le *sujet* de la photo (fond neutre détouré) :
histogramme de couleurs HSV, disposition des couleurs 3x3, texture/contours (HOG simplifié), forme
(proportions, remplissage) et empreinte dHash. Il capte bien couleur, forme et aspect de surface
(feuilletage, décor, fourrage visible) mais pas le sens du produit : c'est pourquoi il se combine au texte,
à la famille produit et, en option, à une description IA (voir ``vision_ai.py``).
"""

from __future__ import annotations

import io
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import numpy as np
from PIL import Image

from sourcing_errors import SourcingError

HIST_DIMS, LAYOUT_DIMS, HOG_DIMS, SHAPE_DIMS = 72, 27, 128, 2
DESCRIPTOR_DIMS = HIST_DIMS + LAYOUT_DIMS + HOG_DIMS + SHAPE_DIMS  # 229
_S_HIST = slice(0, HIST_DIMS)
_S_LAYOUT = slice(HIST_DIMS, HIST_DIMS + LAYOUT_DIMS)
_S_HOG = slice(HIST_DIMS + LAYOUT_DIMS, HIST_DIMS + LAYOUT_DIMS + HOG_DIMS)
_S_SHAPE = slice(DESCRIPTOR_DIMS - SHAPE_DIMS, DESCRIPTOR_DIMS)
WEIGHTS = {"hist": 0.35, "layout": 0.20, "hog": 0.30, "shape": 0.10, "dhash": 0.05}
RAW_BASE, RAW_SPAN = 0.50, 0.50  # une similarité brute de 0,50 vaut 0 ; 1,00 vaut 1

MIN_SIDE = 80
MAX_IMAGE_BYTES = 8_000_000


@dataclass
class ImageDescriptor:
    vector: np.ndarray       # float32, DESCRIPTOR_DIMS
    dhash: int               # 64 bits non signés
    thumb: bytes             # JPEG réduit (aperçu dans les résultats)
    width: int
    height: int

    def vector_blob(self) -> bytes:
        return self.vector.astype(np.float16).tobytes()


def load_image(data: bytes, max_bytes: int = MAX_IMAGE_BYTES) -> Image.Image | None:
    """Ouvre une image sans planter : taille bornée, formats non lisibles -> ``None``."""
    if not data or len(data) > max_bytes:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.draft("RGB", (512, 512))  # accélère et réduit la mémoire pour les JPEG
        image.load()
        return image
    except Exception:  # noqa: BLE001 - tout format illisible est simplement ignoré
        return None


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def is_usable_product_photo(image: Image.Image) -> bool:
    """Écarte logos, icônes, bandeaux, aplats et fonds blancs."""
    width, height = image.size
    if min(width, height) < MIN_SIDE:
        return False
    aspect = width / height
    if aspect > 6 or aspect < 1 / 6:
        return False
    small = np.asarray(_to_rgb(image).resize((48, 48)), dtype=np.float32)
    return float(small.std()) >= 8.0 and not (small.mean() > 248 and small.std() < 20)


def _subject_mask(arr: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Masque du sujet par écart à la couleur de fond (médiane du cadre). Repli : image entière."""
    height, width = arr.shape[:2]
    border = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]]).astype(np.int16)
    background = np.median(border, axis=0)
    distance = np.abs(arr.astype(np.int16) - background).max(axis=2)
    mask = distance > 32
    coverage = float(mask.mean())
    if coverage < 0.03 or coverage > 0.97:
        return np.ones((height, width), dtype=bool), (0, 0, width, height)
    ys, xs = np.where(mask)
    pad_y, pad_x = int(0.03 * height), int(0.03 * width)
    box = (max(int(xs.min()) - pad_x, 0), max(int(ys.min()) - pad_y, 0),
           min(int(xs.max()) + 1 + pad_x, width), min(int(ys.max()) + 1 + pad_y, height))
    return mask, box


def _hsv_histogram(crop: np.ndarray, mask: np.ndarray) -> np.ndarray:
    hsv = np.asarray(Image.fromarray(crop).convert("HSV"), dtype=np.int32)
    pixels = hsv[mask] if int(mask.sum()) >= 50 else hsv.reshape(-1, 3)
    hue, sat, val = pixels[:, 0], pixels[:, 1], pixels[:, 2]
    chromatic = sat >= 40
    h_bin = np.where(chromatic, hue * 8 // 256, 0)
    s_bin = np.where(chromatic, np.where(sat < 130, 1, 2), 0)
    v_bin = np.minimum(val * 3 // 256, 2)
    counts = np.bincount(h_bin * 9 + s_bin * 3 + v_bin, minlength=HIST_DIMS).astype(np.float32)
    counts /= max(float(counts.sum()), 1.0)
    return np.sqrt(counts)  # Hellinger : le produit scalaire devient un coefficient de Bhattacharyya


def _hog(gray: np.ndarray) -> np.ndarray:
    gy, gx = np.gradient(gray.astype(np.float32))
    magnitude = np.hypot(gx, gy)
    angle = np.mod(np.arctan2(gy, gx), np.pi)
    bins = np.minimum((angle / np.pi * 8).astype(np.int32), 7)
    cells = np.zeros((4, 4, 8), dtype=np.float32)
    for row in range(4):
        for col in range(4):
            sl = (slice(row * 16, (row + 1) * 16), slice(col * 16, (col + 1) * 16))
            cells[row, col] = np.bincount(bins[sl].ravel(), weights=magnitude[sl].ravel(), minlength=8)
    cells /= np.linalg.norm(cells, axis=2, keepdims=True) + 1e-6
    flat = cells.ravel()
    return flat / (np.linalg.norm(flat) + 1e-6)


def _dhash(gray_image: Image.Image) -> int:
    pixels = np.asarray(gray_image.resize((9, 8), Image.LANCZOS), dtype=np.int16)
    bits = (pixels[:, 1:] > pixels[:, :-1]).ravel()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def make_thumbnail(image: Image.Image, size: int = 112, quality: int = 55) -> bytes:
    thumb = _to_rgb(image)
    thumb.thumbnail((size, size))
    buffer = io.BytesIO()
    thumb.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def compute_descriptor(image: Image.Image) -> ImageDescriptor | None:
    """Descripteur d'une photo produit. ``None`` si l'image est inexploitable."""
    try:
        rgb = _to_rgb(image)
        original_size = rgb.size
        rgb.thumbnail((192, 192))
        arr = np.asarray(rgb, dtype=np.uint8)
        mask, (x0, y0, x1, y1) = _subject_mask(arr)
        crop, crop_mask = arr[y0:y1, x0:x1], mask[y0:y1, x0:x1]
        if crop.shape[0] < 8 or crop.shape[1] < 8:
            return None
        crop_image = Image.fromarray(crop)
        layout = np.asarray(crop_image.resize((3, 3), Image.BOX), dtype=np.float32).ravel() / 255.0
        gray = crop_image.convert("L")
        hog = _hog(np.asarray(gray.resize((64, 64), Image.LANCZOS)))
        aspect = np.clip(np.log(crop.shape[1] / crop.shape[0]), -1.5, 1.5) / 1.5
        shape = np.array([aspect, float(crop_mask.mean())], dtype=np.float32)
        vector = np.concatenate([_hsv_histogram(crop, crop_mask), layout, hog, shape]).astype(np.float32)
        return ImageDescriptor(vector, _dhash(gray), make_thumbnail(rgb), *original_size)
    except Exception:  # noqa: BLE001 - une image corrompue ne doit jamais bloquer l'indexation
        return None


def vector_from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


def _popcount64(values: np.ndarray) -> np.ndarray:
    bits = np.unpackbits(values.astype(np.uint64).view(np.uint8).reshape(-1, 8), axis=1)
    return bits.sum(axis=1)


def similarity_scores(query: ImageDescriptor, matrix: np.ndarray, dhashes: np.ndarray) -> np.ndarray:
    """Similarité visuelle dans [0, 1] entre la photo cible et ``N`` descripteurs (matrice N x 229)."""
    if matrix.size == 0:
        return np.zeros(0, dtype=np.float32)
    q = query.vector
    hist = matrix[:, _S_HIST] @ q[_S_HIST]
    layout = np.clip(1.0 - 2.0 * np.abs(matrix[:, _S_LAYOUT] - q[_S_LAYOUT]).mean(axis=1), 0.0, 1.0)
    hog = np.clip(matrix[:, _S_HOG] @ q[_S_HOG], 0.0, 1.0)
    d_aspect = np.abs(matrix[:, _S_SHAPE][:, 0] - q[_S_SHAPE][0])
    d_fill = np.abs(matrix[:, _S_SHAPE][:, 1] - q[_S_SHAPE][1])
    shape = np.clip(1.0 - (d_aspect + d_fill) / 2.0, 0.0, 1.0)
    xor = np.bitwise_xor(dhashes.astype(np.uint64), np.uint64(query.dhash))
    dhash = 1.0 - _popcount64(xor) / 64.0
    raw = (WEIGHTS["hist"] * hist + WEIGHTS["layout"] * layout + WEIGHTS["hog"] * hog
           + WEIGHTS["shape"] * shape + WEIGHTS["dhash"] * dhash)
    return np.clip((raw - RAW_BASE) / RAW_SPAN, 0.0, 1.0).astype(np.float32)


def similarity_pair(a: ImageDescriptor, b: ImageDescriptor) -> float:
    return float(similarity_scores(a, b.vector[None, :], np.array([b.dhash], dtype=np.uint64))[0])


# --- Récupération d'images web (sources officielles uniquement, protégée contre le SSRF) -----------------------


def is_public_http_url(url: str) -> bool:
    """Refuse tout ce qui n'est pas http(s) public : localhost, réseaux privés, adresses réservées."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port not in (None, 80, 443):
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            address = ipaddress.ip_address(info[4][0])
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
                return False
        return True
    except (OSError, ValueError):
        return False


def fetch_remote_image(session: Any, url: str, *, max_bytes: int = 2_500_000, timeout: float = 8.0) -> Image.Image | None:
    """Télécharge une image publique (3 redirections max, taille bornée). Erreurs -> ``None``."""
    current = url
    for _ in range(4):
        if not is_public_http_url(current):
            return None
        try:
            response = session.get(current, timeout=timeout, stream=True, allow_redirects=False)
        except Exception:  # noqa: BLE001 - réseau : on ignore cette image
            return None
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                current = urljoin(current, response.headers.get("Location", ""))
                continue
            if response.status_code != 200 or "image" not in response.headers.get("Content-Type", "image").lower():
                return None
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > max_bytes:
                    return None
                chunks.append(chunk)
            return load_image(b"".join(chunks), max_bytes)
        finally:
            response.close()
    return None


def require_descriptor(data: bytes) -> ImageDescriptor:
    """Pour l'interface : photo cible téléversée -> descripteur, ou erreur lisible."""
    image = load_image(data)
    if image is None:
        raise SourcingError("Image illisible ou trop lourde (8 Mo max). Formats : JPG, PNG, WEBP.")
    descriptor = compute_descriptor(image)
    if descriptor is None:
        raise SourcingError("Impossible d'analyser cette image : essayez une photo plus grande et nette du produit.")
    return descriptor
