"""Fournisseurs déjà référencés : reconnaissance tolérante mais prudente.

Objectifs :
- savoir si une entreprise trouvée sur Internet est déjà chez Délifrance (« déjà connu » / « nouveau ») ;
- ne pas dépenser de requêtes sur des fournisseurs déjà référencés ;
- retrouver le nom officiel d'un fournisseur à partir du nom d'un fichier catalogue.

Le nom est ramené à son « cœur » : formes juridiques (GmbH, SRL, S.L., AG, NV...), pays/branches
(Germany, Italia, Europe, IT...), codes numériques et lettres isolées sont retirés. « Dupont
Foods Germany GmbH & Co. KG » devient « dupont foods ».

Prudence : un nom composé uniquement de mots génériques (« Bakery SPA ») ne reconnaît jamais une
entreprise par simple ressemblance, pour ne pas écarter de vrais nouveaux fabricants.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from sourcing_textutils import normalize

_TRAILING = frozenset(
    """gmbh co kg ag sa sas sarl srl spa sl slu sau bv bvba nv ab as ltd limited inc llc oy aps zoo snc unico socio
    con di e a s dd d it nl dk de fr es uk europe europa branch deutschland germany italia italy espana spain
    france netherlands nederland belgium belgique suisse switzerland schweiz sweden sverige denmark danmark""".split()
)
_GENERIC = frozenset(
    """bakery bakeries bake baker bakers food foods foodgroup pastry pastries group bageri backerei boulangerie
    panificio panaderia surgeles frozen industrial industrie dolce dolci""".split()
)


def core_tokens(name: str) -> tuple[str, ...]:
    tokens = normalize(name).split()
    while tokens and (tokens[-1] in _TRAILING or len(tokens[-1]) == 1 or any(c.isdigit() for c in tokens[-1])):
        tokens.pop()
    while len(tokens) > 1 and tokens[0] == "the":
        tokens.pop(0)
    return tuple(tokens)


@dataclass(frozen=True)
class KnownSupplier:
    raw: str
    tokens: tuple[str, ...]

    @property
    def compact(self) -> str:
        return "".join(self.tokens)

    @property
    def generic_only(self) -> bool:
        return all(t in _GENERIC for t in self.tokens)


@dataclass(frozen=True)
class KnownMatch:
    supplier: str
    reason: str


class SupplierDirectory:
    def __init__(self, names: Iterable[str] = ()) -> None:
        cleaned = [n.strip() for n in names if n and n.strip()]
        built = [KnownSupplier(raw, core_tokens(raw)) for raw in dict.fromkeys(cleaned)]
        self.suppliers = [s for s in built if s.tokens]

    def __len__(self) -> int:
        return len(self.suppliers)

    @classmethod
    def from_file(cls, path: str | Path) -> "SupplierDirectory":
        file = Path(path)
        if not file.exists():
            return cls()
        lines = file.read_text(encoding="utf-8-sig").splitlines()
        return cls(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))

    def with_names(self, extra: Iterable[str]) -> "SupplierDirectory":
        return SupplierDirectory([*(s.raw for s in self.suppliers), *extra])

    # -- reconnaissance d'une entreprise trouvée sur Internet --------------

    def match(self, names: Iterable[str], domain: str = "", text: str = "") -> KnownMatch | None:
        """``names`` : noms candidats (nom déduit du domaine, titres). ``text`` : titres pour recherche d'expression."""
        label = normalize(domain.split(".")[0]).replace(" ", "") if domain else ""
        candidates = [("".join(core_tokens(n)), n) for n in names if n]
        if label:
            candidates.append((label, domain))
        text_tokens = core_tokens(text) if text else ()
        for supplier in self.suppliers:
            compact = supplier.compact
            for cand_compact, _ in candidates:
                if len(cand_compact) < 4:
                    continue
                if cand_compact == compact:
                    return KnownMatch(supplier.raw, "même nom")
                if supplier.generic_only:
                    continue
                shorter, longer = sorted((cand_compact, compact), key=len)
                if len(shorter) >= 6 and longer.startswith(shorter):
                    return KnownMatch(supplier.raw, "nom ou domaine proche")
                if min(len(cand_compact), len(compact)) >= 8 and SequenceMatcher(None, cand_compact, compact).ratio() >= 0.9:
                    return KnownMatch(supplier.raw, "nom très proche")
            if len(supplier.tokens) >= 2 and not supplier.generic_only and _contains(text_tokens, supplier.tokens):
                return KnownMatch(supplier.raw, "nom cité dans la page")
        return None

    # -- nom officiel d'après un nom de fichier catalogue ---------------

    def canonical_for_stem(self, stem: str) -> str | None:
        """Fournisseur dont le nom commence par le début du fichier (préfixe unique le plus long)."""
        tokens = normalize(stem).split()
        for size in range(min(4, len(tokens)), 0, -1):
            prefix = tuple(tokens[:size])
            found = {s.raw for s in self.suppliers if s.tokens[:size] == prefix and len(s.tokens) >= size}
            if len(found) == 1 and not (size == 1 and prefix[0] in _GENERIC):
                return next(iter(found))
        return None


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    size = len(needle)
    return any(haystack[i : i + size] == needle for i in range(len(haystack) - size + 1))
