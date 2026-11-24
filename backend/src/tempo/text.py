"""Text normalisation shared by the deterministic parsers (conversation, coach plans)."""

import unicodedata


def plain(text: str) -> str:
    """Case- and accent-insensitive form of ``text`` ("Fractionné" -> "fractionne")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
