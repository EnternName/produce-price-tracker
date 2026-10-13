"""Text normalization shared by the unit parser and the product matcher (Spanish + Catalan)."""

from __future__ import annotations

import re
import unicodedata

_SPACES = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    # Also folds ñ->n and ç->c: OCR and keyboard input often drop them, so matching is more forgiving.
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fold(text: str) -> str:
    """Lowercase, strip accents, normalize Catalan l·l and odd punctuation, collapse spaces."""
    text = text.replace("l·l", "ll").replace("L·L", "LL").replace("l.l", "ll")
    text = text.replace("’", "'").replace("´", "'").replace("`", "'")
    text = strip_accents(text.lower())
    return _SPACES.sub(" ", text).strip()


_WORD = re.compile(r"[a-z0-9ñç]+")


def words(text: str) -> list[str]:
    """Alphanumeric word tokens of folded text; Catalan elisions (l'oli, d'oliva) split apart."""
    return _WORD.findall(fold(text).replace("'", " "))
