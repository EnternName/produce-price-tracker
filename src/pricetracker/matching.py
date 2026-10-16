"""Link a raw offer name ("Plàtan de Canàries IGP", "Leche entera Hacendado 6x1L") to a catalog product.

Strategy, in order:
1. exact alias (a name the user already assigned by hand in the review screen);
2. phrase match: every word of a product synonym appears in the offer name, and the synonym's
   first word is the offer's head noun (first word). Spanish and Catalan put the noun first, which
   keeps "zumo de naranja" away from "naranja" and "pechuga de pollo" away from "pollo".
Longer synonyms win, so "leche sin lactosa" beats "leche".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .textnorm import fold, words

STOPWORDS = frozenset(
    """
    de del d la el l lo los las les els en con amb y i e a al para per x un una
    pack formato format bandeja safata malla bolsa bossa tarrina bote pot lata llauna botella ampolla
    brick caja capsa paquete paquet envase envas granel aprox origen espana espanya nacional
    categoria cat calidad qualitat unidad unidades ud uds u unitat unitats pieza piezas peca peces
    kg g gr grs l ml cl lt mg
    """.split()
)

# Words that may precede the head noun without being it ("Nuevo yogur griego", "Bio manzana").
PREFIX_WORDS = frozenset(
    "nuevo nueva nou nova mini maxi bio eco ecologico ecologica ecologic organic super gran premium "
    "gourmet seleccion seleccio oferta promo".split()
)

_DIGITS = re.compile(r"^\d+$")


def _forms(token: str) -> frozenset[str]:
    """Singular candidates for Spanish/Catalan plurals: tomates->tomate, limones->limon, pomes->poma,
    taronges->taronja, pastanagues->pastanaga, nueces->nuez, xampinyons->xampinyo, alberginies->alberginia."""
    forms = {token}
    if len(token) > 3 and token.endswith("s"):
        forms.add(token[:-1])
        if token.endswith("es") and len(token) > 4:
            stem = token[:-2]
            forms.update({stem, stem + "a"})
            if stem.endswith("g"):
                forms.add(stem[:-1] + "ja")
            if stem.endswith("gu"):  # pastanagues -> pastanaga
                forms.add(stem[:-1] + "a")
            if stem.endswith("qu"):  # vaques -> vaca
                forms.add(stem[:-2] + "ca")
            if stem.endswith("c"):
                forms.add(stem[:-1] + "z")
            if stem.endswith("i"):
                forms.add(stem + "a")
        if token.endswith("ns"):
            forms.add(token[:-2])
    return frozenset(forms)


def content_tokens(text: str, drop: Iterable[str] = ()) -> list[str]:
    dropped = set(drop)
    return [w for w in words(text) if w not in STOPWORDS and w not in dropped and not _DIGITS.match(w)]


@dataclass(frozen=True)
class Match:
    product_id: int
    method: str  # "alias" | "phrase"
    score: float  # 0..1, share of the offer's words explained by the synonym
    phrase: str


@dataclass(frozen=True)
class _Phrase:
    product_id: int
    text: str
    tokens: tuple[frozenset[str], ...]


class Matcher:
    def __init__(self, aliases: Iterable[tuple[int, str, str]]):
        """`aliases`: (product_id, alias text, source) where source is 'seed' or 'user'."""
        self._exact: dict[str, int] = {}
        self._phrases: list[_Phrase] = []
        for product_id, alias, source in aliases:
            if source == "user":
                self._exact[fold(alias)] = product_id
            tokens = content_tokens(alias)
            if tokens:
                self._phrases.append(_Phrase(product_id, alias, tuple(_forms(t) for t in tokens)))

    def match(self, *names: str | None, brand: str | None = None) -> Match | None:
        """Try each name in turn (e.g. the generic name first, then the printed one)."""
        for name in names:
            if not name:
                continue
            if (product_id := self._exact.get(fold(name))) is not None:
                return Match(product_id, "alias", 1.0, name)
            if found := self._match_phrase(name, brand):
                return found
        return None

    def _match_phrase(self, name: str, brand: str | None) -> Match | None:
        tokens = content_tokens(name, drop=words(brand) if brand else ())
        if not tokens:
            return None
        head_positions = [0]
        if tokens[0] in PREFIX_WORDS and len(tokens) > 1:
            head_positions.append(1)
        token_forms = [_forms(t) for t in tokens]

        best: tuple[tuple[int, int, int], _Phrase] | None = None
        for phrase in self._phrases:
            first = phrase.tokens[0]
            head = next((i for i in head_positions if token_forms[i] & first), None)
            if head is None:
                continue
            positions = []
            for forms in phrase.tokens[1:]:
                pos = next((i for i in range(head + 1, len(tokens)) if token_forms[i] & forms), None)
                if pos is None:
                    break
                positions.append(pos)
            else:
                # longer synonym first, then the one whose words sit closest to the head noun
                key = (len(phrase.tokens), -head, -sum(positions))
                if best is None or key > best[0]:
                    best = (key, phrase)
        if best is None:
            return None
        phrase = best[1]
        score = min(1.0, len(phrase.tokens) / len(tokens))
        return Match(phrase.product_id, "phrase", round(score, 3), phrase.text)
