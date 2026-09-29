"""Turkish-aware text normalization and name matching."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Characters that differ between Turkish and ASCII spellings.
_TR_TO_ASCII = str.maketrans(
    {
        "ı": "i", "İ": "I", "ş": "s", "Ş": "S", "ğ": "g", "Ğ": "G",
        "ü": "u", "Ü": "U", "ö": "o", "Ö": "O", "ç": "c", "Ç": "C",
        "â": "a", "Â": "A", "î": "i", "Î": "I", "û": "u", "Û": "U",
    }
)


def _strip_marks(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def tr_lower(s: str) -> str:
    """Lowercase with Turkish dotted/dotless I rules (I -> ı, İ -> i)."""
    return s.replace("I", "ı").replace("İ", "i").lower()


def fold(s: str) -> str:
    """Normalize text for matching: Turkish lowercase, then fold to plain ASCII letters.

    'AHMET YILMAZ', 'Ahmet Yılmaz' and 'ahmet yilmaz' all fold to 'ahmet yilmaz'.
    """
    return _strip_marks(tr_lower(s).translate(_TR_TO_ASCII))


def ascii_translit(s: str) -> str:
    """Case-preserving transliteration to ASCII: 'Ahmet Yılmaz' -> 'Ahmet Yilmaz'."""
    return _strip_marks(s.translate(_TR_TO_ASCII))


def spelling_forms(s: str) -> list[str]:
    """The original spelling plus its ASCII transliteration (deduplicated, order kept)."""
    forms = [s.strip(), ascii_translit(s.strip())]
    return list(dict.fromkeys(f for f in forms if f))


def collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=512)
def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Regex that finds `phrase` inside folded text, tolerant of whitespace and hyphens.

    Tokens ending in '.' (initials such as 'A.') may be followed by optional whitespace.
    """
    tokens = fold(phrase).split()
    if not tokens:
        raise ValueError("empty phrase")
    parts: list[str] = []
    for i, tok in enumerate(tokens):
        parts.append(re.escape(tok))
        if i < len(tokens) - 1:
            parts.append(r"\s*" if tok.endswith(".") else r"[\s\-_]+")
    return re.compile(r"(?<!\w)" + "".join(parts) + r"(?!\w)")


def find_spans(folded_text: str, phrases: list[str]) -> list[tuple[int, int]]:
    """All non-overlapping match spans of any phrase in already-folded text.

    Overlapping matches (e.g. 'Ahmet Yılmaz' inside 'Ahmet Kemal Yılmaz' never overlaps,
    but 'Yılmaz' inside 'Ahmet Yılmaz' would) are merged so each mention counts once.
    """
    spans: list[tuple[int, int]] = []
    for p in phrases:
        if not p.strip():
            continue
        spans.extend(m.span() for m in phrase_pattern(p).finditer(folded_text))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def count_mentions(text: str, names: list[str]) -> int:
    """Case-insensitive, Turkish-aware count of name mentions in raw text."""
    return len(find_spans(fold(text), names))


def contains_phrase(folded_text: str, phrase: str) -> bool:
    return bool(phrase.strip()) and phrase_pattern(phrase).search(folded_text) is not None
