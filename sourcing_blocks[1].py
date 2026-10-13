"""Découpage du texte d'une page en blocs produits / passages courts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from sourcing_textutils import (
    extract_packaging,
    extract_references,
    extract_weights,
    normalize,
)

MAX_BLOCK_CHARS = 700
MIN_BLOCK_CHARS = 40
_NOISE_LINE = re.compile(r"^(?:\d{1,3}|page\s*\d+|www\.\S+|https?://\S+|[-–—_=*·•\s]+)$", re.IGNORECASE)
_BOILERPLATE = re.compile(
    r"sommaire|table des mati[eè]res|table of contents|mentions l[ée]gales|conditions g[ée]n[ée]rales|"
    r"legal notice|terms and conditions|all rights reserved|tous droits r[ée]serv[ée]s",
    re.IGNORECASE,
)


@dataclass
class Block:
    text: str
    title: str
    refs: list[str] = field(default_factory=list)
    weights_g: list[float] = field(default_factory=list)
    packaging: list[str] = field(default_factory=list)
    structured: bool = False
    boilerplate: bool = False

    @property
    def norm_text(self) -> str:
        return normalize(self.text)

    @property
    def content_hash(self) -> str:
        return hashlib.sha1(self.norm_text.encode("utf-8")).hexdigest()


def looks_like_title(line: str) -> bool:
    """Ligne courte, majoritairement alphabétique, en MAJUSCULES ou Casse De Titre."""
    stripped = line.strip(" -–—•·*:")
    if not 3 <= len(stripped) <= 80 or stripped.endswith((".", ";", ",")):
        return False
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) < 3 or len(letters) / len(stripped) < 0.5:
        return False
    upper_ratio = sum(c.isupper() for c in letters) / len(letters)
    words = stripped.split()
    title_case = len(words) <= 8 and sum(w[:1].isupper() for w in words) >= max(1, len(words) - 1)
    return upper_ratio > 0.6 or title_case


def _has_clues(text: str) -> bool:
    return bool(extract_weights(text) or extract_references(text) or extract_packaging(text))


def _is_anchor(lines: list[str], idx: int) -> bool:
    """Début probable d'une fiche produit : titre suivi d'indices, ou ligne de référence isolée."""
    line = lines[idx]
    previous_is_title = idx > 0 and looks_like_title(lines[idx - 1])
    is_ref_line = bool(
        re.match(r"^\s*(?:r[ée]f|code|art)\b", line, re.IGNORECASE) or re.match(r"^\s*\d{5,8}\s+\S", line)
    )
    if is_ref_line:
        return not previous_is_title
    if looks_like_title(line):
        return _has_clues(" ".join(lines[idx : idx + 4]))
    return False


def _split_group(lines: list[str]) -> list[tuple[list[str], bool]]:
    """Coupe un groupe de lignes aux ancres. Retourne (lignes, commence_par_une_ancre)."""
    anchors = [i for i in range(len(lines)) if _is_anchor(lines, i)]
    cuts = sorted({0, *anchors})
    ends = [*cuts[1:], len(lines)]
    return [(lines[a:b], a in anchors) for a, b in zip(cuts, ends) if lines[a:b]]


def _chunk(lines: list[str]) -> list[str]:
    chunks, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) > MAX_BLOCK_CHARS:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _make_block(text: str) -> Block:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    title = next((ln.strip() for ln in lines if looks_like_title(ln)), lines[0].strip() if lines else "")
    weights = extract_weights(text)
    refs = extract_references(text)
    packaging = extract_packaging(text)
    clue_count = sum(bool(x) for x in (weights, refs, packaging))
    return Block(
        text=text.strip(),
        title=title[:120],
        refs=refs,
        weights_g=weights,
        packaging=packaging,
        structured=(looks_like_title(title) and clue_count >= 1) or (bool(refs) and clue_count >= 2),
        boilerplate=bool(_BOILERPLATE.search(text)),
    )


def build_blocks(page_text: str) -> list[Block]:
    """Découpe une page en blocs (produit si possible, sinon passage court), dédupliqués."""
    if not page_text or not page_text.strip():
        return []
    raw_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in page_text.splitlines()]
    groups: list[list[str]] = [[]]
    for line in raw_lines:
        if not line:
            if groups[-1]:
                groups.append([])
        elif not _NOISE_LINE.match(line):
            groups[-1].append(line)
    fragments: list[tuple[str, bool]] = []
    for group in groups:
        if not group:
            continue
        for lines, anchored in _split_group(group):
            for index, chunk in enumerate(_chunk(lines)):
                fragments.append((chunk, anchored and index == 0))
    merged: list[str] = []
    for text, anchored in fragments:
        if merged and not anchored and len(text) < MIN_BLOCK_CHARS:
            merged[-1] = f"{merged[-1]}\n{text}"
        else:
            merged.append(text)
    blocks: list[Block] = []
    seen: set[str] = set()
    for text in merged:
        block = _make_block(text)
        if len(block.norm_text) < 8 or block.content_hash in seen:
            continue
        seen.add(block.content_hash)
        blocks.append(block)
    return blocks
